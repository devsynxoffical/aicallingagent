"""Inbound calls: the dialed number picks the campaign, and an inbound call never queues an outbound one."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from callagent import db
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.dialer.campaign_runner import reap_stale_calls
from callagent.postcall.summarizer import finalize_call

NUMBER = "+14155550111"
CALLER = "+14155550177"


@pytest.fixture
def client(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.ngrok.app")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'in.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("INBOUND_CAMPAIGN", raising=False)
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'in.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        c.post("/api/campaigns", json={"name": "front-desk", "playbook": sample_playbook.model_dump()})
        yield c
    get_settings.cache_clear()


def _ring(client, sid="CAin1", caller=CALLER, to=NUMBER):
    return client.post("/twilio/inbound", data={"CallSid": sid, "From": caller, "To": to})


def test_a_number_nobody_answers_gets_a_polite_message_not_an_agent(client):
    r = _ring(client)
    assert r.status_code == 200 and "<Say>" in r.text and "<Hangup" in r.text and "<Stream" not in r.text
    with db.session_scope() as s:
        assert s.query(db.CallRun).count() == 0 and s.query(db.Lead).count() == 0


def test_inbound_number_is_validated_and_cannot_be_shared(client, sample_playbook):
    assert client.post("/api/campaigns/front-desk/inbound", params={"number": "12"}).status_code == 400
    r = client.post("/api/campaigns/front-desk/inbound", params={"number": "(415) 555-0111"})
    assert r.status_code == 200 and r.json()["inbound_number"] == NUMBER and r.json()["languages"] == ["en"]
    client.post("/api/campaigns", json={"name": "other", "playbook": sample_playbook.model_dump()})
    clash = client.post("/api/campaigns/other/inbound", params={"number": NUMBER})
    assert clash.status_code == 409 and "front-desk" in clash.json()["detail"]
    assert client.post("/api/campaigns/front-desk/inbound").json()["inbound_number"] is None  # stop answering


def test_inbound_call_reaches_the_agent_with_its_own_call_record(client):
    client.post("/api/campaigns/front-desk/inbound", params={"number": NUMBER})
    r = _ring(client)
    assert r.status_code == 200 and "<Stream" in r.text and 'value="inbound"' in r.text and 'name="stream_token"' in r.text
    with db.session_scope() as s:
        run = s.query(db.CallRun).one()
        lead = s.get(db.Lead, run.lead_id)
        assert (run.direction, run.mode, run.status, run.twilio_call_sid) == ("inbound", "inbound", "in-progress", "CAin1")
        assert run.stream_token and f'value="{run.stream_token}"' in r.text
        assert lead.phone_e164 == CALLER and lead.status == "calling" and lead.attempts == 0
        assert lead.extra == {"source": "inbound call"}
        # The media stream for this call is accepted exactly like an outbound one.
        from callagent.server import authorize_stream

        body = {"call_run_id": str(run.id), "stream_token": run.stream_token}
        assert authorize_stream(s, body, "CAin1") is None
        assert authorize_stream(s, {**body, "stream_token": "guess"}, "CAin1") == "bad stream token"

    # Twilio retries a webhook it thinks failed: same call, not a second one.
    again = _ring(client)
    assert "<Stream" in again.text
    with db.session_scope() as s:
        assert s.query(db.CallRun).count() == 1 and s.query(db.Lead).count() == 1
    calls = client.get("/api/campaigns/front-desk/calls").json()
    assert calls[0]["direction"] == "inbound"


def test_fallback_campaign_answers_numbers_without_their_own(client, monkeypatch):
    monkeypatch.setenv("INBOUND_CAMPAIGN", "front-desk")
    get_settings.cache_clear()
    assert "<Stream" in _ring(client, to="+14155550999").text


def test_withheld_caller_id_gets_its_own_lead_and_no_callback_or_text(client):
    client.post("/api/campaigns/front-desk/inbound", params={"number": NUMBER})
    _ring(client, sid="CAanon1", caller="anonymous")
    _ring(client, sid="CAanon2", caller="")
    with db.session_scope() as s:
        phones = sorted(l.phone_e164 for l in s.query(db.Lead))
        assert phones == ["anon:CAanon1", "anon:CAanon2"]  # two callers, two leads


def test_caller_who_hangs_up_before_talking_goes_back_where_they_were(client):
    client.post("/api/campaigns/front-desk/inbound", params={"number": NUMBER})
    with db.session_scope() as s:
        c = db.get_campaign_by_name(s, "front-desk")
        s.add(db.Lead(campaign_id=c.id, phone_e164=CALLER, status="pending", attempts=1))
    _ring(client)
    # The number's status callback has no call_run_id: the call is found by its SID.
    r = client.post("/twilio/status", data={"CallSid": "CAin1", "CallStatus": "completed", "CallDuration": "2"})
    assert r.status_code == 200
    with db.session_scope() as s:
        run = s.query(db.CallRun).one()
        lead = s.get(db.Lead, run.lead_id)
        assert run.status == "completed" and run.disposition is None
        assert lead.status == "pending" and lead.attempts == 1 and lead.next_attempt_at is None  # untouched


def test_do_not_call_caller_is_answered_but_stays_on_the_list(client):
    client.post("/api/campaigns/front-desk/inbound", params={"number": NUMBER})
    with db.session_scope() as s:
        c = db.get_campaign_by_name(s, "front-desk")
        s.add(db.Lead(campaign_id=c.id, phone_e164=CALLER, status="dnc"))
        db.add_dnc(s, CALLER, db.default_organization_id(s))
    assert "<Stream" in _ring(client).text
    with db.session_scope() as s:
        assert s.query(db.Lead).one().status == "dnc"


def test_finished_inbound_conversation_never_schedules_an_outbound_retry(client, sample_playbook, monkeypatch):
    client.post("/api/campaigns/front-desk/inbound", params={"number": NUMBER})
    _ring(client)
    with db.session_scope() as s:
        run_id = s.query(db.CallRun).one().id
    # The review model is not reachable in tests; the call must still close cleanly.
    monkeypatch.setattr("callagent.postcall.summarizer.make_client", lambda settings=None: (_ for _ in ()).throw(RuntimeError("offline")))
    transcript = [{"role": "user", "text": "Hi, what do you do?"}, {"role": "assistant", "text": "We automate bookkeeping."}]
    asyncio.run(
        finalize_call(run_id, sample_playbook, transcript, [], "inbound", "other", Settings(anthropic_api_key="x"),
                      language={"detected": "en", "final": "en", "turns_by_language": {"en": 1}, "switches": []}, direction="inbound")
    )
    with db.session_scope() as s:
        run = s.get(db.CallRun, run_id)
        lead = s.get(db.Lead, run.lead_id)
        assert lead.status == "completed" and lead.next_attempt_at is None  # an outbound "other" would be retried
        assert run.language == "en" and run.summary["language"]["detected"] == "en"
        assert run.transcript == transcript


def test_stale_inbound_call_is_released_not_requeued(client):
    from datetime import timedelta

    client.post("/api/campaigns/front-desk/inbound", params={"number": NUMBER})
    _ring(client)
    with db.session_scope() as s:
        run = s.query(db.CallRun).one()
        run.started_at = db.utcnow() - timedelta(hours=3)
        cid = run.campaign_id
    with db.session_scope() as s:
        assert reap_stale_calls(s, cid, Settings(anthropic_api_key="x")) == 1
    with db.session_scope() as s:
        lead = s.query(db.Lead).one()
        assert lead.status == "completed" and lead.next_attempt_at is None


def test_campaign_language_the_providers_cannot_hear_is_refused(client, sample_playbook):
    sample_playbook.additional_languages = ["ar"]  # Deepgram multilingual is not confirmed for Arabic by default
    client.post("/api/campaigns", json={"name": "gulf", "playbook": sample_playbook.model_dump()})
    r = client.post("/api/campaigns/gulf/inbound", params={"number": "+14155550122"})
    assert r.status_code == 400 and "Arabic" in r.json()["detail"]
