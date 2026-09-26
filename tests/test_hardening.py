"""Regression tests for the adversarial-review findings."""

from datetime import timedelta

import pandas as pd
import pytest
from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame, TTSSpeakFrame, UserStoppedSpeakingFrame
from pipecat.tests.utils import SleepFrame, run_test

from callagent import db
from callagent.config import Settings
from callagent.dialer.campaign_runner import apply_twilio_status, reap_stale_calls
from callagent.leads.importer import map_columns
from callagent.server import authorize_stream
from callagent.voice.fillers import FillerProcessor
from callagent.voice.speech_filter import sanitize_speech


# ---- speech sanitizer -------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Sure, let me check that.", "Sure, let me check that."),
        ("<thinking>they seem busy</thinking>Totally, I'll be quick.", "Totally, I'll be quick."),
        ('{"name": "end_call", "input": {"reason": "declined"}}', ""),
        ("call end_call(reason='declined')", ""),
        ("**Great** question, *Maria*.", "Great question, Maria."),
        ("[laughs] That's fair.", "That's fair."),
        ("<function_calls><invoke name=\"book_meeting\">", ""),
        ("Two thirty works? <br> Perfect.", "Two thirty works? Perfect."),
    ],
)
def test_sanitize_speech(raw, expected):
    assert sanitize_speech(raw) == expected


# ---- filler must not speak on the opener or on silence nudges ---------------------------

@pytest.mark.asyncio
async def test_no_filler_before_the_prospect_has_spoken():
    proc = FillerProcessor(delay_secs=0.05)
    await run_test(
        proc,
        frames_to_send=[LLMFullResponseStartFrame(), SleepFrame(0.2), LLMTextFrame("Hi, is this Sam?")],
        expected_down_frames=[LLMFullResponseStartFrame, LLMTextFrame],
    )
    assert proc.filler_count == 0


@pytest.mark.asyncio
async def test_filler_after_a_real_turn_still_works():
    proc = FillerProcessor(delay_secs=0.05)
    down, _ = await run_test(
        proc,
        frames_to_send=[UserStoppedSpeakingFrame(), LLMFullResponseStartFrame(), SleepFrame(0.2), LLMTextFrame("So")],
        expected_down_frames=[UserStoppedSpeakingFrame, LLMFullResponseStartFrame, TTSSpeakFrame, LLMTextFrame],
    )
    assert proc.filler_count == 1


# ---- /ws handshake authorization --------------------------------------------------------

def test_media_stream_requires_matching_token_and_fresh_call(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'ws.db'}")
    with db.session_scope() as s:
        c = db.Campaign(name="ws", playbook_json={})
        s.add(c)
        s.flush()
        lead = db.Lead(campaign_id=c.id, phone_e164="+14155550100")
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress", twilio_call_sid="CA_real", stream_token="tok123")
        s.add(run)
        s.flush()
        rid = run.id
    with db.session_scope() as s:
        ok = {"call_run_id": str(rid), "stream_token": "tok123"}
        assert authorize_stream(s, ok, "CA_real") is None
        assert authorize_stream(s, {"call_run_id": str(rid)}, "CA_real") == "bad stream token"
        assert authorize_stream(s, {**ok, "stream_token": "nope"}, "CA_real") == "bad stream token"
        assert authorize_stream(s, ok, "CA_attacker") == "call sid mismatch"
        assert authorize_stream(s, {"call_run_id": "999", "stream_token": "tok123"}, "CA_real") == "unknown call_run_id"
        assert authorize_stream(s, {"stream_token": "tok123"}, "CA_real") == "missing call_run_id"
        run = s.get(db.CallRun, rid)
        run.stream_connected = True
        assert authorize_stream(s, ok, "CA_real") == "stream already connected for this call"
        run.stream_connected = False
        run.status = "completed"
        assert authorize_stream(s, ok, "CA_real") == "call already ended"


# ---- header matching --------------------------------------------------------------------

def test_header_matching_uses_whole_words():
    df = pd.DataFrame({"Hotel": ["x"], "Account Number": ["1"], "Last Contacted": ["2026"], "Phone Number": ["+14155550100"], "First Seen": ["2025"]})
    m = map_columns(df)
    assert m["phone"] == "Phone Number"
    assert "last_name" not in m and "first_name" not in m


# ---- dialer leftovers -------------------------------------------------------------------

def _settings(**kw):
    return Settings(anthropic_api_key="x", default_timezone="UTC", **kw)


def test_late_ringing_does_not_resurrect_a_finished_call(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'order.db'}")
    settings = _settings()
    with db.session_scope() as s:
        c = db.Campaign(name="o", playbook_json={})
        s.add(c)
        s.flush()
        lead = db.Lead(campaign_id=c.id, phone_e164="+14155550100", status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress", stream_connected=True)
        s.add(run)
        s.flush()
        rid = run.id
    with db.session_scope() as s:
        apply_twilio_status(s, rid, {"CallStatus": "completed"}, settings)
        apply_twilio_status(s, rid, {"CallStatus": "ringing"}, settings)
        assert s.get(db.CallRun, rid).status == "completed"


def test_reap_recovers_lead_stuck_in_calling_after_its_call_ended(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'reap.db'}")
    settings = _settings(max_attempts=3)
    with db.session_scope() as s:
        c = db.Campaign(name="r", playbook_json={})
        s.add(c)
        s.flush()
        lead = db.Lead(campaign_id=c.id, phone_e164="+14155550100", status="calling", attempts=1)
        s.add(lead)
        s.flush()
        s.add(db.CallRun(lead_id=lead.id, campaign_id=c.id, status="completed", stream_connected=True,
                         ended_at=db.utcnow() - timedelta(minutes=10)))
        s.flush()
        cid, lid = c.id, lead.id
    with db.session_scope() as s:
        assert reap_stale_calls(s, cid, settings) == 1
        lead = s.get(db.Lead, lid)
        assert lead.status == "pending" and lead.next_attempt_at is not None
