"""Per-campaign calling rules: days and hours, attempts, daily caps, budgets and stop conditions."""

from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from callagent import db
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.dialer import rules
from callagent.dialer.campaign_runner import CampaignRunner, apply_twilio_status, in_calling_window, next_window_open, pick_next_lead

UTC = timezone.utc


def _settings(**kw) -> Settings:
    base = dict(anthropic_api_key="x", default_timezone="UTC", calling_window_start="00:00", calling_window_end="23:59")
    return Settings(**{**base, **kw})


def _campaign(tmp_path, name="c", **rule_values) -> int:
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / (name + '.db')}")
    with db.session_scope() as s:
        c = db.Campaign(name=name, playbook_json={}, status="running", rules_json=rules.CampaignRules(**rule_values).model_dump(mode="json", exclude_none=True))
        s.add(c)
        s.flush()
        return c.id


def _call(s, cid, *, cost=None, disposition=None, answered=False, started=None, lead_phone="+14155550100"):
    lead = db.Lead(campaign_id=cid, phone_e164=lead_phone, status="completed")
    s.add(lead)
    s.flush()
    run = db.CallRun(lead_id=lead.id, campaign_id=cid, status="completed", cost_usd=cost, disposition=disposition,
                     stream_connected=answered, started_at=started or db.utcnow(), ended_at=db.utcnow())
    s.add(run)
    s.flush()
    return run


def test_rules_are_validated():
    assert rules.parse_days("mon-fri") == [0, 1, 2, 3, 4] and rules.parse_days("sat-mon") == [0, 5, 6] and rules.parse_days("Tue, thu") == [1, 3]
    with pytest.raises(ValueError):
        rules.parse_days("someday")
    for bad in (
        dict(window_start="18:00", window_end="09:00"),
        dict(window_start="9am"),
        dict(calling_days=[7]),
        dict(budget_usd=0),
        dict(max_attempts=0),
        dict(max_calls_per_day=500, max_calls_per_month=100),
        dict(starts_at=datetime(2026, 12, 1, 9)),  # no timezone
        dict(starts_at=datetime(2026, 12, 2, tzinfo=UTC), ends_at=datetime(2026, 12, 1, tzinfo=UTC)),
        dict(max_opt_out_rate=1.5),
    ):
        with pytest.raises(ValidationError):
            rules.CampaignRules(**bad)


def test_a_campaign_without_rules_behaves_like_the_global_settings(tmp_path):
    cid = _campaign(tmp_path)
    settings = _settings(max_attempts=3, retry_delay_minutes=240)
    with db.session_scope() as s:
        assert rules.campaign_settings(s, cid, settings) is settings
        assert rules.calling_days(s.get(db.Campaign, cid)) == (0, 1, 2, 3, 4, 5)
        assert rules.check_limits(s, s.get(db.Campaign, cid), settings, 0).action == "dial"
        assert rules.describe(s.get(db.Campaign, cid), settings)["max_attempts"] == 3


def test_calling_days_and_hours_follow_the_campaign(tmp_path):
    monday_10 = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    saturday_10 = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    sunday_10 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    nine, five = time(9), time(17)
    assert in_calling_window(saturday_10, "UTC", nine, five, "UTC") is True  # default: Monday to Saturday
    assert in_calling_window(sunday_10, "UTC", nine, five, "UTC") is False
    weekdays = (0, 1, 2, 3, 4)
    assert in_calling_window(saturday_10, "UTC", nine, five, "UTC", weekdays) is False
    assert in_calling_window(monday_10, "UTC", nine, five, "UTC", weekdays) is True
    assert in_calling_window(sunday_10, "UTC", nine, five, "UTC", (6,)) is True
    # From Friday evening a weekdays-only campaign next opens Monday morning, not Saturday.
    friday_evening = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    assert next_window_open(friday_evening, "UTC", nine, "UTC", weekdays) == datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    assert next_window_open(friday_evening, "UTC", nine, "UTC") == datetime(2026, 11, 21, 9, 0, tzinfo=UTC)

    cid = _campaign(tmp_path, window_start="10:00", window_end="12:00", max_attempts=5, retry_delay_minutes=30)
    with db.session_scope() as s:
        effective = rules.campaign_settings(s, cid, _settings(max_attempts=3))
        assert (effective.max_attempts, effective.retry_delay_minutes) == (5, 30)
        assert (effective.calling_window_start, effective.calling_window_end) == (time(10), time(12))


def test_campaign_attempt_limit_and_retry_delay_drive_the_dialer(tmp_path):
    cid = _campaign(tmp_path, max_attempts=1, retry_delay_minutes=15)
    settings = _settings(max_attempts=3, retry_delay_minutes=240)
    with db.session_scope() as s:
        lead = db.Lead(campaign_id=cid, phone_e164="+14155550100", status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=cid, status="initiated")
        s.add(run)
        s.flush()
        apply_twilio_status(s, run.id, {"CallStatus": "no-answer"}, settings)
        assert lead.status == "completed"  # the campaign allows one attempt; the global three do not apply
        retry = db.Lead(campaign_id=cid, phone_e164="+14155550101", status="pending", attempts=1)
        s.add(retry)
        s.flush()
        assert pick_next_lead(s, cid, settings) is None  # already at the campaign's limit


def test_higher_priority_leads_are_dialed_first(tmp_path):
    cid = _campaign(tmp_path)
    with db.session_scope() as s:
        s.add(db.Lead(campaign_id=cid, phone_e164="+14155550101", timezone="UTC"))
        s.add(db.Lead(campaign_id=cid, phone_e164="+14155550102", timezone="UTC", priority=5))
        s.add(db.Lead(campaign_id=cid, phone_e164="+14155550103", timezone="UTC", status="callback", next_attempt_at=db.utcnow() - timedelta(minutes=1)))
        s.flush()
        assert pick_next_lead(s, cid, _settings()).phone_e164 == "+14155550103"  # a requested callback still goes first
        s.query(db.Lead).filter_by(phone_e164="+14155550103").delete()
        assert pick_next_lead(s, cid, _settings()).phone_e164 == "+14155550102"


def test_call_cost_is_billed_by_the_started_minute(tmp_path):
    settings = _settings(cost_per_connected_minute_usd=0.10, cost_per_call_attempt_usd=0.01)
    assert rules.call_cost(0, settings) == 0.01 and rules.call_cost(None, settings) == 0.01  # an unanswered attempt
    assert rules.call_cost(1, settings) == 0.11 and rules.call_cost(60, settings) == 0.11 and rules.call_cost(61, settings) == 0.21
    cid = _campaign(tmp_path)
    with db.session_scope() as s:
        lead = db.Lead(campaign_id=cid, phone_e164="+14155550100", status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=cid, status="in-progress", stream_connected=True)
        s.add(run)
        s.flush()
        apply_twilio_status(s, run.id, {"CallStatus": "completed", "CallDuration": "185"}, settings)
        assert run.cost_usd == pytest.approx(0.41)  # 4 started minutes + the attempt
        assert rules.campaign_spend(s, cid) == pytest.approx(0.41)


def test_budget_stops_the_campaign_before_it_can_be_overshot(tmp_path):
    cid = _campaign(tmp_path, budget_usd=10.0)
    settings = _settings(cost_per_connected_minute_usd=0.20, call_time_limit_seconds=600)  # worst case $2.00 a call
    with db.session_scope() as s:
        c = s.get(db.Campaign, cid)
        assert rules.check_limits(s, c, settings, 0).action == "dial"
        _call(s, cid, cost=5.00)
        assert rules.check_limits(s, c, settings, 0).action == "dial"  # 5 spent + 2 worst case fits
        # Two calls in progress could each still cost $2: 5 + 3 x 2 = 11 > 10, so wait for them.
        waiting = rules.check_limits(s, c, settings, 2)
        assert waiting.action == "wait" and "in progress" in waiting.reason
        _call(s, cid, cost=3.50, lead_phone="+14155550101")
        stopped = rules.check_limits(s, c, settings, 0)  # 8.50 spent; one more worst-case call would exceed
        assert stopped.action == "stop" and "does not cover one more call" in stopped.reason
        _call(s, cid, cost=1.50, lead_phone="+14155550102")
        reached = rules.check_limits(s, c, settings, 0)
        assert reached.action == "stop" and "budget of $10.00 was reached (spent $10.00)" in reached.reason


def test_a_budget_needs_a_configured_call_cost_to_start(tmp_path):
    cid = _campaign(tmp_path, budget_usd=50.0)
    with db.session_scope() as s:
        c = s.get(db.Campaign, cid)
        assert "COST_PER_CONNECTED_MINUTE_USD" in rules.start_problems(c, _settings())[0]
        assert rules.start_problems(c, _settings(cost_per_connected_minute_usd=0.12)) == []
        c.rules_json = {"ends_at": "2020-01-01T00:00:00+00:00"}
        assert "already passed" in rules.start_problems(c, _settings())[0]


def test_daily_and_monthly_caps_pause_until_the_next_period(tmp_path):
    cid = _campaign(tmp_path, max_calls_per_day=2, max_calls_per_month=3)
    settings = _settings()
    now = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    with db.session_scope() as s:
        c = s.get(db.Campaign, cid)
        _call(s, cid, started=now - timedelta(days=1), lead_phone="+14155550101")  # yesterday
        _call(s, cid, started=now - timedelta(hours=2), lead_phone="+14155550102")
        assert rules.check_limits(s, c, settings, 0, now).action == "dial"
        _call(s, cid, started=now - timedelta(hours=1), lead_phone="+14155550103")
        today = rules.check_limits(s, c, settings, 0, now)
        assert today.action == "wait" and "today's limit of 2" in today.reason  # waits, does not end the campaign
        tomorrow = rules.check_limits(s, c, settings, 0, now + timedelta(days=1))
        assert tomorrow.action == "wait" and "this month's limit of 3" in tomorrow.reason
        assert rules.check_limits(s, c, settings, 0, datetime(2026, 12, 1, 10, 0, tzinfo=UTC)).action == "dial"
        # Inbound calls do not count against the outbound caps.
        s.query(db.CallRun).update({"direction": "inbound"})
        assert rules.check_limits(s, c, settings, 0, now).action == "dial"


def test_schedule_meeting_target_and_opt_out_rate_stop_the_campaign(tmp_path):
    now = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    cid = _campaign(tmp_path, starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=30))
    settings = _settings()
    with db.session_scope() as s:
        c = s.get(db.Campaign, cid)
        assert rules.check_limits(s, c, settings, 0, now).action == "wait"
        assert rules.check_limits(s, c, settings, 0, now + timedelta(days=2)).action == "dial"
        ended = rules.check_limits(s, c, settings, 0, now + timedelta(days=31))
        assert ended.action == "stop" and "end date" in ended.reason

        c.rules_json = {"stop_after_meetings": 2}
        for i in range(2):
            run = _call(s, cid, lead_phone=f"+1415555020{i}", answered=True, disposition="meeting_booked")
            s.add(db.Appointment(lead_id=run.lead_id, call_run_id=run.id, starts_at="2026-12-01T10:00:00+00:00"))
            s.flush()
            verdict = rules.check_limits(s, c, settings, 0, now)
            assert verdict.action == ("dial" if i == 0 else "stop")
        assert "2 booked meetings" in verdict.reason

        c.rules_json = {"max_opt_out_rate": 0.10, "opt_out_min_calls": 10}
        for i in range(7):
            _call(s, cid, lead_phone=f"+1415555030{i}", answered=True, disposition="do_not_call" if i < 2 else "not_interested")
        assert rules.check_limits(s, c, settings, 0, now).action == "dial"  # 9 answered: too few to judge
        _call(s, cid, lead_phone="+14155550399", answered=True, disposition="not_interested")
        verdict = rules.check_limits(s, c, settings, 0, now)  # 2 of 10 = 20% > 10%
        assert verdict.action == "stop" and "2 of 10 answered calls asked not to be called again" in verdict.reason


def test_the_dialer_stops_itself_and_records_why(tmp_path):
    import asyncio

    cid = _campaign(tmp_path, budget_usd=1.0)
    settings = _settings(cost_per_connected_minute_usd=0.50, call_time_limit_seconds=60)
    with db.session_scope() as s:
        s.add(db.Lead(campaign_id=cid, phone_e164="+14155550150", timezone="UTC"))
        _call(s, cid, cost=1.00)
    runner = CampaignRunner(settings)
    placed = []

    async def never_dial(*args):
        placed.append(args)

    runner.dial_lead = never_dial

    async def go():
        await runner.start(cid)
        await asyncio.wait_for(runner._tasks[cid], timeout=5)

    asyncio.run(go())
    with db.session_scope() as s:
        c = s.get(db.Campaign, cid)
        assert c.status == "paused" and "budget of $1.00 was reached" in c.stop_reason
    assert placed == [] and not runner.is_running(cid)

    async def restart():  # the owner raises the budget and restarts: the old reason is cleared
        await runner.start(cid)
        await runner.stop(cid)

    with db.session_scope() as s:
        s.get(db.Campaign, cid).rules_json = {"budget_usd": 100.0}
    asyncio.run(restart())
    with db.session_scope() as s:
        assert s.get(db.Campaign, cid).stop_reason is None


@pytest.fixture
def client(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        c.post("/api/campaigns", json={"name": "q4", "playbook": sample_playbook.model_copy(update={"open_questions": []}).model_dump()})
        yield c
    get_settings.cache_clear()


def test_rules_api_shows_what_is_in_force_and_rejects_bad_values(client):
    shown = client.get("/api/campaigns/q4/rules").json()
    assert shown["stored"] == {} and shown["rules"]["calling_days"] == ["mon", "tue", "wed", "thu", "fri", "sat"]
    assert shown["rules"]["max_attempts"] == 3 and shown["rules"]["budget_usd"] is None and shown["spend_usd"] == 0

    saved = client.put("/api/campaigns/q4/rules", json={"calling_days": [0, 1, 2, 3, 4], "window_start": "09:30", "window_end": "17:00",
                                                         "max_calls_per_day": 200, "budget_usd": 250, "max_attempts": 2})
    assert saved.status_code == 200
    r = saved.json()["rules"]
    assert r["calling_days"] == ["mon", "tue", "wed", "thu", "fri"] and (r["window_start"], r["window_end"]) == ("09:30", "17:00")
    assert (r["max_calls_per_day"], r["budget_usd"], r["max_attempts"]) == (200, 250, 2)
    assert saved.json()["stored"]["budget_usd"] == 250
    listed = client.get("/api/campaigns/q4").json()
    assert listed["budget_usd"] == 250 and listed["spend_usd"] == 0 and listed["stop_reason"] is None

    assert client.put("/api/campaigns/q4/rules", json={"window_start": "18:00", "window_end": "09:00"}).status_code == 422
    assert client.put("/api/campaigns/q4/rules", json={"budget_usd": -5}).status_code == 422
    assert client.get("/api/campaigns/q4/rules").json()["stored"]["budget_usd"] == 250  # a rejected change changes nothing
    assert client.put("/api/campaigns/q4/rules", json={}).json()["stored"] == {}  # cleared back to defaults
