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
