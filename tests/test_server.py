"""End-to-end smoke of the HTTP surface with a test client (no Twilio/LLM network calls)."""

import pytest
from fastapi.testclient import TestClient

from callagent import db
from callagent.config import get_settings, resolve_api_token


@pytest.fixture
def client(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.ngrok.app")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c
    get_settings.cache_clear()


def test_api_requires_token(client):
    assert client.get("/health").status_code == 200  # health is public
    assert client.get("/api/campaigns", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/campaigns", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/campaigns").status_code == 200


def test_campaign_lifecycle_and_twilio_webhooks(client, sample_playbook):
    assert client.get("/health").json()["ok"] is True

    r = client.post("/api/campaigns", json={"name": "smoke", "playbook": sample_playbook.model_dump(), "script_text": "s"})
    assert r.status_code == 200 and r.json()["status"] == "draft"  # blocking question open

    with open("examples/sample_leads.csv", "rb") as f:
        r = client.post("/api/campaigns/smoke/leads", files={"file": ("leads.csv", f, "text/csv")})
    assert r.status_code == 200 and r.json()["imported"] == 3

    r = client.post("/api/campaigns/smoke/start")
    assert r.status_code == 400  # not configured for live calls in tests

    with db.session_scope() as s:
        lead = s.query(db.Lead).order_by(db.Lead.id).first()
        lead.status = "calling"
        lead.attempts = 1
        run = db.CallRun(lead_id=lead.id, campaign_id=lead.campaign_id, status="initiated", twilio_call_sid="CA1")
        s.add(run)
        s.flush()
        rid = run.id

    # A human answered: we should get <Connect><Stream> TwiML carrying the call id and live mode.
    r = client.post(f"/twilio/voice?call_run_id={rid}", data={"AnsweredBy": "human", "CallSid": "CA1"})
    assert r.status_code == 200 and "<Stream" in r.text and "live" in r.text and str(rid) in r.text
    assert 'url="wss://example.ngrok.app/ws"' in r.text
    assert 'name="stream_token"' in r.text
    with db.session_scope() as s:
        token = s.get(db.CallRun, rid).stream_token
    assert token and token in r.text

    # Voicemail beep: stream in voicemail mode.
    r = client.post(f"/twilio/voice?call_run_id={rid}", data={"AnsweredBy": "machine_end_beep", "CallSid": "CA1"})
    assert "voicemail" in r.text

    # Fax: hang up.
    r = client.post(f"/twilio/voice?call_run_id={rid}", data={"AnsweredBy": "fax", "CallSid": "CA1"})
    assert "<Hangup" in r.text

    r = client.post(f"/twilio/status?call_run_id={rid}", data={"CallStatus": "no-answer", "CallSid": "CA1"})
    assert r.status_code == 200
    calls = client.get("/api/campaigns/smoke/calls").json()
    assert calls[0]["status"] == "no-answer" and calls[0]["disposition"] == "no_answer"

    r = client.get("/api/campaigns/smoke/export.csv")
    assert r.status_code == 200 and r.text.startswith("phone,first_name")
    assert "+14155550134" in r.text


def test_twilio_signature_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "true")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sig.db'}")
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'sig.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        r = c.post("/twilio/status?call_run_id=1", data={"CallStatus": "completed"})
        assert r.status_code == 403
    get_settings.cache_clear()
