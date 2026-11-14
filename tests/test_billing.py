"""The usage ledger: a line per cost, written once, corrected only by adding; spend
and budgets read from it. The phone provider and the AI model are stand-ins here:
no figure in this file came from a provider, and none has been compared with an invoice."""

import asyncio
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from callagent import background, billing, db, llm
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.db import AIUsage, CallRun, Campaign, Lead, UsageRecord, session_scope, utcnow
from callagent.dialer import rules
from callagent.dialer.campaign_runner import LeadNotDialable, apply_twilio_status, claim_lead_for_call
from callagent.postcall import summarizer
from callagent.training import versions
from conftest import POSTGRES_URL, postgres_app_url

SETTINGS = Settings(anthropic_api_key="x", cost_per_connected_minute_usd=0.10, cost_per_call_attempt_usd=0.02, max_attempts=3, call_time_limit_seconds=600)
TRANSCRIPT = [{"role": "assistant", "text": "Hi, this is Jordan."}, {"role": "user", "text": "Not interested, thanks."}]


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set"))])
def campaign(request, tmp_path, sample_playbook, monkeypatch):
    url = f"sqlite:///{tmp_path / 'bill.db'}" if request.param == "sqlite" else postgres_app_url()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COST_PER_CONNECTED_MINUTE_USD", "0.10")
    monkeypatch.setenv("COST_PER_CALL_ATTEMPT_USD", "0.02")
    get_settings.cache_clear()
    db.reset_engine_for_tests(url)
    with session_scope() as s:
        c = Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        ids = SimpleNamespace(campaign=c.id, org=c.organization_id, backend=request.param)
    yield ids
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


def new_call(campaign, phone="+14155550100", status="in-progress", seconds_ago=170, connected=True) -> int:
    from datetime import timedelta

    with session_scope() as s:
        lead = Lead(campaign_id=campaign.campaign, phone_e164=phone, status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = CallRun(lead_id=lead.id, campaign_id=campaign.campaign, status=status, twilio_call_sid=f"CA{lead.id}", stream_connected=connected,
                      started_at=utcnow() - timedelta(seconds=seconds_ago))
        s.add(run)
        s.flush()
        return run.id


def ledger(call_run_id=None):
    with session_scope() as s:
        query = select(UsageRecord).order_by(UsageRecord.id)
        if call_run_id is not None:
            query = query.where(UsageRecord.call_run_id == call_run_id)
        return [SimpleNamespace(id=l.id, kind=l.kind, key=l.key, seconds=l.quantity_seconds, quantity=Decimal(str(l.quantity)), unit=l.unit, amount=Decimal(str(l.amount_usd)),
                                basis=l.basis, corrects=l.adjusts_id, detail=dict(l.detail or {}), note=l.note, by=l.created_by, org=l.organization_id, call=l.call_run_id)
                for l in s.execute(query).scalars()]


def carrier_says(call_run_id, seconds, status="completed"):
    with session_scope() as s:
        apply_twilio_status(s, call_run_id, {"CallSid": f"CA-{call_run_id}", "CallStatus": status, "CallDuration": str(seconds)}, SETTINGS)


def engine_ends(call_run_id, sample_playbook, monkeypatch):
    monkeypatch.setattr(summarizer, "make_client", lambda settings=None: object())
    monkeypatch.setattr(summarizer, "review_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    return summarizer.finalize_call(call_run_id, sample_playbook, TRANSCRIPT, [], "live", None, SETTINGS)


def call_cost(call_run_id):
    with session_scope() as s:
        return s.get(CallRun, call_run_id).cost_usd


# ----------------------------------------------------------------------------- a call's line (audit D-1)


def test_a_finished_call_has_one_line_that_says_how_its_amount_was_arrived_at(campaign):
    run = new_call(campaign)
    carrier_says(run, 170)
    (line,) = ledger(run)
    assert (line.kind, line.key, line.unit, line.basis, line.corrects) == ("call", f"call:{run}:call", "minute", "estimate", None)
    assert (line.seconds, line.quantity, line.amount) == (170, Decimal("3"), Decimal("0.32"))  # the attempt, and three started minutes
    assert line.detail == {"seconds": 170, "minutes": 3, "per_minute_usd": "0.1", "attempt_fee_usd": "0.02", "source": "carrier"}
    assert line.org == campaign.org and call_cost(run) == pytest.approx(0.32)


def test_the_final_report_delivered_any_number_of_times_adds_nothing(campaign):
    run = new_call(campaign)
    for seconds in (170, 170, 999, 5):  # repeated, and repeated with other numbers: the first report stands
        carrier_says(run, seconds)
    assert [(l.seconds, l.amount) for l in ledger(run)] == [(170, Decimal("0.32"))]
    with session_scope() as s:
        assert rules.campaign_spend(s, campaign.campaign) == pytest.approx(0.32)


# ----------------------------------------------------------------------------- out-of-order endings (audit D-2)


async def test_the_engines_figure_is_corrected_by_the_carriers_without_changing_the_first_line(campaign, sample_playbook, monkeypatch):
    run = new_call(campaign, seconds_ago=200)  # the engine's own clock, from when the call was placed: 4 started minutes
    await engine_ends(run, sample_playbook, monkeypatch)
    (first,) = ledger(run)
    assert first.detail["source"] == "engine" and first.amount == Decimal("0.42") and call_cost(run) == pytest.approx(0.42)

    carrier_says(run, 95)  # the carrier billed 95 connected seconds: 2 started minutes
    original, correction = ledger(run)
    assert (original.id, original.amount, original.seconds, original.detail) == (first.id, first.amount, first.seconds, first.detail)  # untouched
    assert correction.corrects == original.id and correction.amount == Decimal("-0.20") and correction.detail["source"] == "carrier"
    assert "the carrier reported 95 s" in correction.note
    # Spend, the call's cost and its duration all follow the ledger.
    with session_scope() as s:
        assert rules.campaign_spend(s, campaign.campaign) == pytest.approx(0.22)
        assert s.get(CallRun, run).cost_usd == pytest.approx(0.22) and s.get(CallRun, run).duration_seconds == 95

    carrier_says(run, 95)
    carrier_says(run, 400)  # again, and again with another figure
    assert len(ledger(run)) == 2


async def test_when_the_carrier_reports_first_the_engines_ending_adds_nothing(campaign, sample_playbook, monkeypatch):
    run = new_call(campaign, seconds_ago=200)
    carrier_says(run, 95)
    await engine_ends(run, sample_playbook, monkeypatch)
    assert [(l.detail["source"], l.amount) for l in ledger(run)] == [("carrier", Decimal("0.22"))]
    assert call_cost(run) == pytest.approx(0.22)


# ----------------------------------------------------------------------------- AI model lines (audit D-1, C-9)


def test_ai_use_with_a_known_price_is_a_ledger_line_and_part_of_the_calls_cost(campaign):
    run = new_call(campaign)
    carrier_says(run, 60)
    tokens = {"input": 100_000, "output": 10_000, "cache_read": 0, "cache_write": 0}  # $0.50 + $0.25 at claude-opus-5 list prices
    with llm.attributed(call_run_id=run):
        llm.record_usage("call_review", "claude-opus-5", tokens, outcome="ok", duration_ms=900, settings=SETTINGS)
        llm.record_usage("call_review", "some-future-model", tokens, outcome="ok", duration_ms=900, settings=SETTINGS)  # no price known: nothing to charge
    with llm.attributed(campaign_id=campaign.campaign):
        llm.record_usage("coach", "claude-opus-5", tokens, outcome="ok", duration_ms=900, settings=SETTINGS)  # for the campaign, about no one call
    llm.record_usage("rehearsal", "claude-opus-5", tokens, outcome="ok", duration_ms=900, settings=SETTINGS)  # for no campaign: measured, not charged

    lines = [l for l in ledger() if l.kind == "ai_model"]
    assert [(l.amount, l.unit, l.quantity, l.call, l.detail["workflow"]) for l in lines] == [
        (Decimal("0.75"), "token", Decimal("110000"), run, "call_review"), (Decimal("0.75"), "token", Decimal("110000"), None, "coach")]
    assert all(l.key == f"ai:{l.detail['ai_usage_id']}" and l.basis == "estimate" for l in lines)
    assert call_cost(run) == pytest.approx(0.12 + 0.75)  # the minute, and the review of it
    with session_scope() as s:
        assert s.query(AIUsage).count() == 4  # every use is measured, whether or not it is charged
        assert rules.campaign_spend(s, campaign.campaign) == pytest.approx(0.12 + 0.75 + 0.75)
        assert billing.record_ai(s, s.execute(select(AIUsage).order_by(AIUsage.id)).scalars().first()).key == lines[0].key  # asked again: the same line
        assert s.query(UsageRecord).filter(UsageRecord.kind == "ai_model").count() == 2


# ----------------------------------------------------------------------------- corrections


def test_a_correction_is_a_new_line_and_the_line_it_corrects_never_changes(campaign):
    run = new_call(campaign)
    carrier_says(run, 170)
    (original,) = ledger(run)
    with session_scope() as s:
        made = billing.adjust(s, original.id, Decimal("-0.10"), note="Goodwill credit after a dropped call", by="ops@devytalk.example")
        assert (made.adjusts_id, str(made.amount_usd), made.basis, made.created_by) == (original.id, "-0.1000", "manual", "ops@devytalk.example")
    unchanged, correction = ledger(run)
    assert (unchanged.amount, unchanged.detail, unchanged.note) == (original.amount, original.detail, "")
    assert correction.note == "Goodwill credit after a dropped call" and call_cost(run) == pytest.approx(0.22)

    with session_scope() as s:
        for amount, note, problem in [(Decimal("0"), "nothing", "zero"), (Decimal("1"), "  ", "needs a note")]:
            with pytest.raises(billing.LedgerError, match=problem):
                billing.adjust(s, original.id, amount, note=note, by="ops")
        with pytest.raises(billing.LedgerError, match="itself a correction"):
            billing.adjust(s, correction.id, Decimal("1"), note="again", by="ops")
        with pytest.raises(billing.LedgerError, match="no such"):
            billing.adjust(s, original.id, Decimal("1"), note="not theirs", by="ops", organization_id=campaign.org + 999)


def test_no_code_changes_or_removes_a_ledger_line():
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path(billing.__file__).parent.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        if re.search(r"(update|delete)\(\s*(db\.)?UsageRecord", path.read_text()):
            offenders.append(path.name)
    assert offenders == []


async def test_the_providers_own_price_corrects_the_estimate_once(campaign, monkeypatch):
    run = new_call(campaign)
    carrier_says(run, 170)  # estimated at 0.32
    answers = iter([{"sid": "CA1", "status": "completed", "duration": "170", "price": None, "price_unit": "USD"},
                    {"sid": "CA1", "status": "completed", "duration": "170", "price": "-0.0420", "price_unit": "USD"},
                    {"sid": "CA1", "status": "completed", "duration": "170", "price": "-0.0420", "price_unit": "USD"}])
    provider = SimpleNamespace(fetch_call=lambda sid, settings: next(answers))

    with pytest.raises(RuntimeError, match="not priced this call yet"):  # the attempt fails, so the queue asks again later
        await background.fetch_provider_price({"call_run_id": run}, provider=provider)
    assert len(ledger(run)) == 1

    await background.fetch_provider_price({"call_run_id": run}, provider=provider)
    estimate, correction = ledger(run)
    assert estimate.amount == Decimal("0.32") and estimate.basis == "estimate"
    assert (correction.amount, correction.basis, correction.corrects, correction.key) == (Decimal("-0.2780"), "provider", estimate.id, f"call:{run}:call:provider-price")
    assert call_cost(run) == pytest.approx(0.042)

    await background.fetch_provider_price({"call_run_id": run}, provider=provider)  # asked again: nothing more
    assert len(ledger(run)) == 2

    other = new_call(campaign, phone="+14155550101")
    carrier_says(other, 60)
    euros = SimpleNamespace(fetch_call=lambda sid, settings: {"sid": "CA2", "status": "completed", "duration": "60", "price": "-0.03", "price_unit": "EUR"})
    await background.fetch_provider_price({"call_run_id": other}, provider=euros)
    assert len(ledger(other)) == 1  # another currency is not silently treated as dollars


def test_asking_the_provider_for_prices_is_off_unless_switched_on(campaign):
    run = new_call(campaign)
    carrier_says(run, 60)
    with session_scope() as s:
        assert s.query(db.Job).filter(db.Job.kind == background.PROVIDER_PRICE).count() == 0
    other = new_call(campaign, phone="+14155550101")
    with session_scope() as s:
        apply_twilio_status(s, other, {"CallSid": "CA9", "CallStatus": "completed", "CallDuration": "60"}, SETTINGS.model_copy(update={"reconcile_provider_price": True}))
    with session_scope() as s:
        job = s.query(db.Job).filter(db.Job.kind == background.PROVIDER_PRICE).one()
        assert job.call_run_id == other and job.run_at > utcnow()  # later: the provider prices a call some minutes after it ends


# ----------------------------------------------------------------------------- the budget when a call is claimed (audit D-3)


def leads(campaign, n):
    with session_scope() as s:
        have = s.query(Lead).count()
        rows = [Lead(campaign_id=campaign.campaign, phone_e164=f"+1415555{2000 + have + i}", status="pending") for i in range(n)]
        s.add_all(rows)
        s.flush()
        return [lead.id for lead in rows]


def set_budget(campaign, amount):
    with session_scope() as s:
        s.get(Campaign, campaign.campaign).rules_json = {"budget_usd": amount}


def test_of_many_claims_against_a_budget_that_covers_one_call_exactly_one_is_placed(campaign):
    # A call may last ten minutes: at worst 0.02 + 10 x 0.10 = 1.02. The budget covers one such call, not two.
    set_budget(campaign, 1.50)
    lead_ids = leads(campaign, 8)
    outcomes, start = [], threading.Barrier(8)

    def claim(lead_id, manual):
        start.wait(5)
        try:
            with session_scope() as s:
                claim_lead_for_call(s, campaign.campaign, lead_id, manual=manual)
            outcomes.append("placed")
        except LeadNotDialable as e:
            outcomes.append(str(e))
        except Exception as e:  # on SQLite a writer may be turned away while another holds the lock; that places no call either
            outcomes.append(f"error: {type(e).__name__}")

    threads = [threading.Thread(target=claim, args=(lead_id, i % 2 == 0)) for i, lead_id in enumerate(lead_ids)]  # the dial loop and an operator's manual dials, mixed
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert outcomes.count("placed") == 1, outcomes
    assert all("does not cover another call" in o for o in outcomes if o != "placed" and not o.startswith("error")), outcomes
    with session_scope() as s:
        assert s.query(CallRun).count() == 1
        # The leads that were refused are as they were: not marked as being called, no attempt counted.
        assert sorted((lead.status, lead.attempts) for lead in s.query(Lead)) == [("calling", 1)] + [("pending", 0)] * 7


def test_what_was_spent_and_what_is_in_progress_both_count_against_the_budget(campaign):
    set_budget(campaign, 2.50)
    first, second, third = leads(campaign, 3)
    with session_scope() as s:
        run_id, _phone = claim_lead_for_call(s, campaign.campaign, first)  # reserved at its worst case: 1.02
    with session_scope() as s:
        claim_lead_for_call(s, campaign.campaign, second)  # 2.04 at worst: still within 2.50
    with pytest.raises(LeadNotDialable, match="while 2 are in progress"):
        with session_scope() as s:
            claim_lead_for_call(s, campaign.campaign, third, manual=True)  # an operator's own dial is held to the budget too

    carrier_says(run_id, 120)  # the first call ended after two minutes: 0.22 spent, its reservation released
    with session_scope() as s:
        claim_lead_for_call(s, campaign.campaign, third)  # 0.22 spent + one in progress + this one = 2.26

    set_budget(campaign, 0.20)
    (fourth,) = leads(campaign, 1)
    with pytest.raises(LeadNotDialable, match=r"budget of \$0.20 is spent"):
        with session_scope() as s:
            claim_lead_for_call(s, campaign.campaign, fourth)


def test_a_campaign_without_a_budget_is_not_held_back(campaign):
    for lead_id in leads(campaign, 3):
        with session_scope() as s:
            claim_lead_for_call(s, campaign.campaign, lead_id)
    with session_scope() as s:
        assert s.query(CallRun).count() == 3


# ----------------------------------------------------------------------------- reading the ledger


def test_the_ledger_is_read_by_its_organization_and_corrected_only_by_the_operator(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    password = "correct horse battery"
    with TestClient(app) as client:
        operator = {"Authorization": f"Bearer {resolve_api_token(get_settings())}"}
        client.headers.update(operator)
        assert client.post("/api/users", json={"email": "admin@acme.example", "role": "admin", "password": password}).status_code == 201
        other = client.post("/api/organizations", json={"name": "Bolt", "admin_email": "boss@bolt.example", "admin_password": password}).json()
        sign_in = lambda email: {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": password}, headers={"Authorization": ""}).json()["token"]}  # noqa: E731
        acme, bolt = sign_in("admin@acme.example"), sign_in("boss@bolt.example")

        with session_scope() as s:
            c = Campaign(name="q4", status="running", playbook_json=sample_playbook.model_dump())
            s.add(c)
            s.flush()
            lead = Lead(campaign_id=c.id, phone_e164="+14155550100", status="calling", attempts=1)
            s.add(lead)
            s.flush()
            run = CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress", twilio_call_sid="CA1")
            s.add(run)
            s.flush()
            apply_twilio_status(s, run.id, {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "170"}, SETTINGS)
            line_id = s.query(UsageRecord).one().id

        mine = client.get("/api/usage", headers=acme).json()
        assert mine["total_usd"] == "0.3200" and mine["by_kind"] == [{"kind": "call", "basis": "estimate", "lines": 1, "amount_usd": "0.3200"}]
        assert mine["lines"][0]["detail"]["minutes"] == 3 and "not an invoice" in mine["what_the_amounts_are"]
        assert client.get("/api/usage", params={"campaign": "q4"}, headers=acme).json()["total_usd"] == "0.3200"
        theirs = client.get("/api/usage", headers=bolt).json()
        assert (theirs["total_usd"], theirs["by_kind"], theirs["lines"]) == ("0.0000", [], [])  # another organization sees none of it

        credit = {"amount_usd": "-0.12", "note": "Credit for a dropped call"}
        assert client.post(f"/api/usage/{line_id}/adjustments", json=credit, headers=acme).status_code == 403  # not by the organization billed
        # The operator acts in one organization at a time, and cannot reach another's line from there.
        assert client.post(f"/api/usage/{line_id}/adjustments", json=credit, headers={**operator, "X-DevyTalk-Organization": str(other["id"])}).status_code == 404
        assert client.post(f"/api/usage/{line_id}/adjustments", json={"amount_usd": "-0.12", "note": ""}, headers=operator).status_code == 422
        made = client.post(f"/api/usage/{line_id}/adjustments", json=credit, headers=operator)
        assert made.status_code == 201 and (made.json()["corrects"], made.json()["basis"], made.json()["amount_usd"]) == (line_id, "manual", "-0.1200")

        after = client.get("/api/usage", headers=acme).json()
        assert after["total_usd"] == "0.2000" and [(k["basis"], k["amount_usd"]) for k in after["by_kind"]] == [("estimate", "0.3200"), ("manual", "-0.1200")]
        assert client.get("/api/campaigns/q4/rules", headers=acme).json()["spend_usd"] == pytest.approx(0.20)
    get_settings.cache_clear()


# ----------------------------------------------------------------------------- monthly statements


def _line(campaign, key, amount, *, at, kind="call", quantity=1, unit="minute", adjusts=None, basis="estimate", note=None) -> int:
    """A ledger line written at a chosen moment (the ledger itself always writes 'now')."""
    with session_scope() as s:
        line = UsageRecord(organization_id=campaign.org, campaign_id=campaign.campaign, kind=kind, key=key, quantity=quantity, unit=unit, amount_usd=Decimal(amount),
                           basis=basis, adjusts_id=adjusts, note=note, created_at=at, quantity_seconds=int(quantity * 60) if kind == "call" else 0)
        s.add(line)
        s.flush()
        return line.id


def test_a_month_s_statement_adds_up_calls_ai_and_corrections_and_stays_as_it_is(campaign):
    from datetime import datetime, timezone

    october, november = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc), datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    first = _line(campaign, "call:1:call", "0.3200", at=october, quantity=3)
    _line(campaign, "call:2:call", "0.1200", at=november, quantity=1)
    _line(campaign, "call:3:call", "0.5200", at=datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc), quantity=5)
    _line(campaign, "ai:1", "0.0150", at=datetime(2026, 11, 14, 9, 5, tzinfo=timezone.utc), kind="ai_model", quantity=4200, unit="token")
    # A credit given in November for October's call belongs to November: October's statement was already final.
    _line(campaign, "call:1:call:adjust:1", "-0.1000", at=datetime(2026, 11, 20, tzinfo=timezone.utc), quantity=0, adjusts=first, basis="manual", note="Credit for a dropped call")

    now = datetime(2026, 11, 25, tzinfo=timezone.utc)
    with session_scope() as s:
        was = billing.statement(s, campaign.org, 2026, 10, now=now)
        this = billing.statement(s, campaign.org, 2026, 11, now=now)
        months = billing.statement_months(s, campaign.org, now=now)
        rows = billing.statement_lines(s, campaign.org, 2026, 11)

    assert (was["status"], was["total_usd"], was["calls"], was["lines"]) == ("closed", "0.3200", {"count": 1, "minutes": "3", "amount_usd": "0.3200"}, 1)
    assert this["status"] == "open" and this["number"] == f"{campaign.org}-2026-11" and this["lines"] == 4
    assert this["calls"] == {"count": 2, "minutes": "6", "amount_usd": "0.6400"}
    assert this["ai"] == {"requests": 1, "tokens": 4200, "amount_usd": "0.0150"}
    assert this["corrections"] == {"lines": 1, "amount_usd": "-0.1000"}
    assert this["total_usd"] == "0.5550" == str(Decimal(this["calls"]["amount_usd"]) + Decimal(this["ai"]["amount_usd"]) + Decimal(this["corrections"]["amount_usd"]))
    assert this["by_basis"] == [{"basis": "estimate", "lines": 3, "amount_usd": "0.6550"}, {"basis": "manual", "lines": 1, "amount_usd": "-0.1000"}]
    assert this["by_campaign"] == [{"campaign_id": campaign.campaign, "campaign": "q4", "calls": 2, "minutes": "6", "amount_usd": "0.5550"}]
    assert "not a tax invoice" in this["what_this_is"] and this["period"]["from"].startswith("2026-11-01T00:00:00") and this["period"]["until"].startswith("2026-12-01T00:00:00")
    assert months == [{"period": "2026-11", "lines": 4, "total_usd": "0.5550", "status": "open"}, {"period": "2026-10", "lines": 1, "total_usd": "0.3200", "status": "closed"}]
    assert [r["amount_usd"] for r in rows] == ["0.1200", "0.5200", "0.0150", "-0.1000"] and rows[-1]["corrects_line"] == first and rows[-1]["note"] == "Credit for a dropped call"

    # Nothing written later can change a month that is over: every new line carries the moment it was written.
    with session_scope() as s:
        run = CallRun(lead_id=_a_lead(s, campaign), campaign_id=campaign.campaign, status="in-progress", twilio_call_sid="CA-late")
        s.add(run)
        s.flush()
        apply_twilio_status(s, run.id, {"CallSid": "CA-late", "CallStatus": "completed", "CallDuration": "60"}, SETTINGS)
        billing.adjust(s, first, Decimal("-0.05"), note="A second credit for October's call", by="operator")
        assert billing.statement(s, campaign.org, 2026, 10, now=now) == was

    with session_scope() as s:
        empty = billing.statement(s, campaign.org, 2025, 1, now=now)
        assert (empty["total_usd"], empty["lines"], empty["by_campaign"], empty["status"]) == ("0.0000", 0, [], "closed")
        assert billing.statement(s, campaign.org, 2026, 12, now=now)["period"]["until"].startswith("2027-01-01")
    for nonsense in ("2026-13", "2026-1", "26-11", "november", "2026-00", ""):
        with pytest.raises(billing.LedgerError):
            billing.parse_period(nonsense)


def _a_lead(s, campaign) -> int:
    lead = Lead(campaign_id=campaign.campaign, phone_e164="+14155550177", status="calling", attempts=1)
    s.add(lead)
    s.flush()
    return lead.id


def test_a_statement_is_read_by_its_organization_as_figures_or_as_a_sheet(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'statement.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'statement.db'}")
    from callagent.server import app

    password = "correct horse battery"
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        assert client.post("/api/users", json={"email": "viewer@acme.example", "role": "viewer", "password": password}).status_code == 201
        client.post("/api/organizations", json={"name": "Bolt", "admin_email": "boss@bolt.example", "admin_password": password})
        sign_in = lambda email: {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": password}, headers={"Authorization": ""}).json()["token"]}  # noqa: E731
        acme, bolt = sign_in("viewer@acme.example"), sign_in("boss@bolt.example")
        with session_scope() as s:
            c = Campaign(name="=SUM(A1)", status="running", playbook_json=sample_playbook.model_dump())  # a name a spreadsheet would run
            s.add(c)
            s.flush()
            lead = Lead(campaign_id=c.id, phone_e164="+14155550100", status="calling", attempts=1)
            s.add(lead)
            s.flush()
            run = CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress", twilio_call_sid="CA1")
            s.add(run)
            s.flush()
            apply_twilio_status(s, run.id, {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "170"}, SETTINGS)
        period = utcnow().strftime("%Y-%m")

        assert client.get("/api/usage/statements", headers=acme).json() == [{"period": period, "lines": 1, "total_usd": "0.3200", "status": "open"}]
        mine = client.get(f"/api/usage/statements/{period}", headers=acme).json()
        assert (mine["total_usd"], mine["calls"]["count"], mine["calls"]["minutes"], mine["organization"]["name"]) == ("0.3200", 1, "3", "default")
        sheet = client.get(f"/api/usage/statements/{period}", params={"format": "csv"}, headers=acme)
        assert sheet.headers["content-type"].startswith("text/csv") and f"usage-{period}.csv" in sheet.headers["content-disposition"]
        header, row = sheet.text.strip().splitlines()
        assert header == "line,written_at,kind,campaign,call,quantity,unit,amount_usd,basis,corrects_line,note"
        assert ",call,'=SUM(A1)," in row and row.endswith(",0.3200,estimate,,")  # the name is text in a spreadsheet, not a formula

        # Another organization has its own months, and none of this one's.
        assert client.get("/api/usage/statements", headers=bolt).json() == []
        theirs = client.get(f"/api/usage/statements/{period}", headers=bolt).json()
        assert (theirs["total_usd"], theirs["lines"], theirs["organization"]["name"]) == ("0.0000", 0, "Bolt")
        assert client.get(f"/api/usage/statements/{period}", params={"format": "csv"}, headers=bolt).text.strip() == header
        assert client.get("/api/usage/statements/2026-13", headers=acme).status_code == 400
        assert client.get(f"/api/usage/statements/{period}", params={"format": "pdf"}, headers=acme).status_code == 400
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")
