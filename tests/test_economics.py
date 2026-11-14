"""What a result costs, and whether quality moved with the playbook: counted from
stored calls and summed from the ledger. The calls here are made up; no figure in
this file came from a real campaign."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from callagent import db
from callagent.config import get_settings, resolve_api_token
from callagent.db import UsageRecord, session_scope
from callagent.intelligence.economics import MIN_MEETINGS_FOR_A_COST, economics, week_of
from callagent.intelligence.report import MIN_CALLS_PER_WEEK, campaign_report
from callagent.training import versions

MONDAY = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)  # a Monday
NEXT_MONDAY = MONDAY + timedelta(days=7)


@pytest.fixture
def campaign(tmp_path, sample_playbook):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'economics.db'}")
    with session_scope() as s:
        c = db.Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        return c.id


_phone = iter(range(10_000, 99_999))


def calls(campaign_id, n, *, at, disposition, version=1, connected=True, minutes=2, cost="0.22", ai="0.03", quality=None):
    """``n`` calls that started at ``at``, each with a ledger line for the call and one for the AI."""
    with session_scope() as s:
        campaign = s.get(db.Campaign, campaign_id)
        for _ in range(n):
            lead = db.Lead(campaign_id=campaign.id, phone_e164=f"+14155{next(_phone)}", status="completed")
            s.add(lead)
            s.flush()
            run = db.CallRun(lead_id=lead.id, campaign_id=campaign.id, status="completed", disposition=disposition, stream_connected=connected, started_at=at,
                             transcript=[{"role": "user", "text": "hi"}] if connected else [], playbook_version=version,
                             summary={"review": {"agent_quality_score": quality}} if quality is not None else {})
            s.add(run)
            s.flush()
            written = at + timedelta(minutes=minutes)
            s.add(UsageRecord(organization_id=campaign.organization_id, campaign_id=campaign.id, call_run_id=run.id, kind="call", key=f"call:{run.id}:call",
                              quantity=minutes if connected else 0, unit="minute", amount_usd=Decimal(cost), created_at=written))
            if connected and ai:
                s.add(UsageRecord(organization_id=campaign.organization_id, campaign_id=campaign.id, call_run_id=run.id, kind="ai_model", key=f"ai:{run.id}",
                                  quantity=3000, unit="token", amount_usd=Decimal(ai), created_at=written))
        return campaign.organization_id


def test_cost_per_call_per_minute_and_per_meeting_are_sums_over_the_ledger_divided_by_counts(campaign):
    org = calls(campaign, 10, at=MONDAY, disposition="no_answer", connected=False, cost="0.02")
    calls(campaign, 24, at=MONDAY, disposition="not_interested")
    calls(campaign, 6, at=MONDAY, disposition="meeting_booked")
    with session_scope() as s:
        total = economics(s, org, MONDAY - timedelta(days=1))["total"]

    # 10 unanswered at 0.02; 30 connected at 0.22 for the call and 0.03 for the AI.
    assert (total["calls"], total["connected"], total["meetings"], total["billed_minutes"]) == (40, 30, 6, "60")
    assert (total["call_cost_usd"], total["ai_cost_usd"], total["spend_usd"]) == ("6.8000", "0.9000", "7.7000")
    assert total["cost_per_call_usd"] == "0.1925" and total["cost_per_connected_call_usd"] == "0.2567" and total["cost_per_minute_usd"] == "0.1283"
    assert total["cost_per_meeting_usd"] == "1.2833" and total["enough_meetings"] is True  # everything spent, the unanswered calls included, over 6 meetings
    assert (total["connect_rate"], total["meeting_rate"], total["ai_share"]) == (0.75, 0.2, 0.1169)


def test_a_ratio_with_nothing_under_it_is_absent_and_a_thin_one_is_marked(campaign):
    with session_scope() as s:
        org = s.get(db.Campaign, campaign).organization_id
        nothing = economics(s, org, MONDAY)["total"]
    assert (nothing["calls"], nothing["spend_usd"]) == (0, "0.0000")
    for ratio in ("cost_per_call_usd", "cost_per_connected_call_usd", "cost_per_minute_usd", "cost_per_meeting_usd", "connect_rate", "meeting_rate", "ai_share"):
        assert nothing[ratio] is None, ratio  # not zero: nothing was measured

    calls(campaign, 12, at=MONDAY, disposition="not_interested")
    with session_scope() as s:
        no_meetings = economics(s, org, MONDAY - timedelta(days=1))["total"]
    assert no_meetings["cost_per_meeting_usd"] is None and no_meetings["cost_per_call_usd"] == "0.2500"  # money was spent; a meeting has no price yet

    calls(campaign, MIN_MEETINGS_FOR_A_COST - 1, at=MONDAY, disposition="meeting_booked")
    with session_scope() as s:
        few = economics(s, org, MONDAY - timedelta(days=1))
    assert few["total"]["cost_per_meeting_usd"] is not None and few["total"]["enough_meetings"] is False and few["min_meetings_for_a_cost"] == MIN_MEETINGS_FOR_A_COST


def test_figures_are_broken_down_by_campaign_and_by_week_and_add_up(campaign, sample_playbook):
    org = calls(campaign, 20, at=MONDAY, disposition="not_interested")
    calls(campaign, 5, at=NEXT_MONDAY + timedelta(days=2), disposition="meeting_booked")
    with session_scope() as s:
        other = db.Campaign(name="pilot", status="paused")
        s.add(other)
        s.flush()
        versions.record_active(s, other, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        other_id = other.id
    calls(other_id, 4, at=NEXT_MONDAY, disposition="meeting_booked", cost="1.00", ai="")

    with session_scope() as s:
        report = economics(s, org, MONDAY - timedelta(days=1))
        only = economics(s, org, MONDAY - timedelta(days=1), campaign_id=other_id)
        later = economics(s, org, NEXT_MONDAY)

    assert [(row["campaign"], row["calls"], row["meetings"], row["spend_usd"]) for row in report["by_campaign"]] == [("q4", 25, 5, "6.2500"), ("pilot", 4, 4, "4.0000")]
    assert [(w["week"], w["calls"], w["meetings"], w["spend_usd"]) for w in report["by_week"]] == [("2026-11-02", 20, 0, "5.0000"), ("2026-11-09", 9, 9, "5.2500")]
    assert report["by_week"][0]["cost_per_meeting_usd"] is None and report["by_week"][1]["cost_per_meeting_usd"] == "0.5833"
    for part in ("by_campaign", "by_week"):  # the parts are the whole, cut two ways
        assert sum(Decimal(row["spend_usd"]) for row in report[part]) == Decimal(report["total"]["spend_usd"]) == Decimal("10.2500")
        assert sum(row["calls"] for row in report[part]) == report["total"]["calls"] == 29
    assert (only["total"]["calls"], only["total"]["spend_usd"], only["total"]["ai_share"]) == (4, "4.0000", 0.0)
    assert (later["total"]["calls"], later["total"]["spend_usd"]) == (9, "5.2500")  # the first week is before the period
    assert week_of(datetime(2026, 11, 8, 23, 59, tzinfo=timezone.utc)) == "2026-11-02" and week_of(datetime(2026, 11, 9, 0, 0)) == "2026-11-09"


def test_quality_and_cost_are_followed_per_playbook_version_week_by_week(campaign):
    calls(campaign, 12, at=MONDAY, disposition="not_interested", version=1, quality=5)
    calls(campaign, 2, at=MONDAY + timedelta(days=3), disposition="meeting_booked", version=1, quality=7)
    # The playbook changed during the second week: both versions took calls in it.
    calls(campaign, 3, at=NEXT_MONDAY, disposition="not_interested", version=1, quality=5)
    calls(campaign, 8, at=NEXT_MONDAY + timedelta(days=1), disposition="not_interested", version=2, quality=8)
    calls(campaign, 4, at=NEXT_MONDAY + timedelta(days=2), disposition="meeting_booked", version=2, quality=9)
    calls(campaign, 2, at=NEXT_MONDAY + timedelta(days=2), disposition="no_answer", version=2, connected=False, cost="0.02")
    with session_scope() as s:
        report = campaign_report(s, s.get(db.Campaign, campaign))

    weeks = [(w["week"], w["playbook_version"], w["calls"], w["answered"], w["reviewed"], w["average_quality"], w["conversion_rate"], w["enough_data"]) for w in report["quality_by_week"]]
    assert weeks == [
        ("2026-11-02", "1", 14, 14, 14, 5.29, 0.1429, True),
        ("2026-11-09", "1", 3, 3, 3, 5.0, 0.0, False),  # three calls: shown, and marked as too few to read anything into
        ("2026-11-09", "2", 14, 12, 12, 8.33, 0.3333, True),
    ]
    assert MIN_CALLS_PER_WEEK == 10
    v1, v2 = report["by_playbook_version"]["1"], report["by_playbook_version"]["2"]
    assert (v1["reviewed"], v1["spend_usd"], v1["cost_per_conversion"]) == (17, 4.25, 2.12)  # 17 calls at 0.25, over 2 meetings
    assert (v2["reviewed"], v2["spend_usd"], v2["cost_per_conversion"]) == (12, 3.04, 0.76)  # 12 at 0.25 and 2 at 0.02, over 4 meetings
    assert report["best_version"] is None  # neither version has enough answered calls for a winner to be named


def test_an_organization_reads_its_own_economics_and_nobody_else_s(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    password = "correct horse battery"
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        client.post("/api/users", json={"email": "viewer@acme.example", "role": "viewer", "password": password})
        client.post("/api/organizations", json={"name": "Bolt", "admin_email": "boss@bolt.example", "admin_password": password})
        sign_in = lambda email: {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": password}, headers={"Authorization": ""}).json()["token"]}  # noqa: E731
        with session_scope() as s:
            c = db.Campaign(name="q4", status="running", playbook_json=sample_playbook.model_dump())
            s.add(c)
            s.flush()
            campaign_id = c.id
        calls(campaign_id, 6, at=db.utcnow() - timedelta(days=2), disposition="meeting_booked")
        calls(campaign_id, 3, at=db.utcnow() - timedelta(days=60), disposition="meeting_booked")  # before the period asked for

        mine = client.get("/api/economics", headers=sign_in("viewer@acme.example")).json()
        assert (mine["total"]["calls"], mine["total"]["meetings"], mine["total"]["cost_per_meeting_usd"], mine["by_campaign"][0]["campaign"]) == (6, 6, "0.2500", "q4")
        assert client.get("/api/economics", params={"days": 90}, headers=sign_in("viewer@acme.example")).json()["total"]["calls"] == 9
        assert client.get("/api/economics", params={"campaign": "q4"}, headers=sign_in("viewer@acme.example")).json()["total"]["calls"] == 6
        theirs = client.get("/api/economics", headers=sign_in("boss@bolt.example")).json()
        assert (theirs["total"]["calls"], theirs["total"]["spend_usd"], theirs["by_campaign"], theirs["by_week"]) == (0, "0.0000", [], [])
        assert client.get("/api/economics", params={"campaign": "q4"}, headers=sign_in("boss@bolt.example")).status_code == 404
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")
