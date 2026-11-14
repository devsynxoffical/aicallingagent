"""The calling engine under things going wrong: providers failing, two sockets for one
call, two campaigns for one person, an opt-out the model reported the other way."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from callagent import db, server
from callagent.config import Settings, get_settings
from callagent.db import CallRun, Campaign, Lead, session_scope, utcnow
from callagent.dialer import reconcile, rules
from callagent.dialer.campaign_runner import LeadNotDialable, claim_lead_for_call
from callagent.dialer.reconcile import reconcile_calls
from callagent.postcall import summarizer
from callagent.training import ingest, versions, web
from callagent.training.ingest import IngestError

SETTINGS = Settings(anthropic_api_key="x", twilio_account_sid="AC1", twilio_auth_token="secret", twilio_from_number="+14155550000", public_base_url="https://example.test", max_attempts=3)
PHONE = "+14155550100"


@pytest.fixture
def campaigns(tmp_path, sample_playbook, monkeypatch):
    """Two campaigns of one organization that both hold the same person."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'r.db'}")
    out = {}
    with session_scope() as s:
        for name in ("q4", "renewals"):
            c = Campaign(name=name, status="running")
            s.add(c)
            s.flush()
            versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
            lead = Lead(campaign_id=c.id, phone_e164=PHONE, status="pending")
            s.add(lead)
            s.flush()
            out[name] = {"campaign": c.id, "lead": lead.id}
    yield out
    get_settings.cache_clear()


def add_call(campaign_id, lead_id, **values):
    with session_scope() as s:
        run = CallRun(lead_id=lead_id, campaign_id=campaign_id, **{"status": "completed", "direction": "outbound", **values})
        s.add(run)
        s.flush()
        return run.id


# ----------------------------------------------------------------------------- one person, one call (audit B-4)


def test_two_campaigns_cannot_ring_the_same_person_at_the_same_time(campaigns):
    with session_scope() as s:
        first, _ = claim_lead_for_call(s, campaigns["q4"]["campaign"], campaigns["q4"]["lead"])
    with pytest.raises(LeadNotDialable, match="on a call from another campaign"):
        with session_scope() as s:
            claim_lead_for_call(s, campaigns["renewals"]["campaign"], campaigns["renewals"]["lead"])
    with session_scope() as s:
        assert s.get(Lead, campaigns["renewals"]["lead"]).status == "pending"  # still due: it is called once the line is free
        s.get(CallRun, first).status = "completed"
    with session_scope() as s:
        assert claim_lead_for_call(s, campaigns["renewals"]["campaign"], campaigns["renewals"]["lead"])[1] == PHONE


# ----------------------------------------------------------------------------- the media socket (audit B-10, B-6)


def test_of_many_sockets_presenting_the_same_valid_token_exactly_one_gets_the_call(campaigns):
    run_id = add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], status="in-progress", stream_token="tok-123", twilio_call_sid="CA1")
    body = {"call_run_id": str(run_id), "stream_token": "tok-123"}

    def connect(_):
        with session_scope() as s:
            return server.authorize_stream(s, dict(body), "CA1")

    with ThreadPoolExecutor(max_workers=12) as pool:
        answers = list(pool.map(connect, range(12)))
    assert answers.count(None) == 1  # one pipeline, however the requests interleave
    assert set(answers) == {None, "stream already connected for this call"}
    with session_scope() as s:
        assert server.authorize_stream(s, {**body, "stream_token": "wrong"}, "CA1") == "bad stream token"


def test_a_socket_that_says_nothing_is_dropped(campaigns, monkeypatch):
    monkeypatch.setattr(server, "WS_HANDSHAKE_SECONDS", 0.2)
    with TestClient(server.app) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/ws") as socket:
                socket.receive_text()  # we never send the provider's opening messages
        assert closed.value.code == 4008


# ----------------------------------------------------------------------------- reconciliation (audit B-9)


class Provider:
    def __init__(self, by_sid=None, by_number=None):
        self.by_sid, self.by_number = by_sid or {}, by_number or {}

    def fetch_call(self, sid, settings):
        return self.by_sid.get(sid)

    def find_call(self, phone, started_after, settings):
        return self.by_number.get(phone)


def test_one_call_that_cannot_be_reconciled_does_not_stop_the_rest(campaigns, monkeypatch):
    old = utcnow() - timedelta(minutes=40)
    broken = add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], status="ringing", twilio_call_sid="CAbroken", started_at=old)
    fine = add_call(campaigns["renewals"]["campaign"], campaigns["renewals"]["lead"], status="ringing", twilio_call_sid="CAfine", started_at=old)
    real_apply = reconcile._apply

    def apply(s, candidate, found, settings):
        if candidate["id"] == broken:
            raise RuntimeError("something about this one call is wrong")
        return real_apply(s, candidate, found, settings)

    monkeypatch.setattr(reconcile, "_apply", apply)
    provider = Provider(by_sid={"CAbroken": {"sid": "CAbroken", "status": "completed", "duration": "5"}, "CAfine": {"sid": "CAfine", "status": "no-answer", "duration": "0"}})
    assert asyncio.run(reconcile_calls(SETTINGS, provider)) == {"failed to apply": 1, "ended": 1}
    with session_scope() as s:
        assert (s.get(CallRun, broken).status, s.get(CallRun, fine).status) == ("ringing", "no-answer")


def test_a_provider_call_that_belongs_to_another_record_is_not_adopted(campaigns):
    old = utcnow() - timedelta(minutes=40)
    theirs = add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], status="completed", twilio_call_sid="CAshared", started_at=old)
    ours = add_call(campaigns["renewals"]["campaign"], campaigns["renewals"]["lead"], status="created", twilio_call_sid=None, started_at=old)
    # Looking the unconfirmed call up by number finds the newest call to that person: the other campaign's.
    provider = Provider(by_number={PHONE: {"sid": "CAshared", "status": "completed", "duration": "61"}})
    assert asyncio.run(reconcile_calls(SETTINGS, provider)) == {"belongs to another call": 1}
    with session_scope() as s:
        assert (s.get(CallRun, ours).twilio_call_sid, s.get(CallRun, ours).status, s.get(CallRun, ours).duration_seconds) == (None, "created", None)
        assert s.get(CallRun, theirs).twilio_call_sid == "CAshared"


# ----------------------------------------------------------------------------- opt-outs (audit B-12)


async def test_an_opt_out_reaches_the_do_not_call_list_however_it_was_noticed(campaigns, sample_playbook, monkeypatch):
    monkeypatch.setattr(summarizer, "make_client", lambda settings=None: object())
    monkeypatch.setattr(summarizer, "review_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreachable")))
    run_id = add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], status="in-progress", stream_connected=True)
    # The model logged "do_not_call" as the outcome instead of using the do-not-call tool.
    await summarizer.finalize_call(run_id, sample_playbook, [{"role": "user", "text": "Stop calling me."}], [], "live", "do_not_call", get_settings())
    with session_scope() as s:
        lead = s.get(Lead, campaigns["q4"]["lead"])
        assert lead.status == "dnc" and db.is_dnc(s, PHONE, lead.organization_id)
    # So the same person in the organization's other campaign is not called either.
    with pytest.raises(LeadNotDialable, match="do-not-call"):
        with session_scope() as s:
            claim_lead_for_call(s, campaigns["renewals"]["campaign"], campaigns["renewals"]["lead"])


# ----------------------------------------------------------------------------- a provider that is down (audit B-5)


def silent_calls(campaigns, n, **values):
    for _ in range(n):
        add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], stream_connected=True, ended_at=utcnow(), started_at=utcnow(), transcript=[{"role": "user", "text": "Hello? Hello?"}], **values)


def verdict(campaigns):
    with session_scope() as s:
        return rules.check_limits(s, s.get(Campaign, campaigns["q4"]["campaign"]), SETTINGS, active_calls=0)


def test_a_run_of_answered_calls_with_no_agent_speech_stops_the_campaign(campaigns):
    silent_calls(campaigns, rules.FAILING_CALLS_TO_STOP - 1)
    assert verdict(campaigns).action == "dial"  # not yet: a few people hanging up at once is not an outage
    silent_calls(campaigns, 1)
    stopped = verdict(campaigns)
    assert stopped.action == "stop" and "speech or AI provider is probably not working" in stopped.reason

    # One call in which the agent did speak, and it is not an outage.
    add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], stream_connected=True, ended_at=utcnow(), started_at=utcnow(), transcript=[{"role": "assistant", "text": "Hi, this is Jordan."}])
    assert verdict(campaigns).action == "dial"


def test_calls_that_reached_a_real_outcome_are_not_an_outage_even_without_a_transcript(campaigns):
    # A transcript can be missing for reasons of its own (recording switched off, an older
    # record). An outcome only a conversation produces shows the agent was working.
    for outcome in ("meeting_booked", "not_interested", "do_not_call", "not_interested", "callback_requested"):
        add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], stream_connected=True, ended_at=utcnow(), started_at=utcnow(), disposition=outcome)
    assert verdict(campaigns).action == "dial"

    silent_calls(campaigns, rules.FAILING_CALLS_TO_STOP, disposition="no_answer")  # what a silent call is recorded as
    assert verdict(campaigns).action == "stop"


def test_the_breaker_counts_failures_during_calls_and_starts_fresh_when_the_campaign_is_restarted(campaigns):
    spoke = [{"role": "assistant", "text": "Hi."}]
    for _ in range(rules.FAILING_CALLS_TO_STOP):  # the agent spoke, the pipeline then failed
        add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], stream_connected=True, ended_at=utcnow(), started_at=utcnow(), transcript=spoke, error="speech service disconnected")
    assert verdict(campaigns).action == "stop"

    with session_scope() as s:  # the cause was fixed and the campaign started again: those calls no longer count
        campaign = s.get(Campaign, campaigns["q4"]["campaign"])
        campaign.status, campaign.updated_at = "running", utcnow() + timedelta(seconds=1)
    assert verdict(campaigns).action == "dial"

    for _ in range(rules.FAILING_CALLS_TO_STOP):  # a review that failed after the call is not a failed call
        add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], stream_connected=True, ended_at=utcnow(), started_at=utcnow() + timedelta(seconds=2), transcript=spoke, error="review failed: model overloaded")
    assert verdict(campaigns).action == "dial"


# ----------------------------------------------------------------------------- reading files and websites


def test_a_windows_encoded_french_file_is_read_as_french_not_as_utf16():
    french = "Tarifs: le forfait Croissance coûte 49 € par mois.".encode("cp1252")
    assert len(french) % 2 == 0  # exactly the case that used to be mis-read
    assert "forfait Croissance coûte 49" in ingest._decode(french)
    assert ingest._decode("﻿Bonjour".encode("utf-16")) .lstrip("﻿") == "Bonjour"  # real UTF-16, which announces itself, still works


def test_a_page_is_fetched_from_the_address_that_was_checked(monkeypatch):
    target, headers, extensions = web.pinned_request("https://example.com/pricing?plan=growth", "93.184.216.34")
    assert (target, headers, extensions) == ("https://93.184.216.34/pricing?plan=growth", {"Host": "example.com"}, {"sni_hostname": "example.com"})
    assert web.pinned_request("http://example.com/", "2606:2800:220:1::1")[0] == "http://[2606:2800:220:1::1]/"

    # A hostile DNS server answers the safety check with a public address and any later lookup with the cloud metadata address.
    answers = iter([["93.184.216.34"], ["169.254.169.254"], ["169.254.169.254"]])
    lookups, connected = [], []

    def resolver(host):
        lookups.append(host)
        return next(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        connected.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><body><h1>Pricing</h1><p>Growth costs 49 dollars.</p></body></html>")

    real_client = httpx.Client
    monkeypatch.setattr(web.httpx, "Client", lambda **kwargs: real_client(**{**kwargs, "transport": httpx.MockTransport(handler)}))
    final, _headers, body = web.safe_get("https://rebind.example/pricing", resolver=resolver)
    assert connected == [("93.184.216.34", "rebind.example", "rebind.example")]  # the checked address, never the second answer
    assert lookups == ["rebind.example"] and b"Growth costs 49" in body

    with pytest.raises(IngestError, match="not a public website address"):
        web.safe_get("https://rebind.example/pricing", resolver=resolver)  # an internal answer to the check itself is refused
    assert len(connected) == 1


# ----------------------------------------------------------------------------- document limits (audit C-14)


def test_an_office_file_that_unpacks_to_far_more_than_it_weighs_is_refused(monkeypatch):
    import io
    import zipfile

    from callagent.training import ingest

    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"0" * (6 * 1024 * 1024))  # 6 MB of zeros packs into a few kilobytes
    data = packed.getvalue()
    assert len(data) < 50_000
    assert ingest.validate_upload("notes.docx", data) == "docx"  # within the limit: accepted
    monkeypatch.setattr(ingest, "MAX_UNPACKED_BYTES", 5 * 1024 * 1024)
    with pytest.raises(ingest.IngestError, match="unpacks to more than 5 MB"):
        ingest.validate_upload("notes.docx", data)


def test_a_pdf_with_too_many_pages_is_refused_and_unreadable_pages_are_reported(monkeypatch):
    from fixtures import make_pdf

    from callagent.training import ingest

    warnings: list[str] = []
    sections = ingest.extract_sections("pdf", make_pdf(["Pricing starts at forty-nine dollars.", "", "", "Support is included.", ""]), warnings)
    assert [s.label for s in sections] == ["page 1", "page 4"]
    assert warnings == ["no text could be read from pages 2-3, 5 of 5 (scanned or image-only); the agent does not know their content"]

    monkeypatch.setattr(ingest, "MAX_PDF_PAGES", 4)
    with pytest.raises(ingest.IngestError, match="has 5 pages; the limit is 4"):
        ingest.extract_sections("pdf", make_pdf(["a b c d e f g h"] * 5))


def test_reading_a_pdf_stops_at_its_time_limit(monkeypatch):
    from fixtures import make_pdf

    from callagent.training import ingest

    monkeypatch.setattr(ingest, "EXTRACT_SECONDS", -1)  # the limit has already passed
    with pytest.raises(ingest.IngestError, match="took longer than"):
        ingest.extract_sections("pdf", make_pdf(["Pricing starts at forty-nine dollars."]))


def test_the_warning_is_kept_with_the_document_and_cleared_when_it_is_read_again(tmp_path):
    from fixtures import make_pdf

    from callagent import db
    from callagent.training import service

    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'warn.db'}")
    settings = Settings(anthropic_api_key="x", data_dir=tmp_path / "data")
    with session_scope() as s:
        c = Campaign(name="docs", status="draft", playbook_json={})
        s.add(c)
        s.flush()
        document = service.add_file(s, c, "brochure.pdf", make_pdf(["Pricing starts at forty-nine dollars per month.", ""]), settings)
        read = service.process_document_sync(s, document.id)
        assert read.status == "ready" and "page 2 of 2" in service.describe(read)["warning"]
        full = service.add_file(s, c, "brochure.pdf", make_pdf(["Pricing starts at forty-nine dollars per month.", "Support is included."]), settings)
        assert service.describe(service.process_document_sync(s, full.id))["warning"] is None


# ----------------------------------------------------------------------------- a model that fails mid-call (audit B-6)


async def test_a_failed_model_turn_is_covered_by_a_sentence_and_a_second_one_ends_the_call(sample_playbook):
    """The model service here is a stand-in raising its error event; what a caller
    really hears on a failing provider has not been checked on a phone call."""
    from types import SimpleNamespace

    from callagent.languages import LanguageTracker
    from callagent.voice.fallback import ASK_AGAIN, GIVE_UP, ModelFailure
    from callagent.voice.session import CallSession

    session = CallSession(call_run_id=7, lead={}, campaign_id=1, playbook=sample_playbook, settings=SETTINGS, twilio_call_sid="CA1", language=LanguageTracker(allowed=["fr", "en"]))
    spoken, ended = [], []

    async def say(sentence):
        spoken.append(sentence)

    async def end(sentence, reason):
        ended.append((sentence, reason))

    on_error = ModelFailure(session, say, end)
    await on_error(None, SimpleNamespace(exception=TimeoutError("the model went quiet")))
    assert spoken == [ASK_AGAIN["fr"]] and ended == [] and session.failure is None  # in the call's language, and the call goes on

    await on_error(None, SimpleNamespace(exception=TimeoutError("again")))
    assert ended == [(GIVE_UP["fr"], "model unavailable")]
    assert "failed 2 times" in session.failure and session.end_reason == "model unavailable"
    assert [e["tool"] for e in session.tool_events] == ["model_failure", "model_failure"]

    await on_error(None, SimpleNamespace(exception=None))  # the call is already ending: nothing more is said
    assert len(spoken) == 1 and len(ended) == 1


def test_calls_that_ended_because_the_model_kept_failing_trip_the_campaign_breaker(campaigns):
    spoke = [{"role": "assistant", "text": "Bonjour."}]
    for _ in range(rules.FAILING_CALLS_TO_STOP):
        add_call(campaigns["q4"]["campaign"], campaigns["q4"]["lead"], stream_connected=True, ended_at=utcnow(), started_at=utcnow(), transcript=spoke,
                 error="the AI model failed 2 times during the call (APITimeoutError)")
    assert verdict(campaigns).action == "stop"


def test_the_live_model_client_gives_up_in_seconds_not_minutes():
    from callagent import llm

    client = llm.make_live_client(Settings(anthropic_api_key="x", call_model_read_timeout_seconds=12, call_model_connect_timeout_seconds=2))
    assert client.timeout.read == 12 and client.timeout.connect == 2 and client.max_retries == 1
