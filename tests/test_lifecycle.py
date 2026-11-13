"""The call lifecycle, the usage ledger, and reconciliation with the provider."""

import asyncio
from datetime import timedelta
from itertools import product
from types import SimpleNamespace

import pytest

from callagent import db
from callagent.config import Settings
from callagent.db import ACTIVE_CALL_STATUSES, TERMINAL_CALL_STATUSES, UsageRecord, utcnow
from callagent.dialer import lifecycle, twilio_client
from callagent.dialer.campaign_runner import CampaignRunner, apply_twilio_status, claim_lead_for_call
from callagent.dialer.reconcile import reconcile_calls
from callagent.training import versions

SETTINGS = Settings(anthropic_api_key="x", twilio_account_sid="AC1", twilio_auth_token="secret", twilio_from_number="+14155550000",
                    public_base_url="https://example.test", cost_per_connected_minute_usd=0.10, cost_per_call_attempt_usd=0.02, max_attempts=3)


@pytest.fixture
def campaign(tmp_path, sample_playbook):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'life.db'}")
    with db.session_scope() as s:
        c = db.Campaign(name="q4", status="paused")  # paused on purpose: loose ends are tied up whatever the campaign's state
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        return c.id


def _call(cid, *, status="ringing", sid="CA1", minutes_ago=30, phone="+14155550100") -> SimpleNamespace:
    with db.session_scope() as s:
        lead = db.Lead(campaign_id=cid, phone_e164=phone, status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=cid, status=status, twilio_call_sid=sid, started_at=utcnow() - timedelta(minutes=minutes_ago))
        s.add(run)
        s.flush()
        return SimpleNamespace(run=run.id, lead=lead.id)


def _state(ids):
    with db.session_scope() as s:
        run, lead = s.get(db.CallRun, ids.run), s.get(db.Lead, ids.lead)
        return SimpleNamespace(status=run.status, source=run.status_source, sid=run.twilio_call_sid, error=run.error, duration=run.duration_seconds,
                               cost=run.cost_usd, lead=lead.status, next_attempt=lead.next_attempt_at)


# ----------------------------------------------------------------------------- state machine


def test_every_state_and_event_pair_follows_the_lifecycle():
    order = list(ACTIVE_CALL_STATUSES)
    for current, new in product([*ACTIVE_CALL_STATUSES, *TERMINAL_CALL_STATUSES], repeat=2):
        for source, current_source in product((lifecycle.SYSTEM, lifecycle.PROVIDER), repeat=2):
            allowed = lifecycle.can_advance(current, new, source=source, current_source=current_source)
            if current == new:
                expected = False  # a repeated report changes nothing
            elif current in TERMINAL_CALL_STATUSES:
                # final, unless the provider corrects an ending we inferred ourselves
                expected = new in TERMINAL_CALL_STATUSES and source == lifecycle.PROVIDER and current_source == lifecycle.SYSTEM
            elif new in TERMINAL_CALL_STATUSES:
                expected = True  # any call can end
            else:
                expected = order.index(new) > order.index(current)  # only forward
            assert allowed is expected, (current, new, source, current_source)
    assert not lifecycle.can_advance("ringing", "teleported", source=lifecycle.PROVIDER, current_source=None)


def test_webhooks_out_of_order_late_or_repeated_leave_the_call_where_it_belongs(campaign):
    ids = _call(campaign, status="created", minutes_ago=0)
    for status in ("ringing", "initiated", "in-progress", "ringing", "completed", "in-progress", "ringing", "completed"):
        with db.session_scope() as s:
            apply_twilio_status(s, ids.run, {"CallStatus": status, "CallDuration": "125" if status == "completed" else ""}, SETTINGS)
    state = _state(ids)
    assert (state.status, state.source, state.duration) == ("completed", "provider", 125)


def test_the_provider_corrects_an_ending_we_only_inferred(campaign):
    ids = _call(campaign, status="in-progress")
    with db.session_scope() as s:
        assert lifecycle.advance(s.get(db.CallRun, ids.run), "failed")  # our pipeline gave up on it
    with db.session_scope() as s:
        apply_twilio_status(s, ids.run, {"CallStatus": "completed", "CallDuration": "61"}, SETTINGS)
    assert (_state(ids).status, _state(ids).source) == ("completed", "provider")
    with db.session_scope() as s:  # but nothing overrules the provider afterwards
        assert not lifecycle.advance(s.get(db.CallRun, ids.run), "failed")
        apply_twilio_status(s, ids.run, {"CallStatus": "busy"}, SETTINGS)
    assert _state(ids).status == "completed"


# ----------------------------------------------------------------------------- usage ledger


def test_the_final_status_reported_any_number_of_times_is_one_usage_row(campaign):
    ids = _call(campaign, status="in-progress")
    for _ in range(5):
        with db.session_scope() as s:
            apply_twilio_status(s, ids.run, {"CallStatus": "completed", "CallDuration": "125"}, SETTINGS)
    with db.session_scope() as s:  # a replay that disagrees with the first report does not rewrite history
        apply_twilio_status(s, ids.run, {"CallStatus": "completed", "CallDuration": "999"}, SETTINGS)
    with db.session_scope() as s:
        record = s.query(UsageRecord).one()
        assert (record.call_run_id, record.kind, record.quantity_seconds) == (ids.run, "call", 125)
        assert str(record.amount_usd) == "0.3200"  # 0.02 per attempt + 3 started minutes at 0.10, as an exact decimal
    state = _state(ids)
    assert (state.duration, state.cost) == (125, pytest.approx(0.32))


def test_usage_across_calls_adds_up_exactly(campaign):
    from decimal import Decimal

    for i in range(30):
        ids = _call(campaign, status="in-progress", sid=f"CA{i}", phone=f"+1415555{i:04d}")
        with db.session_scope() as s:
            apply_twilio_status(s, ids.run, {"CallStatus": "completed", "CallDuration": "61"}, SETTINGS)
            apply_twilio_status(s, ids.run, {"CallStatus": "completed", "CallDuration": "61"}, SETTINGS)
    with db.session_scope() as s:
        records = s.query(UsageRecord).all()
        assert len(records) == 30 and sum(r.amount_usd for r in records) == Decimal("6.6000")  # 30 x 0.22, no rounding drift


# ----------------------------------------------------------------------------- reconciliation


class Provider:
    """What the telephony provider would answer, for calls keyed by their id or number."""

    def __init__(self, by_sid=None, by_number=None, fail=False):
        self.by_sid, self.by_number, self.fail, self.asked = by_sid or {}, by_number or {}, fail, []

    def fetch_call(self, sid, settings):
        self.asked.append(sid)
        if self.fail:
            raise TimeoutError("no answer from the provider")
        return self.by_sid.get(sid)

    def find_call(self, phone, started_after, settings):
        self.asked.append(phone)
        if self.fail:
            raise TimeoutError("no answer from the provider")
        return self.by_number.get(phone)


def _reconcile(provider):
    return asyncio.run(reconcile_calls(SETTINGS, provider))


def test_a_quiet_call_that_is_still_in_progress_at_the_provider_is_left_alone(campaign):
    ids = _call(campaign, status="ringing", minutes_ago=30)
    assert _reconcile(Provider(by_sid={"CA1": {"sid": "CA1", "status": "in-progress", "duration": None}})) == {"still active": 1}
    state = _state(ids)
    assert (state.status, state.lead) == ("in-progress", "calling")  # nobody redials this person


def test_a_quiet_call_that_ended_at_the_provider_gets_its_real_ending(campaign):
    ids = _call(campaign, status="ringing", minutes_ago=30)  # the status webhook was lost
    assert _reconcile(Provider(by_sid={"CA1": {"sid": "CA1", "status": "no-answer", "duration": "0"}})) == {"ended": 1}
    state = _state(ids)
    assert (state.status, state.source, state.lead) == ("no-answer", "provider", "pending") and state.next_attempt > utcnow()
    with db.session_scope() as s:
        assert s.query(UsageRecord).count() == 1
    assert _reconcile(Provider()) == {}  # resolved: nothing more to ask about


def test_when_the_provider_cannot_be_reached_nothing_is_guessed(campaign):
    ids = _call(campaign, status="ringing", minutes_ago=30)
    provider = Provider(fail=True)
    assert _reconcile(provider) == {"provider unreachable": 1} and provider.asked == ["CA1"]
    state = _state(ids)
    assert (state.status, state.lead) == ("ringing", "calling")  # untouched, and still not dialable
    with pytest.raises(Exception), db.session_scope() as s:
        claim_lead_for_call(s, campaign, ids.lead, manual=True)


def test_a_call_the_provider_has_never_heard_of_is_closed_and_its_lead_released(campaign):
    ids = _call(campaign, status="created", sid=None, minutes_ago=10)  # the create request never reached the provider
    assert _reconcile(Provider()) == {"no record": 1}
    state = _state(ids)
    assert (state.status, state.source, state.lead) == ("failed", "system", "pending") and "no record" in state.error


def test_a_recent_call_is_not_asked_about_yet(campaign):
    _call(campaign, status="ringing", minutes_ago=1)
    _call(campaign, status="created", sid=None, minutes_ago=1, phone="+14155550101")
    _call(campaign, status="in-progress", sid="CA9", minutes_ago=8, phone="+14155550102")  # a normal conversation
    provider = Provider()
    assert _reconcile(provider) == {} and provider.asked == []


def test_a_create_request_that_timed_out_but_succeeded_is_adopted_not_redialed(campaign, monkeypatch):
    with db.session_scope() as s:
        lead = db.Lead(campaign_id=campaign, phone_e164="+14155550100")
        s.add(lead)
        s.flush()
        lead_id = lead.id

    async def timed_out(run_id, phone, settings):
        raise TimeoutError("the provider did not answer in time")  # ... but it did place the call

    monkeypatch.setattr(twilio_client, "place_call_async", timed_out)
    runner = CampaignRunner(SETTINGS)
    with pytest.raises(TimeoutError):
        asyncio.run(runner.dial_lead(campaign, lead_id))
    with db.session_scope() as s:
        run = s.query(db.CallRun).one()
        assert run.status == "created" and "did not confirm" in run.error
        assert s.get(db.Lead, lead_id).status == "calling"  # still reserved: the dialer cannot pick this person again
        run.started_at = utcnow() - timedelta(minutes=3)
        run_id = run.id

    found = {"sid": "CAreal", "status": "in-progress", "duration": None}
    assert _reconcile(Provider(by_number={"+14155550100": found})) == {"still active": 1}
    with db.session_scope() as s:
        run = s.get(db.CallRun, run_id)
        assert (run.twilio_call_sid, run.status, run.status_source) == ("CAreal", "in-progress", "provider")
        assert s.query(db.CallRun).count() == 1  # one person, one call


def test_a_request_the_provider_refused_outright_fails_at_once_and_can_be_retried(campaign, monkeypatch):
    with db.session_scope() as s:
        lead = db.Lead(campaign_id=campaign, phone_e164="+14155550100")
        s.add(lead)
        s.flush()
        lead_id = lead.id

    class Refused(Exception):
        status = 400  # e.g. the number is not a valid destination

    async def refused(run_id, phone, settings):
        raise Refused("invalid number")

    monkeypatch.setattr(twilio_client, "place_call_async", refused)
    with pytest.raises(Refused):
        asyncio.run(CampaignRunner(SETTINGS).dial_lead(campaign, lead_id))
    with db.session_scope() as s:
        assert s.query(db.CallRun).one().status == "failed" and s.get(db.Lead, lead_id).status == "pending"
    assert twilio_client.definitely_not_placed(Refused()) and not twilio_client.definitely_not_placed(TimeoutError())
