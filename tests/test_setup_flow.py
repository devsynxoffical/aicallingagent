"""Setting up a campaign from the console: status, the short form, languages, inbound, direction."""

import pytest
from fastapi.testclient import TestClient

from callagent import db
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.db import CallRun, Lead, session_scope
from callagent.playbook.brief import CampaignBrief, playbook_from_brief
from callagent.playbook.prompt_builder import build_system_prompt

BRIEF = {
    "name": "Spring outreach", "company_name": "Northwind Accounting", "company_description": "Bookkeeping for small firms.",
    "offer_summary": "Monthly bookkeeping with a fixed price.", "target_customer": "Firms with 3 to 30 staff.", "call_goal": "book_meeting",
    "agent_name": "Sam", "language": "en", "value_props": ["Fixed monthly price", "  "], "pricing_and_terms": "From $300 per month.",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for name in ("ANTHROPIC_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "PUBLIC_BASE_URL"):
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c
    get_settings.cache_clear()


# ----------------------------------------------------------------------------- status


def test_status_names_what_is_missing_and_never_shows_a_value(client, monkeypatch):
    status = client.get("/api/status").json()
    assert status["ready_for_calls"] is False and status["inbound_webhook"] is None
    assert {c["key"]: c["ok"] for c in status["checks"]} == {"model": False, "telephony": False, "stt": False, "tts": False, "public_url": False}
    assert next(c for c in status["checks"] if c["key"] == "telephony")["missing"] == ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"]
    assert [l["code"] for l in status["languages"]] == ["en", "ar", "fr"]

    secrets = {"ANTHROPIC_API_KEY": "sk-secret-model", "TWILIO_ACCOUNT_SID": "ACsecret", "TWILIO_AUTH_TOKEN": "twilio-secret", "TWILIO_FROM_NUMBER": "+14155550000",
               "DEEPGRAM_API_KEY": "dg-secret", "ELEVENLABS_API_KEY": "el-secret", "ELEVENLABS_VOICE_ID": "voice-1", "PUBLIC_BASE_URL": "https://engine.example.com/"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    response = client.get("/api/status")
    status = response.json()
    assert status["ready_for_calls"] is True and all(c["ok"] and c["missing"] == [] for c in status["checks"])
    assert status["inbound_webhook"] == "https://engine.example.com/twilio/inbound"
    assert status["from_number"] == "+14155550000"  # a phone number, not a credential
    for name in ("ANTHROPIC_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        assert secrets[name] not in response.text


# ----------------------------------------------------------------------------- the short form


def test_the_short_form_makes_a_campaign_that_is_ready_and_says_only_what_was_given(client):
    created = client.post("/api/campaign-briefs", json=BRIEF)
    assert created.status_code == 201, created.text
    assert (created.json()["name"], created.json()["status"], created.json()["languages"]) == ("Spring outreach", "ready", ["en"])

    version = client.get("/api/campaigns/Spring outreach/versions/1").json()
    assert (version["status"], version["source"]) == ("active", "brief")
    pb = version["playbook"]
    assert pb["proof_points"] == [] and pb["objections"] == []  # nothing invented
    assert pb["value_props"] == ["Fixed monthly price"]
    assert pb["pricing_and_terms"] == "From $300 per month."
    assert pb["disclose_ai_upfront"] is True and pb["disclose_ai_if_asked"] is True
    assert pb["opening_line"] == "Hi, is this {first_name}? This is Sam, calling from Northwind Accounting."
    assert "{callback_number}" in pb["voicemail_script"]
    assert {c["key"] for c in pb["required_capabilities"]} == {"book_meeting", "schedule_callback", "mark_do_not_call"}
    assert "has not reviewed" in pb["readiness_notes"] and pb["open_questions"] == []

    assert client.post("/api/campaign-briefs", json=BRIEF).status_code == 409  # never overwrites a campaign
    assert client.get("/api/campaigns/Spring outreach/versions").json()[0]["version"] == 1


@pytest.mark.parametrize("change", [{"name": "   "}, {"company_name": ""}, {"offer_summary": ""}, {"language": "de"}, {"additional_languages": ["xx"]}, {"call_goal": "spam"}])
def test_an_incomplete_or_unsupported_form_is_refused(client, change):
    assert client.post("/api/campaign-briefs", json={**BRIEF, **change}).status_code == 422
    assert client.get("/api/campaigns").json() == []


@pytest.mark.parametrize("language, opening", [("fr", "Bonjour, est-ce bien {first_name} ? Ici Sam, de la part de Northwind Accounting."), ("ar", "مرحباً، هل أتحدث مع {first_name}؟ معك Sam من Northwind Accounting.")])
def test_a_campaign_can_open_in_french_or_arabic_and_the_prompt_can_be_built(language, opening):
    pb = playbook_from_brief(CampaignBrief(**{**BRIEF, "language": language, "additional_languages": ["en", language]}))
    assert pb.opening_line == opening
    assert pb.languages == [language, "en"]  # the opening language is not repeated
    lead = {"first_name": "Sam", "last_name": "Taylor", "company": "Taylor & Co", "timezone": "UTC"}
    for mode in ("live", "voicemail", "inbound"):
        assert "Northwind Accounting" in build_system_prompt(pb, lead, mode=mode, callback_number="+14155550000")


def test_only_an_admin_creates_campaigns_and_changes_languages(client):
    client.post("/api/campaign-briefs", json=BRIEF)
    client.post("/api/users", json={"email": "op@example.com", "role": "operator", "password": "correct horse battery"})
    token = client.post("/api/auth/login", json={"email": "op@example.com", "password": "correct horse battery"}, headers={"Authorization": ""}).json()["token"]
    operator = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/campaign-briefs", json={**BRIEF, "name": "Another"}, headers=operator).status_code == 403
    assert client.put("/api/campaigns/Spring outreach/languages", json={"languages": ["en", "fr"]}, headers=operator).status_code == 403
    assert client.post("/api/campaigns/Spring outreach/inbound", params={"number": "+14155550123"}, headers=operator).status_code == 403
    assert client.get("/api/campaigns/Spring outreach/languages", headers=operator).status_code == 200
    assert client.get("/api/status", headers=operator).status_code == 200


# ----------------------------------------------------------------------------- languages


def test_languages_are_set_opening_language_first_and_recorded_as_a_version(client):
    client.post("/api/campaign-briefs", json=BRIEF)
    before = client.get("/api/campaigns/Spring outreach/languages").json()
    assert (before["languages"], before["localized"], before["can_localize"]) == (["en"], [], False)

    changed = client.put("/api/campaigns/Spring outreach/languages", json={"languages": ["ar", "en-US", "fr", "ar"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["languages"] == ["ar", "en", "fr"]
    assert client.get("/api/campaigns/Spring outreach").json()["languages"] == ["ar", "en", "fr"]
    versions = client.get("/api/campaigns/Spring outreach/versions").json()
    assert [(v["version"], v["status"], v["source"]) for v in versions[:2]] == [(2, "active", "languages"), (1, "archived", "brief")]

    assert client.put("/api/campaigns/Spring outreach/languages", json={"languages": ["en"]}).json()["languages"] == ["en"]  # back to one
    for bad in ({"languages": []}, {"languages": ["de"]}):
        assert client.put("/api/campaigns/Spring outreach/languages", json=bad).status_code in (400, 422)
    # Native phrasing is written by the AI model: without its key the engine says so and changes nothing.
    refused = client.put("/api/campaigns/Spring outreach/languages", json={"languages": ["en", "fr"], "localize": True})
    assert refused.status_code == 400 and "ANTHROPIC_API_KEY is not set" in refused.json()["detail"]
    assert client.get("/api/campaigns/Spring outreach/languages").json()["languages"] == ["en"]


# ----------------------------------------------------------------------------- inbound and outbound


def test_an_inbound_call_is_turned_away_politely_when_the_engine_has_no_public_address(client):
    client.post("/api/campaign-briefs", json=BRIEF)
    client.post("/api/campaigns/Spring outreach/inbound", params={"number": "+14155550123"})
    answer = client.post("/twilio/inbound", data={"CallSid": "CAin0", "From": "+14155550777", "To": "+14155550123"})
    assert answer.status_code == 200 and "<Say" in answer.text and "<Stream" not in answer.text
    assert client.get("/api/campaigns/Spring outreach/calls").json() == []  # no half-open call is left behind


def test_a_number_is_answered_by_one_campaign_and_calls_are_told_apart_by_direction(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://engine.example.com")
    get_settings.cache_clear()
    client.post("/api/campaign-briefs", json=BRIEF)
    client.post("/api/campaign-briefs", json={**BRIEF, "name": "Second"})
    set_number = client.post("/api/campaigns/Spring outreach/inbound", params={"number": "(415) 555-0123"})
    assert set_number.status_code == 200 and set_number.json()["inbound_number"] == "+14155550123"
    assert client.post("/api/campaigns/Second/inbound", params={"number": "+14155550123"}).status_code == 409
    assert client.post("/api/campaigns/Second/inbound", params={"number": "not a number"}).status_code == 400

    # A prospect calls the number: the agent answers with this campaign's playbook.
    answered = client.post("/twilio/inbound", data={"CallSid": "CAin1", "From": "+14155550777", "To": "+14155550123"})
    assert answered.status_code == 200 and "<Stream" in answered.text
    with session_scope() as s:
        campaign_id = db.get_campaign_by_name(s, "Spring outreach").id
        lead = Lead(campaign_id=campaign_id, phone_e164="+14155550888", status="completed")
        s.add(lead)
        s.flush()
        s.add(CallRun(lead_id=lead.id, campaign_id=campaign_id, status="completed", direction="outbound", mode="live", stream_connected=True,
                      transcript=[{"role": "user", "text": "Yes, book it."}], disposition="meeting_booked"))

    everything = client.get("/api/campaigns/Spring outreach/calls").json()
    assert sorted(call["direction"] for call in everything) == ["inbound", "outbound"]
    assert [c["direction"] for c in client.get("/api/campaigns/Spring outreach/calls", params={"direction": "inbound"}).json()] == ["inbound"]
    assert [c["direction"] for c in client.get("/api/campaigns/Spring outreach/calls", params={"direction": "outbound"}).json()] == ["outbound"]

    by_direction = client.get("/api/campaigns/Spring outreach/report").json()["by_direction"]
    assert (by_direction["outbound"]["calls"], by_direction["outbound"]["conversions"]) == (1, 1)
    assert (by_direction["inbound"]["calls"], by_direction["inbound"]["conversions"]) == (1, 0)

    # Stop answering: the number is free again, and a call to it is turned away politely.
    assert client.post("/api/campaigns/Spring outreach/inbound").json()["inbound_number"] is None
    turned_away = client.post("/twilio/inbound", data={"CallSid": "CAin2", "From": "+14155550777", "To": "+14155550123"})
    assert "<Stream" not in turned_away.text and "<Say" in turned_away.text
