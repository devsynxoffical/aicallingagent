"""One customer organization can neither see nor change another's data.

Two organizations are set up side by side. Everything one owns is then asked for by
the other, through every route the API has.
"""

import io
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from callagent import accounts, db, jobs, webhooks
from callagent.accounts import invitations
from callagent.config import get_settings, resolve_api_token
from callagent.db import utcnow, Appointment, CallRun, Campaign, Job, Lead, UsageRecord, User, WebsiteRequest, session_scope
from callagent.dialer import lifecycle
from callagent.dialer.campaign_runner import claim_lead_for_call
from callagent.training import versions
from conftest import POSTGRES_URL, postgres_app_url

PASSWORD = "correct horse battery"
BRIEF = {"company_name": "Acme", "offer_summary": "A demo.", "agent_name": "Sam", "language": "en"}
SHEET = b"First Name,Phone\nAda,+14155550101\nGrace,+14155550102\n"


# Everything here runs on SQLite, where the application's own filters are the only
# protection, and again on PostgreSQL as an ordinary role, where row-level security is
# enforced underneath them (set TEST_POSTGRES_URL).
@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def client(request, tmp_path, monkeypatch):
    url = postgres_app_url() if request.param == "postgres" else f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://engine.example.com")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INTAKE_TOKEN", "website-intake-token")
    get_settings.cache_clear()
    db.reset_engine_for_tests(url)
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        c.backend = request.param
        yield c
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")  # release the connections


def sign_in(client, email):
    token = client.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers={"Authorization": ""}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def new_organization(client, name, admin_email):
    created = client.post("/api/organizations", json={"name": name, "admin_email": admin_email, "admin_password": PASSWORD, "admin_name": f"{name} admin"})
    assert created.status_code == 201, created.text
    return created.json()["id"], sign_in(client, admin_email)


@pytest.fixture
def two(client):
    """Acme and Bolt, each with an admin. Acme owns one of everything the API can address."""
    acme_id, acme = new_organization(client, "Acme", "admin@acme.example")
    bolt_id, bolt = new_organization(client, "Bolt", "admin@bolt.example")

    for name in ("Acme only", "Q4"):
        assert client.post("/api/campaign-briefs", json={**BRIEF, "name": name}, headers=acme).status_code == 201
    assert client.post("/api/campaigns/Acme only/leads", files={"file": ("leads.csv", io.BytesIO(SHEET), "text/csv")}, headers=acme).json()["imported"] == 2
    document = client.post("/api/campaigns/Acme only/documents", files={"file": ("pricing.txt", io.BytesIO(b"Pricing.\n\nThe Acme plan costs 49 dollars per month.\n"), "text/plain")}, headers=acme)
    assert document.status_code == 201, document.text
    member = client.post("/api/users", json={"email": "member@acme.example", "role": "operator", "password": PASSWORD, "name": "Acme Member"}, headers=acme).json()

    with session_scope() as s:
        campaign = db.get_campaign_by_name(s, "Acme only", acme_id)
        lead = s.execute(select(Lead).where(Lead.campaign_id == campaign.id).order_by(Lead.id)).scalars().first()
        call = CallRun(lead_id=lead.id, campaign_id=campaign.id, status="completed", direction="outbound", mode="live", stream_connected=True, twilio_call_sid="CAacme1",
                       transcript=[{"role": "user", "text": "Acme secret: we are switching suppliers."}], disposition="meeting_booked", summary={"review": {"summary": "Acme confidential summary"}})
        s.add(call)
        s.flush()
        s.add(Appointment(lead_id=lead.id, call_run_id=call.id, starts_at="2027-01-05T10:00:00+00:00"))
        draft = versions.propose(s, campaign, campaign.playbook_json, source="training", by="training", note="Acme draft")
        dead = jobs.enqueue(s, "call_review", f"call-review:{call.id}", {"call_run_id": call.id}, call_run_id=call.id)
        dead.status, dead.last_error = "dead", "Acme job error text"
        request = WebsiteRequest(organization_id=acme_id, kind="contact", email="visitor@acme-customer.example", name="Acme Visitor", message="Acme inbound message")
        s.add(request)
        s.flush()
        key, _secret = accounts.create_api_key(s, acme_id, "Acme integration", "viewer", "test")
        db.add_dnc(s, "+14155550177", acme_id, "Acme opt-out")
        suppressed = s.execute(select(db.DoNotCall).where(db.DoNotCall.phone_e164 == "+14155550177")).scalar_one()
        endpoint, _secret = webhooks.create_endpoint(s, acme_id, "https://hooks.acme.example/devytalk", ["*"], "CRM", "boss@acme.example")
        delivery = db.WebhookDelivery(organization_id=acme_id, endpoint_id=endpoint.id, event_id=None, event_type="ping", status="failed")
        s.add(delivery)
        s.flush()
        invitation, _token = invitations.invite(s, acme_id, "newcomer@acme.example", role="viewer", by="boss@acme.example")
        # A one-time link is opened by whoever holds its token, signed in or not: it names nothing by id.
        ids = {"period": utcnow().strftime("%Y-%m"), "invitation_id": invitation.id, "token": "dti_not-a-link-anyone-was-given", "endpoint_id": endpoint.id, "delivery_id": delivery.id, "call_run_id": call.id, "job_id": dead.id, "user_id": member["id"], "request_id": request.id, "document_id": document.json()["id"],
               "lead_id": lead.id, "number": draft.version, "experiment_id": 1, "name": "Acme only", "key_id": key.id, "entry_id": suppressed.id}
    return {"acme": acme, "bolt": bolt, "acme_id": acme_id, "bolt_id": bolt_id, "ids": ids}


def snapshot(acme_id):
    """Everything of Acme's that a request could change."""
    with session_scope() as s:
        campaign = db.get_campaign_by_name(s, "Acme only", acme_id)
        return {
            "campaign": (campaign.status, campaign.inbound_number, campaign.rules_json, campaign.playbook_json, campaign.stop_reason),
            "versions": [(v.version, v.status) for v in versions.list_versions(s, campaign)],
            "leads": s.execute(select(func.count()).select_from(Lead).where(Lead.organization_id == acme_id)).scalar_one(),
            "calls": [(r.id, r.status, r.disposition) for r in s.execute(select(CallRun).where(CallRun.organization_id == acme_id)).scalars()],
            "documents": s.execute(select(func.count()).select_from(db.TrainingDocument).where(db.TrainingDocument.organization_id == acme_id)).scalar_one(),
            "users": [(u.email, u.role, u.active, u.name, u.password_hash) for u in s.execute(select(User).where(User.organization_id == acme_id).order_by(User.id)).scalars()],
            "jobs": [(j.id, j.status, j.attempts) for j in s.execute(select(Job).where(Job.organization_id == acme_id)).scalars()],
            "requests": [(r.id, r.status) for r in s.execute(select(WebsiteRequest).where(WebsiteRequest.organization_id == acme_id)).scalars()],
            "campaigns": s.execute(select(func.count()).select_from(Campaign).where(Campaign.organization_id == acme_id)).scalar_one(),
            "keys": [(k.id, k.revoked_at) for k in accounts.list_api_keys(s, acme_id)],
            "endpoints": [(e.id, e.url, e.secret, e.active, tuple(e.events)) for e in webhooks.list_endpoints(s, acme_id)],
            "invitations": [(i.id, i.revoked_at, i.used_at) for i in invitations.list_invitations(s, acme_id)],
            "deliveries": [(d.id, d.status, d.attempts) for d in s.execute(select(db.WebhookDelivery).where(db.WebhookDelivery.organization_id == acme_id)).scalars()],
            "do_not_call": sorted(s.execute(select(db.DoNotCall.phone_e164).where(db.DoNotCall.organization_id == acme_id)).scalars()),
            "settings": dict(s.get(db.Organization, acme_id).settings_json or {}),
        }


# What each route needs to get past validation, so that the answer is about ownership and nothing else.
VALID = {
    ("PUT", "/api/campaigns/{name}/rules"): {"json": {}},
    ("PUT", "/api/campaigns/{name}/languages"): {"json": {"languages": ["en"]}},
    ("POST", "/api/campaigns/{name}/experiments"): {"json": {"variant_version": 2}},
    ("POST", "/api/campaigns/{name}/sources"): {"json": {"url": "https://example.com/pricing"}},
    ("POST", "/api/campaigns/{name}/documents"): {"files": {"file": ("x.txt", io.BytesIO(b"Some text that is long enough to be read as a document."), "text/plain")}},
    ("POST", "/api/campaigns/{name}/leads"): {"files": {"file": ("leads.csv", io.BytesIO(SHEET), "text/csv")}},
    ("POST", "/api/campaigns/{name}/dial"): {"params": {"phone": "+14155550199"}},
    ("GET", "/api/campaigns/{name}/knowledge/search"): {"params": {"q": "price"}},
    ("POST", "/api/campaigns/{name}/inbound"): {"params": {"number": "+14155550188"}},
    ("PATCH", "/api/users/{user_id}"): {"json": {"role": "admin", "active": False, "name": "Taken over", "password": "a new long password"}},
    ("POST", "/api/requests/{request_id}/handled"): {"json": {"handled": True}},
    ("POST", "/api/auth/invitations/{token}"): {"json": {"password": "a new long password"}},
    ("PATCH", "/api/webhooks/{endpoint_id}"): {"json": {"url": "https://tap.bolt.example/acme", "events": ["*"], "active": False}},
}
ACME_MARKERS = ["Acme only", "acme.example", "Acme secret", "Acme confidential", "Acme job error", "Acme inbound message", "acme-customer", "Acme draft", "Acme Member", "49 dollars"]


def test_every_route_refuses_another_organizations_identifiers(client, two):
    before = snapshot(two["acme_id"])
    addressed = [(method.upper(), path) for path, operations in client.app.openapi()["paths"].items() if path.startswith("/api") and "{" in path for method in operations]
    # Routes for the deployment's operator alone answer 403 to every organization's people, whatever id is named;
    # that the operator cannot reach across organizations with them is tested with the ledger (tests/test_billing.py).
    addressed = [(method, path) for method, path in addressed if accounts.required_role(method, path) != accounts.PLATFORM]
    assert len(addressed) > 30  # the sweep really covers the API

    answers = {}
    for method, path in sorted(addressed):
        url = re.sub(r"\{(\w+)\}", lambda m: str(two["ids"][m.group(1)]), path)
        response = client.request(method, url, headers=two["bolt"], **VALID.get((method, path), {}))
        answers[(method, path)] = response.status_code
        for marker in ACME_MARKERS[1:]:  # the campaign name itself is what Bolt typed; a 404 may repeat it
            assert marker not in response.text, f"{method} {path} showed Bolt something of Acme's: {marker}"

    # A month names nothing of anyone's: Bolt asking for this month gets its own statement, which is empty (checked for markers above, and in tests/test_billing.py).
    assert answers.pop(("GET", "/api/usage/statements/{period}")) == 200
    # Someone else's campaign, call, job, person, request or document does not exist as far as Bolt can tell.
    assert {route: status for route, status in answers.items() if status != 404} == {}
    assert snapshot(two["acme_id"]) == before  # and nothing of Acme's changed

    # The same sweep by Acme itself finds its own things (so the 404s above are about ownership, not about broken URLs).
    own = {}
    for method, path in sorted(addressed):
        if method != "GET":
            continue
        url = re.sub(r"\{(\w+)\}", lambda m: str(two["ids"][m.group(1)]), path)
        own[path] = client.get(url, headers=two["acme"], **VALID.get((method, path), {})).status_code
    assert {path: status for path, status in own.items() if status != 200} == {"/api/campaigns/{name}/experiments/{experiment_id}": 404, "/api/campaigns/{name}/train": 404,
                                                                                "/api/auth/invitations/{token}": 404}


def test_lists_hold_only_the_callers_organization(client, two):
    acme, bolt = two["acme"], two["bolt"]
    assert client.post("/api/campaign-briefs", json={**BRIEF, "company_name": "Bolt", "name": "Q4"}, headers=bolt).status_code == 201  # the same name as Acme's: allowed

    assert [c["name"] for c in client.get("/api/campaigns", headers=bolt).json()] == ["Q4"]
    assert sorted(c["name"] for c in client.get("/api/campaigns", headers=acme).json()) == ["Acme only", "Q4"]
    assert client.get("/api/campaigns/Q4", headers=bolt).json()["id"] != client.get("/api/campaigns/Q4", headers=acme).json()["id"]

    assert [u["email"] for u in client.get("/api/users", headers=bolt).json()] == ["admin@bolt.example"]
    assert [u["email"] for u in client.get("/api/users", headers=acme).json()] == ["admin@acme.example", "member@acme.example"]

    assert client.get("/api/jobs", headers=bolt).json() == {"stats": {"queued": 0, "running": 0, "done": 0, "dead": 0, "oldest_due_seconds": 0.0}, "jobs": []}
    assert client.get("/api/jobs", headers=acme).json()["stats"]["dead"] == 1

    assert client.get("/api/requests", headers=bolt).json() == {"total": 0, "new": 0, "requests": []}
    assert client.get("/api/requests", headers=acme).json()["total"] == 1

    assert sum(day["calls"] for day in client.get("/api/timeline", headers=bolt).json()["days"]) == 0
    assert sum(day["calls"] for day in client.get("/api/timeline", headers=acme).json()["days"]) == 1

    assert client.get("/api/events", headers=acme).json() != [] and {e["type"] for e in client.get("/api/events", headers=bolt).json()} <= {"PlaybookApproved"}  # only its own
    for path in ("/api/campaigns", "/api/users", "/api/jobs", "/api/requests", "/api/timeline", "/api/events", "/api/auth/me", "/api/status"):
        text = client.get(path, headers=bolt).text
        assert not any(marker in text for marker in ACME_MARKERS), path


def test_the_organization_comes_from_the_account_never_from_the_request(client, two):
    acme, bolt, bolt_id = two["acme"], two["bolt"], two["bolt_id"]
    # A body field or the operator's header naming another organization changes nothing for a signed-in person.
    smuggled = {**bolt, "X-DevyTalk-Organization": "Acme"}
    assert client.get("/api/campaigns", headers=smuggled).json() == []
    assert client.get("/api/campaigns/Acme only", headers=smuggled).status_code == 404
    created = client.post("/api/users", json={"email": "new@bolt.example", "role": "viewer", "password": PASSWORD, "organization_id": two["acme_id"]}, headers=bolt)
    assert created.status_code == 201
    with session_scope() as s:
        assert s.execute(select(User.organization_id).where(User.email == "new@bolt.example")).scalar_one() == bolt_id
    assert client.post("/api/campaign-briefs", json={**BRIEF, "name": "Mine", "organization_id": two["acme_id"]}, headers=bolt).status_code == 201
    with session_scope() as s:
        assert s.execute(select(Campaign.organization_id).where(Campaign.name == "Mine")).scalar_one() == bolt_id

    me = client.get("/api/auth/me", headers=bolt).json()
    assert (me["organization"], me["platform"]) == ({"id": bolt_id, "name": "Bolt"}, False)
    assert client.get("/api/auth/me", headers=acme).json()["organization"]["name"] == "Acme"


def test_only_the_operator_manages_organizations_and_can_choose_one_to_act_in(client, two):
    for headers in (two["acme"], two["bolt"]):
        refused = client.get("/api/organizations", headers=headers)
        assert refused.status_code == 403 and "only the operator" in refused.json()["detail"]
        assert client.post("/api/organizations", json={"name": "Third", "admin_email": "x@third.example", "admin_password": PASSWORD}, headers=headers).status_code == 403

    listed = client.get("/api/organizations").json()
    assert [(o["name"], o["users"]) for o in listed] == [("default", 0), ("Acme", 2), ("Bolt", 1)]
    assert client.post("/api/organizations", json={"name": "Acme", "admin_email": "other@acme.example", "admin_password": PASSWORD}).status_code == 409
    assert client.post("/api/organizations", json={"name": "Weak", "admin_email": "w@weak.example", "admin_password": "short"}).status_code == 400
    assert [o["name"] for o in client.get("/api/organizations").json()] == ["default", "Acme", "Bolt"]  # a refused admin creates no organization

    # Without naming one, the operator works in the default organization, which is empty here.
    assert client.get("/api/campaigns").json() == []
    assert sorted(c["name"] for c in client.get("/api/campaigns", headers={"X-DevyTalk-Organization": "Acme"}).json()) == ["Acme only", "Q4"]
    assert client.get("/api/campaigns", headers={"X-DevyTalk-Organization": str(two["bolt_id"])}).json() == []
    assert client.get("/api/campaigns", headers={"X-DevyTalk-Organization": "No such"}).status_code == 404
    assert client.get("/api/auth/me", headers={"X-DevyTalk-Organization": "Acme"}).json()["organization"]["name"] == "Acme"


def test_an_opt_out_belongs_to_the_organization_that_was_asked(client, two):
    acme, bolt, acme_id = two["acme"], two["bolt"], two["acme_id"]
    client.post("/api/campaign-briefs", json={**BRIEF, "name": "Bolt leads"}, headers=bolt)
    with session_scope() as s:
        assert db.add_dnc(s, "+14155550150", acme_id, "asked Acme to stop") is True
        assert db.add_dnc(s, "+14155550150", acme_id) is False  # once per organization

    sheet = {"file": ("leads.csv", io.BytesIO(b"Phone\n+14155550150\n"), "text/csv")}
    assert client.post("/api/campaigns/Acme only/leads", files=sheet, headers=acme).json() == {"imported": 0, "with_consent": 0, "invalid_phone": 0, "duplicates": 0, "dnc": 1, "column_map": {"phone": "Phone"}, "samples_invalid": []}
    sheet = {"file": ("leads.csv", io.BytesIO(b"Phone\n+14155550150\n"), "text/csv")}
    theirs = client.post("/api/campaigns/Bolt leads/leads", files=sheet, headers=bolt).json()
    assert (theirs["imported"], theirs["dnc"]) == (1, 0)  # Bolt was never asked, and learns nothing about Acme's list
    with session_scope() as s:
        assert db.is_dnc(s, "+14155550150", acme_id) and not db.is_dnc(s, "+14155550150", two["bolt_id"])


def test_every_new_row_is_filed_under_its_campaigns_organization(client, two):
    acme_id = two["acme_id"]
    client.post("/api/campaigns/Acme only/inbound", params={"number": "+14155550123"}, headers=two["acme"])
    assert "<Stream" in client.post("/twilio/inbound", data={"CallSid": "CAin1", "From": "+14155550777", "To": "+14155550123"}).text  # a caller nobody knew
    with session_scope() as s:
        campaign = db.get_campaign_by_name(s, "Acme only", acme_id)
        lead = s.execute(select(Lead).where(Lead.campaign_id == campaign.id, Lead.phone_e164 == "+14155550102")).scalar_one()
        run_id, _ = claim_lead_for_call(s, campaign.id, lead.id)
        run = s.get(CallRun, run_id)
        lifecycle.record_usage(s, run, 61, 0.12)
        job = jobs.enqueue(s, "call_review", "call-review:new", {}, call_run_id=run_id)
        s.flush()
        assert (lead.organization_id, run.organization_id, job.organization_id) == (acme_id, acme_id, acme_id)
        assert s.execute(select(UsageRecord.organization_id).where(UsageRecord.call_run_id == run_id)).scalar_one() == acme_id
        inbound = s.execute(select(CallRun).where(CallRun.twilio_call_sid == "CAin1")).scalar_one()
        assert inbound.organization_id == acme_id and s.get(Lead, inbound.lead_id).organization_id == acme_id
        for model in (Lead, CallRun, Appointment, UsageRecord, Campaign):
            assert s.execute(select(func.count()).select_from(model).where(model.organization_id.is_(None))).scalar_one() == 0, model.__name__

    # The 409 for a number in use names a campaign only to its own organization.
    client.post("/api/campaign-briefs", json={**BRIEF, "name": "Bolt inbound"}, headers=two["bolt"])
    conflict = client.post("/api/campaigns/Bolt inbound/inbound", params={"number": "+14155550123"}, headers=two["bolt"])
    assert conflict.status_code == 409 and "Acme" not in conflict.text and "another campaign" in conflict.text


def test_website_requests_go_to_the_operators_organization_and_stay_there(client, two):
    client.post("/api/intake/requests", headers={"Authorization": "Bearer website-intake-token"}, json={"kind": "demo", "email": "lead@prospect.example", "name": "Prospect"})
    assert client.get("/api/requests").json()["total"] == 1  # the operator's own inbox
    for headers in (two["acme"], two["bolt"]):
        assert "prospect.example" not in client.get("/api/requests", headers=headers).text


def test_a_lookup_outside_a_request_fails_closed():
    from callagent import apideps

    with pytest.raises(RuntimeError, match="no organization in context"):
        apideps.organization_id()


def test_the_status_route_says_whether_the_database_enforces_isolation(client):
    expected = "enforced" if client.backend == "postgres" else "unavailable"
    assert client.get("/api/status").json()["row_level_security"] == expected
