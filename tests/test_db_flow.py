from datetime import timedelta

from callagent import db
from callagent.config import Settings
from callagent.dialer.campaign_runner import apply_twilio_status, pick_next_lead


def _settings(**kw) -> Settings:
    return Settings(anthropic_api_key="x", default_timezone="UTC", **kw)


def test_pick_next_lead_and_no_answer_retry(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 't.db'}")
    settings = _settings(calling_window_start="00:00", calling_window_end="23:59", max_attempts=2, retry_delay_minutes=60)
    with db.session_scope() as s:
        c = db.Campaign(name="t", playbook_json={}, status="running")
        s.add(c)
        s.flush()
        # The DNC lead comes first so the picker must skip over it.
        s.add(db.DoNotCall(phone_e164="+14155550199"))
        s.add(db.Lead(campaign_id=c.id, phone_e164="+14155550199", timezone="UTC"))
        s.flush()
        s.add(db.Lead(campaign_id=c.id, phone_e164="+14155550100", timezone="UTC"))
        s.flush()
        cid = c.id

    with db.session_scope() as s:
        lead = pick_next_lead(s, cid, settings)
        assert lead is not None and lead.phone_e164 == "+14155550100"
        dnc_lead = [l for l in s.query(db.Lead).all() if l.phone_e164 == "+14155550199"][0]
        assert dnc_lead.status == "dnc"
        lead.status = "calling"
        lead.attempts = 1
        run = db.CallRun(lead_id=lead.id, campaign_id=cid, status="initiated")
        s.add(run)
        s.flush()
        run_id = run.id

    with db.session_scope() as s:
        apply_twilio_status(s, run_id, {"CallStatus": "no-answer", "CallSid": "CA123"}, settings)
        run = s.get(db.CallRun, run_id)
        lead = s.get(db.Lead, run.lead_id)
        assert run.status == "no-answer" and run.disposition == "no_answer"
        assert lead.status == "pending"
        assert lead.next_attempt_at is not None
        assert lead.next_attempt_at - db.utcnow() > timedelta(minutes=55)


def test_datetimes_come_back_timezone_aware_from_sqlite(tmp_path):
    """SQLite drops tzinfo; our TZDateTime restores it so aware/naive comparisons never crash."""
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'tz.db'}")
    with db.session_scope() as s:
        c = db.Campaign(name="tz", playbook_json={})
        s.add(c)
        s.flush()
        s.add(db.Lead(campaign_id=c.id, phone_e164="+14155550100", next_attempt_at=db.utcnow() + timedelta(hours=2)))
    with db.session_scope() as s:
        lead = s.query(db.Lead).one()
        assert lead.next_attempt_at.tzinfo is not None
        assert lead.next_attempt_at > db.utcnow()  # would raise TypeError with naive datetimes
        assert (lead.next_attempt_at - db.utcnow()) > timedelta(hours=1, minutes=55)


def test_requested_callback_is_dialed_even_after_max_attempts_and_goes_first(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'cb.db'}")
    settings = _settings(calling_window_start="00:00", calling_window_end="23:59", max_attempts=2)
    with db.session_scope() as s:
        c = db.Campaign(name="cb", playbook_json={}, status="running")
        s.add(c)
        s.flush()
        s.add(db.Lead(campaign_id=c.id, phone_e164="+14155550101", timezone="UTC", status="pending", attempts=0))
        s.add(db.Lead(campaign_id=c.id, phone_e164="+14155550102", timezone="UTC", status="callback", attempts=2,
                      next_attempt_at=db.utcnow() - timedelta(minutes=1)))
        s.flush()
        cid = c.id
    with db.session_scope() as s:
        lead = pick_next_lead(s, cid, settings)
        assert lead.phone_e164 == "+14155550102"


def test_status_callback_defers_to_finalization_when_stream_connected(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'sc.db'}")
    settings = _settings()
    with db.session_scope() as s:
        c = db.Campaign(name="sc", playbook_json={}, status="running")
        s.add(c)
        s.flush()
        lead = db.Lead(campaign_id=c.id, phone_e164="+14155550103", status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress", stream_connected=True)
        s.add(run)
        s.flush()
        rid = run.id
    with db.session_scope() as s:
        apply_twilio_status(s, rid, {"CallStatus": "completed", "CallDuration": "95"}, settings)
        run = s.get(db.CallRun, rid)
        assert run.status == "completed" and run.duration_seconds == 95
        assert run.disposition is None  # the post-call review decides
        assert s.get(db.Lead, run.lead_id).status == "calling"  # finalize_call will move it


def test_saving_a_ready_playbook_marks_new_campaign_ready(tmp_path, monkeypatch, sample_playbook):
    from callagent.config import get_settings
    from callagent import cli

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'ready.db'}")
    draft = cli._save_campaign("c1", sample_playbook, "script")
    assert draft.status == "draft"  # blocking question open
    sample_playbook.open_questions = []
    ready = cli._save_campaign("c2", sample_playbook, "script")
    assert ready.status == "ready"
    get_settings.cache_clear()
