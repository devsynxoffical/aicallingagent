"""Consent on record, the do-not-call list, removing what is held about a person, and
not keeping call content longer than decided. These test that the engine does what
was configured. Whether what was configured satisfies the law of a given market is
not something a test here can show."""

import io
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from callagent import db, privacy
from callagent.config import get_settings, resolve_api_token
from callagent.db import Appointment, CallRun, Campaign, DoNotCall, Job, Lead, UsageRecord, WebsiteRequest, session_scope, utcnow
from callagent.dialer.campaign_runner import LeadNotDialable, apply_twilio_status, claim_lead_for_call, pick_next_lead
from conftest import POSTGRES_URL, postgres_app_url

PASSWORD = "correct horse battery"
BRIEF = {"company_name": "Acme", "offer_summary": "A demo.", "agent_name": "Sam", "language": "en"}


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def client(request, tmp_path, monkeypatch):
    url = postgres_app_url() if request.param == "postgres" else f"sqlite:///{tmp_path / 'privacy.db'}"
    for name, value in {"TWILIO_VALIDATE_SIGNATURE": "false", "DATABASE_URL": url, "DATA_DIR": str(tmp_path / "data"), "COST_PER_CONNECTED_MINUTE_USD": "0.10",
                        "CALLING_WINDOW_START": "00:00", "CALLING_WINDOW_END": "23:59"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    db.reset_engine_for_tests(url)
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        assert c.post("/api/campaign-briefs", json={**BRIEF, "name": "q4"}).status_code == 201
        yield c
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


def campaign_id():
    with session_scope() as s:
        return s.execute(select(Campaign.id).where(Campaign.name == "q4")).scalar_one()


def leads():
    with session_scope() as s:
        return {lead.phone_e164: SimpleNamespace(id=lead.id, status=lead.status, basis=lead.consent_basis, source=lead.consent_source, at=lead.consent_at, name=lead.first_name)
                for lead in s.execute(select(Lead).order_by(Lead.id)).scalars()}


def upload(client, sheet: str, **params):
    return client.post("/api/campaigns/q4/leads", files={"file": ("people.csv", io.BytesIO(sheet.encode()), "text/csv")}, params=params)


# ----------------------------------------------------------------------------- consent


def test_a_sheet_says_per_person_on_what_ground_they_may_be_called(client):
    sheet = ("First Name,Phone,Consent,Consent date,Consent source\n"
             "Ada,+14155550101,yes,2026-03-01,Website form\n"
             "Grace,+14155550102,no,,\n"
             "Edsger,+14155550103,existing customer,,Contract 2025\n"
             "Linus,+14155550104,,,\n")
    report = upload(client, sheet).json()
    assert (report["imported"], report["with_consent"]) == (4, 2)
    people = leads()
    assert (people["+14155550101"].basis, people["+14155550101"].source, people["+14155550101"].at.date().isoformat()) == ("opt_in", "Website form", "2026-03-01")
    assert people["+14155550103"].basis == "existing customer" and people["+14155550103"].at is not None  # no date given: on record from the import
    assert people["+14155550102"].basis is None and people["+14155550104"].basis is None  # "no" and nothing are both nothing


def test_one_ground_can_be_stated_for_a_whole_list_and_a_column_in_the_sheet_wins(client):
    upload(client, "Phone\n+14155550111\n+14155550112\n", consent="opt_in", consent_source="Spring webinar sign-up")
    people = leads()
    assert all((people[n].basis, people[n].source) == ("opt_in", "Spring webinar sign-up") for n in ("+14155550111", "+14155550112"))
    # A sheet that speaks for each person is believed over a blanket statement about all of them.
    upload(client, "Phone,Consent\n+14155550113,no\n", consent="opt_in")
    assert leads()["+14155550113"].basis is None


def test_a_campaign_that_requires_consent_never_calls_someone_without_it(client):
    upload(client, "First Name,Phone,Consent\nAda,+14155550101,yes\nGrace,+14155550102,\nLinus,+14155550104,\n")
    people, cid = leads(), campaign_id()
    assert client.put("/api/campaigns/q4/rules", json={"require_consent": True}).status_code == 200

    with session_scope() as s:  # the dial loop only ever picks the person on record
        picked = pick_next_lead(s, cid, get_settings())
        assert picked is not None and picked.phone_e164 == "+14155550101"
        claim_lead_for_call(s, cid, picked.id)
        assert pick_next_lead(s, cid, get_settings()) is None  # the other two are not "next": they are not called at all

    # A manual dial is held to the same rule: it is decided where every call is created.
    for manual in (False, True):
        with pytest.raises(LeadNotDialable, match="only people with consent on record"):
            with session_scope() as s:
                claim_lead_for_call(s, cid, people["+14155550102"].id, manual=manual)
    assert leads()["+14155550102"].status == "pending"  # still in the list, uncalled

    # Without the rule the campaign calls everyone, as before.
    client.put("/api/campaigns/q4/rules", json={})
    with session_scope() as s:
        assert pick_next_lead(s, cid, get_settings()).phone_e164 == "+14155550102"


# ----------------------------------------------------------------------------- the do-not-call list


def test_a_register_is_imported_and_everyone_on_it_leaves_the_call_lists_at_once(client):
    upload(client, "Phone\n+14155550101\n+14155550102\n+14155550103\n")
    register = "phone\n+1 (415) 555-0102\n4155550103\n+14155550199\nnot a number\n\n+14155550102\n"
    report = client.post("/api/do-not-call/import", files={"file": ("register.csv", io.BytesIO(register.encode()), "text/csv")}, params={"reason": "National register, June"}).json()
    assert report == {"added": 3, "already_listed": 1, "invalid": 1, "samples_invalid": ["not a number"]}

    people = leads()
    assert (people["+14155550101"].status, people["+14155550102"].status, people["+14155550103"].status) == ("pending", "dnc", "dnc")
    with pytest.raises(LeadNotDialable, match="do-not-call"):
        with session_scope() as s:
            claim_lead_for_call(s, campaign_id(), people["+14155550102"].id, manual=True)
    # And nobody on it can be added to a campaign later.
    assert upload(client, "Phone\n+14155550199\n").json()["dnc"] == 1

    listed = client.get("/api/do-not-call").json()
    assert listed["total"] == 3 and {e["reason"] for e in listed["entries"]} == {"National register, June"}
    assert [e["phone"] for e in client.get("/api/do-not-call", params={"q": "0199"}).json()["entries"]] == ["+14155550199"]


def test_anyone_who_runs_campaigns_can_add_a_number_and_only_an_admin_can_take_one_off(client):
    for email, role in (("op@example.com", "operator"), ("viewer@example.com", "viewer")):
        client.post("/api/users", json={"email": email, "role": role, "password": PASSWORD})
    sign_in = lambda email: {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers={"Authorization": ""}).json()["token"]}  # noqa: E731
    operator, viewer = sign_in("op@example.com"), sign_in("viewer@example.com")

    assert client.post("/api/do-not-call", json={"phone": "+14155550150", "reason": "Asked by email"}, headers=viewer).status_code == 403
    added = client.post("/api/do-not-call", json={"phone": "(415) 555-0150", "reason": "Asked by email"}, headers=operator)
    assert added.status_code == 201 and added.json()["added"] == 1
    assert client.post("/api/do-not-call", json={"phone": "hello"}, headers=operator).status_code == 400
    entry = client.get("/api/do-not-call", headers=viewer).json()["entries"][0]
    assert (entry["phone"], entry["reason"]) == ("+14155550150", "Asked by email")

    assert client.delete(f"/api/do-not-call/{entry['id']}", headers=operator).status_code == 403  # allowing a number to be called again is an admin's decision
    assert client.delete(f"/api/do-not-call/{entry['id']}").status_code == 200
    assert client.delete(f"/api/do-not-call/{entry['id']}").status_code == 404
    assert client.get("/api/do-not-call").json()["total"] == 0


# ----------------------------------------------------------------------------- removing what is held about a person


def person_with_history(client) -> SimpleNamespace:
    upload(client, "First Name,Last Name,Company,Email,Phone,Notes,Segment,Consent\nAda,Lovelace,Analytical Ltd,ada@analytical.example,+14155550101,Prefers mornings,VIP,yes\nGrace,Hopper,Navy,grace@navy.example,+14155550102,,,\n")
    people = leads()
    with session_scope() as s:
        ada = people["+14155550101"].id
        run = CallRun(lead_id=ada, campaign_id=campaign_id(), status="in-progress", twilio_call_sid="CA1", stream_connected=True, disposition="meeting_booked",
                      transcript=[{"role": "user", "text": "This is Ada Lovelace, call me at home."}], tool_events=[{"tool": "book_meeting", "arguments": {"notes": "Ada wants a demo"}}],
                      summary={"review": {"summary": "Ada is interested", "prospect_facts": ["has two children"]}, "latency": {"response": {"p50": 0.8}}},
                      recording_url="https://provider.example/recordings/RE1")
        s.add(run)
        s.flush()
        apply_twilio_status(s, run.id, {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "125"}, get_settings())
        s.add(Appointment(lead_id=ada, call_run_id=run.id, starts_at="2027-01-05T10:00:00+00:00", notes="Ada asked for the long demo"))
        s.add(Job(kind="booking_webhook", key="booking_webhook:appointment:1", status="done", call_run_id=run.id, payload={"delivery_id": "d", "body": {"lead": {"first_name": "Ada", "phone_e164": "+14155550101"}}}))
        s.add(WebsiteRequest(organization_id=run.organization_id, kind="callback", name="Ada Lovelace", phone="+14155550101", email="ada@analytical.example", message="Please call me back"))
        other = CallRun(lead_id=people["+14155550102"].id, campaign_id=campaign_id(), status="completed", transcript=[{"role": "user", "text": "Grace here."}], ended_at=utcnow())
        s.add(other)
        s.flush()
        return SimpleNamespace(lead=ada, run=run.id, other_run=other.id)


def everything_stored() -> str:
    """Every text column and JSON value in the tables that hold personal data, as one string."""
    with session_scope() as s:
        parts = []
        for model in (Lead, CallRun, Appointment, Job, WebsiteRequest):
            for row in s.execute(select(model)).scalars():
                parts.append(str({column.name: getattr(row, column.name) for column in model.__table__.columns}))
        return "\n".join(parts)


def test_erasing_a_person_removes_them_everywhere_and_keeps_what_accounting_needs(client):
    ids = person_with_history(client)
    before = everything_stored()
    assert "Lovelace" in before and "has two children" in before and "+14155550101" in before

    report = client.post("/api/people/erase", json={"phone": "(415) 555-0101"})
    assert report.status_code == 200
    assert report.json() == {"leads": 1, "calls": 1, "appointments": 1, "website_messages": 1, "notifications": 1, "on_do_not_call_list": True,
                             "recordings_to_delete_at_the_provider": ["https://provider.example/recordings/RE1"]}

    after = everything_stored()
    for trace in ("Ada", "Lovelace", "Analytical", "ada@", "+14155550101", "4155550101", "Prefers mornings", "VIP", "two children", "long demo", "call me back", "RE1"):
        assert trace not in after, trace
    assert "Grace" in after and "Grace here." in after  # nobody else's data was touched

    with session_scope() as s:
        lead, run = s.get(Lead, ids.lead), s.get(CallRun, ids.run)
        assert (lead.status, lead.consent_basis, lead.phone_e164) == ("dnc", None, f"erased-{ids.lead}")
        # What says nothing about the person stays: that a call happened, how long, how it ended, what it cost.
        assert (run.status, run.duration_seconds, run.disposition, run.summary) == ("completed", 125, "meeting_booked", {"latency": {"response": {"p50": 0.8}}})
        assert run.content_erased_at is not None and run.transcript == [] and run.recording_url is None
        assert str(s.execute(select(UsageRecord.amount_usd).where(UsageRecord.call_run_id == ids.run)).scalar_one()) == "0.3000"
        # The number is kept in one place, on purpose: it is how "never call me again" is honoured.
        assert s.execute(select(DoNotCall.reason).where(DoNotCall.phone_e164 == "+14155550101")).scalar_one() == "erased at the person's request"

    again = client.post("/api/people/erase", json={"phone": "+14155550101"}).json()
    assert (again["leads"], again["calls"], again["recordings_to_delete_at_the_provider"]) == (0, 0, [])  # nothing left to remove
    assert upload(client, "Phone\n+14155550101\n").json()["dnc"] == 1  # and they cannot be added back
    assert client.post("/api/people/erase", json={"phone": "hello"}).status_code == 400


def test_erasing_is_an_admins_decision_and_is_recorded_without_the_number(client):
    person_with_history(client)
    client.post("/api/users", json={"email": "op@example.com", "role": "operator", "password": PASSWORD})
    operator = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": "op@example.com", "password": PASSWORD}, headers={"Authorization": ""}).json()["token"]}
    assert client.post("/api/people/erase", json={"phone": "+14155550101"}, headers=operator).status_code == 403
    assert client.post("/api/people/erase", json={"phone": "+14155550101", "keep_on_do_not_call": False}).json()["on_do_not_call_list"] is False

    entries = [e for e in client.get("/api/audit").json() if e["action"] == "erase_person"]
    assert [(e["actor"], e["status"]) for e in entries] == [("api token", 200), ("op@example.com", 403)]
    assert "4155550101" not in str(entries)  # that someone was erased is on record; who is not


# ----------------------------------------------------------------------------- retention


def test_call_content_is_removed_after_the_period_the_organization_set_and_not_before(client):
    ids = person_with_history(client)
    with session_scope() as s:
        s.get(CallRun, ids.run).ended_at = utcnow() - timedelta(days=45)
        s.get(CallRun, ids.other_run).ended_at = utcnow() - timedelta(days=10)
        assert privacy.apply_retention(s) == {}  # nothing was set: everything is kept

    saved = client.put("/api/organization/settings", json={"retention_days": 30})
    assert saved.status_code == 200 and saved.json()["retention_days"] == 30
    with session_scope() as s:
        organization = s.get(CallRun, ids.run).organization_id
        assert privacy.apply_retention(s) == {organization: 1}
        old, recent = s.get(CallRun, ids.run), s.get(CallRun, ids.other_run)
        assert old.transcript == [] and old.recording_url is None and old.content_erased_at is not None and old.duration_seconds == 125
        assert recent.transcript == [{"role": "user", "text": "Grace here."}] and recent.content_erased_at is None
        assert privacy.apply_retention(s) == {}  # run again: nothing more to do

    # Changing another setting leaves the period alone; null switches it off.
    client.put("/api/organization/settings", json={"human_transfer_number": "+14155550299"})
    assert client.get("/api/organization/settings").json()["retention_days"] == 30
    assert client.put("/api/organization/settings", json={"retention_days": None}).json()["retention_days"] is None
    assert client.put("/api/organization/settings", json={"retention_days": 0}).status_code == 422
