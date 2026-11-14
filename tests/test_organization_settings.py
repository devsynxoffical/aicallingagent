"""What belongs to one organization and not to the deployment: the numbers it calls
from, where calls are transferred, where bookings are reported, its rates, and the API
keys its own systems use. The phone provider here is a stand-in."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from callagent import accounts, background, db, orgsettings
from callagent.config import get_settings, resolve_api_token
from callagent.db import ApiKey, AuditEntry, CallRun, Campaign, Lead, Organization, UsageRecord, session_scope
from callagent.dialer import reconcile, twilio_client
from callagent.dialer.campaign_runner import CampaignRunner, apply_twilio_status, call_settings, claim_lead_for_call
from conftest import POSTGRES_URL, postgres_app_url

PASSWORD = "correct horse battery"
BRIEF = {"company_name": "Acme", "offer_summary": "A demo.", "agent_name": "Sam", "language": "en"}
DEPLOYMENT_NUMBER, DEPLOYMENT_HOOK = "+14155550000", "https://first-customer.example/bookings"


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def client(request, tmp_path, monkeypatch):
    url = postgres_app_url() if request.param == "postgres" else f"sqlite:///{tmp_path / 'org.db'}"
    for name, value in {"TWILIO_VALIDATE_SIGNATURE": "false", "PUBLIC_BASE_URL": "https://engine.example.com", "DATABASE_URL": url, "DATA_DIR": str(tmp_path / "data"),
                        # The deployment was set up for its first customer: these are the default organization's.
                        "TWILIO_FROM_NUMBER": DEPLOYMENT_NUMBER, "HUMAN_TRANSFER_NUMBER": "+14155550001", "BOOKING_WEBHOOK_URL": DEPLOYMENT_HOOK,
                        "BOOKING_WEBHOOK_SECRET": "first-customer-secret", "COST_PER_CONNECTED_MINUTE_USD": "0.10", "COST_PER_CALL_ATTEMPT_USD": "0.02",
                        "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "provider-token", "ANTHROPIC_API_KEY": "x", "DEEPGRAM_API_KEY": "x",
                        "ELEVENLABS_API_KEY": "x", "ELEVENLABS_VOICE_ID": "x"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    db.reset_engine_for_tests(url)
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


def sign_in(client, email):
    token = client.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers={"Authorization": ""}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bolt(client):
    """A second customer on the same deployment, with a campaign and a lead."""
    created = client.post("/api/organizations", json={"name": "Bolt", "admin_email": "boss@bolt.example", "admin_password": PASSWORD})
    assert created.status_code == 201, created.text
    admin = sign_in(client, "boss@bolt.example")
    assert client.post("/api/campaign-briefs", json={**BRIEF, "company_name": "Bolt", "name": "Bolt outreach"}, headers=admin).status_code == 201
    with session_scope() as s:
        campaign = db.get_campaign_by_name(s, "Bolt outreach", created.json()["id"])
        lead = Lead(campaign_id=campaign.id, phone_e164="+14155550201", status="pending")
        s.add(lead)
        s.flush()
        return SimpleNamespace(id=created.json()["id"], admin=admin, campaign=campaign.id, lead=lead.id)


def default_campaign(client) -> SimpleNamespace:
    assert client.post("/api/campaign-briefs", json={**BRIEF, "name": "First customer"}).status_code == 201
    with session_scope() as s:
        campaign = s.execute(select(Campaign).where(Campaign.name == "First customer")).scalar_one()
        lead = Lead(campaign_id=campaign.id, phone_e164="+14155550301", status="pending")
        s.add(lead)
        s.flush()
        return SimpleNamespace(campaign=campaign.id, lead=lead.id, org=campaign.organization_id)


# ----------------------------------------------------------------------------- a second customer borrows nothing (audit A-11)


def test_a_new_organization_has_no_number_webhook_or_transfer_of_the_first_customers(client, bolt):
    settings = client.get("/api/organization/settings", headers=bolt.admin).json()
    assert settings["from_numbers"] == [] and settings["can_place_calls"] is False
    assert settings["human_transfer_number"] is None and settings["booking_webhook_url"] is None and settings["booking_webhook_signed"] is False
    assert settings["cost_per_connected_minute_usd"] == 0.10  # the operator's rates apply to everyone unless one is given its own

    # Nothing of the first customer's is visible, and no call can go out under their number.
    assert DEPLOYMENT_NUMBER not in str(settings) and "first-customer" not in str(settings)
    status = client.get("/api/status", headers=bolt.admin).json()
    assert status["from_number"] is None and status["ready_for_calls"] is False
    refused = client.post("/api/campaigns/Bolt outreach/start", headers=bolt.admin)
    assert refused.status_code == 400 and "no phone number to call from yet" in refused.json()["detail"]
    assert client.post("/api/campaigns/Bolt outreach/dial", params={"phone": "+14155550202"}, headers=bolt.admin).status_code == 400

    # The first customer is as before: the deployment's settings are theirs.
    mine = client.get("/api/organization/settings").json()
    assert mine["from_numbers"] == [DEPLOYMENT_NUMBER] and mine["booking_webhook_url"] == DEPLOYMENT_HOOK and mine["booking_webhook_signed"] is True
    assert client.get("/api/status").json()["from_number"] == DEPLOYMENT_NUMBER


def test_only_the_operator_assigns_numbers_and_a_number_belongs_to_one_organization(client, bolt):
    numbers = {"from_numbers": ["+1 (415) 555-0210", "+14155550211", "+14155550210"]}
    assert client.put(f"/api/organizations/{bolt.id}/telephony", json=numbers, headers=bolt.admin).status_code == 403  # typing your own caller id is spoofing
    assigned = client.put(f"/api/organizations/{bolt.id}/telephony", json=numbers)
    assert assigned.status_code == 200 and assigned.json()["settings"]["from_numbers"] == ["+14155550210", "+14155550211"]  # cleaned, once each
    assert client.get("/api/organization/settings", headers=bolt.admin).json()["can_place_calls"] is True

    other = client.post("/api/organizations", json={"name": "Cobalt", "admin_email": "boss@cobalt.example", "admin_password": PASSWORD}).json()
    taken = client.put(f"/api/organizations/{other['id']}/telephony", json={"from_numbers": ["+14155550211"]})
    assert taken.status_code == 409 and "already assigned to another organization" in taken.json()["detail"]
    assert client.put(f"/api/organizations/{other['id']}/telephony", json={"from_numbers": ["0612345678"]}).status_code == 400
    assert client.put("/api/organizations/99999/telephony", json=numbers).status_code == 404


async def test_each_organizations_calls_go_out_from_its_own_numbers(client, bolt, monkeypatch):
    client.put(f"/api/organizations/{bolt.id}/telephony", json={"from_numbers": ["+14155550210", "+14155550211"]})
    first = default_campaign(client)
    with session_scope() as s:
        more = [Lead(campaign_id=bolt.campaign, phone_e164=f"+1415555022{i}", status="pending") for i in range(4)]
        s.add_all(more)
        s.flush()
        bolt_leads = [bolt.lead, *[lead.id for lead in more]]

    placed = []

    async def place(call_run_id, phone, settings):
        placed.append((phone, settings.twilio_from_number))
        return f"CA{call_run_id}"

    monkeypatch.setattr(twilio_client, "place_call_async", place)
    runner = CampaignRunner(get_settings())
    for lead_id in bolt_leads:
        await runner.dial_lead(bolt.campaign, lead_id)
    await runner.dial_lead(first.campaign, first.lead)

    from_bolt = {number for phone, number in placed if phone != "+14155550301"}
    assert from_bolt == {"+14155550210", "+14155550211"}  # both of its numbers in use, and only its own
    assert dict(placed)["+14155550301"] == DEPLOYMENT_NUMBER  # the first customer's call from the first customer's number
    with session_scope() as s:
        recorded = {run.lead_id: run.from_number for run in s.execute(select(CallRun)).scalars()}
        assert all(recorded[lead_id] in ("+14155550210", "+14155550211") for lead_id in bolt_leads)
        # The same lead is always called from the same number: someone tried again sees a number they have seen.
        assert orgsettings.number_for(orgsettings.effective(s, bolt.id, get_settings()), bolt.lead) == recorded[bolt.lead]
        # A text, a transfer and a question to the provider about the call use the number it was placed from.
        run_id = s.execute(select(CallRun.id).where(CallRun.lead_id == bolt.lead)).scalar_one()
        assert call_settings(s, run_id, get_settings()).twilio_from_number == recorded[bolt.lead]


async def test_a_quiet_call_is_looked_for_among_its_own_organizations_calls(client, bolt, monkeypatch):
    from datetime import timedelta

    client.put(f"/api/organizations/{bolt.id}/telephony", json={"from_numbers": ["+14155550210"]})
    with session_scope() as s:
        claim_lead_for_call(s, bolt.campaign, bolt.lead)
        run = s.execute(select(CallRun)).scalar_one()
        run.started_at = db.utcnow() - timedelta(minutes=10)  # placed, never confirmed: no provider id
    asked = []
    provider = SimpleNamespace(find_call=lambda phone, since, settings: asked.append((phone, settings.twilio_from_number)) or None, fetch_call=None)
    await reconcile.reconcile_calls(get_settings(), provider=provider)
    assert asked == [("+14155550201", "+14155550210")]  # not among the first customer's calls


def test_an_admin_sets_the_transfer_number_and_the_booking_webhook_and_the_secret_is_never_shown(client, bolt):
    saved = client.put("/api/organization/settings", headers=bolt.admin, json={
        "human_transfer_number": "+14155550299", "booking_webhook_url": "https://bolt.example/hooks/bookings", "booking_webhook_secret": "bolt-signing-secret"})
    assert saved.status_code == 200 and saved.json()["booking_webhook_signed"] is True and "bolt-signing-secret" not in saved.text
    assert "bolt-signing-secret" not in client.get("/api/organization/settings", headers=bolt.admin).text

    # Changing the address without sending the secret keeps the secret.
    client.put("/api/organization/settings", headers=bolt.admin, json={"human_transfer_number": "+14155550299", "booking_webhook_url": "https://bolt.example/v2/bookings"})
    with session_scope() as s:
        effective = orgsettings.effective(s, bolt.id, get_settings())
        assert (effective.booking_webhook_url, effective.booking_webhook_secret, effective.human_transfer_number) == ("https://bolt.example/v2/bookings", "bolt-signing-secret", "+14155550299")

    for bad in ({"booking_webhook_url": "http://bolt.example/plain"}, {"human_transfer_number": "call me"}):
        assert client.put("/api/organization/settings", headers=bolt.admin, json=bad).status_code == 400
    # Removing the webhook removes its secret with it; and none of this is for someone who only looks.
    client.put("/api/organization/settings", headers=bolt.admin, json={"human_transfer_number": None, "booking_webhook_url": None})
    with session_scope() as s:
        assert orgsettings.effective(s, bolt.id, get_settings()).booking_webhook_secret is None
    client.post("/api/users", json={"email": "viewer@bolt.example", "role": "viewer", "password": PASSWORD}, headers=bolt.admin)
    assert client.put("/api/organization/settings", headers=sign_in(client, "viewer@bolt.example"), json={}).status_code == 403


async def test_a_booking_is_reported_to_the_webhook_of_the_organization_it_belongs_to(client, bolt, monkeypatch):
    client.put("/api/organization/settings", headers=bolt.admin, json={"booking_webhook_url": "https://bolt.example/hooks/bookings", "booking_webhook_secret": "bolt-signing-secret"})
    sent = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, content, headers):
            sent.append((url, content, headers))
            return httpx.Response(200)

    monkeypatch.setattr(background.httpx, "AsyncClient", Client)
    body = {"appointment_id": 1}
    await background.deliver_booking_webhook({"delivery_id": "d1", "body": body, "organization_id": bolt.id})
    await background.deliver_booking_webhook({"delivery_id": "d2", "body": body})  # queued before organizations had their own: the first customer's
    with session_scope() as s:
        cobalt, _admin = accounts.create_organization(s, "Cobalt", admin_email="boss@cobalt.example", admin_password=PASSWORD)
        cobalt_id = cobalt.id
    await background.deliver_booking_webhook({"delivery_id": "d3", "body": body, "organization_id": cobalt_id})  # has no webhook: nothing is sent anywhere

    assert [url for url, _content, _headers in sent] == ["https://bolt.example/hooks/bookings", DEPLOYMENT_HOOK]
    (_, content, headers), (_, _, first_headers) = sent
    assert headers["X-Callagent-Signature"] == background.sign("bolt-signing-secret", content)  # signed with Bolt's secret, not the first customer's
    assert first_headers["X-Callagent-Signature"] == background.sign("first-customer-secret", content)


def test_an_organization_with_its_own_rates_is_charged_at_them(client, bolt):
    client.put(f"/api/organizations/{bolt.id}/telephony", json={"from_numbers": ["+14155550210"], "cost_per_connected_minute_usd": 0.25, "cost_per_call_attempt_usd": 0})
    first = default_campaign(client)
    amounts = {}
    for name, campaign, lead in (("bolt", bolt.campaign, bolt.lead), ("first", first.campaign, first.lead)):
        with session_scope() as s:
            run_id, _phone = claim_lead_for_call(s, campaign, lead)
            apply_twilio_status(s, run_id, {"CallSid": f"CA-{name}", "CallStatus": "completed", "CallDuration": "120"}, get_settings())
            amounts[name] = str(s.execute(select(UsageRecord.amount_usd).where(UsageRecord.call_run_id == run_id)).scalar_one())
    assert amounts == {"bolt": "0.5000", "first": "0.2200"}  # two minutes at 0.25; two minutes at 0.10 plus the 0.02 attempt

    client.put(f"/api/organizations/{bolt.id}/telephony", json={"clear_rates": True})
    assert client.get("/api/organization/settings", headers=bolt.admin).json()["cost_per_connected_minute_usd"] == 0.10


# ----------------------------------------------------------------------------- API keys (audit A-10)


def test_a_key_is_shown_once_stored_as_a_hash_and_acts_within_its_organization_and_role(client, bolt):
    created = client.post("/api/keys", json={"name": "CRM sync", "role": "viewer"}, headers=bolt.admin)
    assert created.status_code == 201
    key = created.json()["key"]
    assert key.startswith("dtk_") and created.json()["starts_with"] == key[:12]
    with session_scope() as s:
        stored = s.execute(select(ApiKey)).scalar_one()
        assert key not in (stored.key_hash, stored.prefix, stored.name) and len(stored.key_hash) == 64
    listed = client.get("/api/keys", headers=bolt.admin).json()
    assert [(k["name"], k["role"], k["revoked_at"]) for k in listed] == [("CRM sync", "viewer", None)] and "key" not in listed[0]

    as_key = {"Authorization": f"Bearer {key}"}
    me = client.get("/api/auth/me", headers=as_key).json()
    assert (me["kind"], me["role"], me["organization"]["name"], me["platform"]) == ("key", "viewer", "Bolt", False)
    default_campaign(client)
    assert [c["name"] for c in client.get("/api/campaigns", headers=as_key).json()] == ["Bolt outreach"]  # its organization's, nobody else's
    assert client.get("/api/campaigns/First customer", headers=as_key).status_code == 404
    assert client.put("/api/campaigns/Bolt outreach/rules", json={}, headers=as_key).status_code == 403  # a viewer's key only looks
    assert client.get("/api/keys", headers=bolt.admin).json()[0]["last_used_at"] is not None

    assert client.post("/api/keys", json={"name": "x", "role": "owner"}, headers=bolt.admin).status_code == 400
    client.post("/api/users", json={"email": "op@bolt.example", "role": "operator", "password": PASSWORD}, headers=bolt.admin)
    assert client.post("/api/keys", json={"name": "x"}, headers=sign_in(client, "op@bolt.example")).status_code == 403  # keys are for admins to hand out


def test_a_key_cannot_make_keys_or_manage_people_and_stops_working_when_revoked(client, bolt):
    key = client.post("/api/keys", json={"name": "Automation", "role": "admin"}, headers=bolt.admin).json()
    as_key = {"Authorization": f"Bearer {key['key']}"}
    assert client.put("/api/campaigns/Bolt outreach/rules", json={"budget_usd": 50}, headers=as_key).status_code == 200  # an admin's key can change things
    # But a key that leaks must not be able to keep itself alive or let someone in.
    for method, path, body in (("POST", "/api/keys", {"name": "another"}), ("GET", "/api/keys", None), ("GET", "/api/users", None),
                               ("POST", "/api/users", {"email": "x@bolt.example", "role": "admin", "password": PASSWORD}),
                               ("POST", "/api/auth/password", {"current_password": "a", "new_password": "b" * 12})):
        assert client.request(method, path, json=body, headers=as_key).status_code == 403, (method, path)
    assert client.put(f"/api/organizations/{bolt.id}/telephony", json={"from_numbers": []}, headers=as_key).status_code == 403  # nor is it the operator

    with session_scope() as s:  # what it did is in the audit trail under its name
        entry = s.execute(select(AuditEntry).where(AuditEntry.action == "set_rules")).scalars().one()
        assert (entry.actor, entry.actor_role, entry.organization_id) == ("api key: Automation", "admin", bolt.id)

    revoked = client.delete(f"/api/keys/{key['id']}", headers=bolt.admin)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    gone = client.get("/api/campaigns", headers=as_key)
    assert gone.status_code == 401 and "revoked" in gone.json()["detail"]
    assert client.get("/api/campaigns", headers={"Authorization": "Bearer dtk_not-a-real-key"}).status_code == 401
    assert client.delete("/api/keys/99999", headers=bolt.admin).status_code == 404
