"""Telling an organization's own systems what happened: which endpoints an event
goes to, what they receive, how it is signed, what happens when they do not answer,
and where this server refuses to send. The receiving end here is a stand-in: nothing
is sent over a network."""

import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from callagent import db, events, jobs, outbound, webhooks
from callagent.config import get_settings, resolve_api_token
from callagent.db import CallRun, Job, Lead, WebhookDelivery, WebhookEndpoint, session_scope
from callagent.dialer.campaign_runner import apply_twilio_status, claim_lead_for_call
from conftest import POSTGRES_URL, postgres_app_url

PASSWORD = "correct horse battery"
BRIEF = {"company_name": "Acme", "offer_summary": "A demo.", "agent_name": "Sam", "language": "en"}
HOOK = "https://hooks.bolt.example/devytalk"


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def client(request, tmp_path, monkeypatch):
    url = postgres_app_url() if request.param == "postgres" else f"sqlite:///{tmp_path / 'hooks.db'}"
    for name, value in {"TWILIO_VALIDATE_SIGNATURE": "false", "PUBLIC_BASE_URL": "https://engine.example.com", "DATABASE_URL": url, "DATA_DIR": str(tmp_path / "data"),
                        "TWILIO_FROM_NUMBER": "+14155550000", "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "provider-token", "ANTHROPIC_API_KEY": "x",
                        "DEEPGRAM_API_KEY": "x", "ELEVENLABS_API_KEY": "x", "ELEVENLABS_VOICE_ID": "x"}.items():
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


def organization(client, name: str, number: str) -> SimpleNamespace:
    """A customer with an admin, a caller number, a campaign and one person to call."""
    slug = name.lower()
    created = client.post("/api/organizations", json={"name": name, "admin_email": f"boss@{slug}.example", "admin_password": PASSWORD})
    assert created.status_code == 201, created.text
    assert client.put(f"/api/organizations/{created.json()['id']}/telephony", json={"from_numbers": [number]}).status_code == 200
    admin = sign_in(client, f"boss@{slug}.example")
    assert client.post("/api/campaign-briefs", json={**BRIEF, "company_name": name, "name": f"{name} outreach"}, headers=admin).status_code == 201
    with session_scope() as s:
        campaign = db.get_campaign_by_name(s, f"{name} outreach", created.json()["id"])
        lead = Lead(campaign_id=campaign.id, phone_e164=number[:-2] + "99", first_name="Mina", company="Harbor Cafe", status="pending")
        s.add(lead)
        s.flush()
        return SimpleNamespace(id=created.json()["id"], admin=admin, campaign=campaign.id, lead=lead.id)


@pytest.fixture
def bolt(client):
    return organization(client, "Bolt", "+14155550210")


class Receiver:
    """The customer's endpoint: records what it was sent and answers as told."""

    def __init__(self, monkeypatch, answers=()):
        self.answers, self.requests = list(answers), []

        async def post(url, body, headers, *, timeout, public_only=True):
            self.requests.append(SimpleNamespace(url=url, body=body, headers=headers, public_only=public_only))
            answer = self.answers.pop(0) if self.answers else 200
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(outbound, "post", post)


def add_endpoint(client, who, events_wanted, url=HOOK) -> dict:
    made = client.post("/api/webhooks", headers=who.admin, json={"url": url, "events": events_wanted, "description": "CRM"})
    assert made.status_code == 201, made.text
    return made.json()


def a_finished_call(who) -> int:
    """A call that was placed and ended, as the phone provider reports it."""
    with session_scope() as s:
        run_id, _phone = claim_lead_for_call(s, who.campaign, who.lead)
        apply_twilio_status(s, run_id, {"CallSid": f"CA-{run_id}", "CallStatus": "completed", "CallDuration": "75"}, get_settings())
        return run_id


def deliveries(endpoint_id: int) -> list[dict]:
    with session_scope() as s:
        return [webhooks.describe_delivery(d) for d in s.execute(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id).order_by(WebhookDelivery.id)).scalars()]


# ----------------------------------------------------------------------------- signing


def test_a_signature_holds_for_the_exact_bytes_and_for_a_few_minutes_only():
    body, secret = b'{"id":"evt_1","type":"call.finalized"}', "whsec_test"
    header = webhooks.sign(secret, 1_700_000_000, body)
    assert header.startswith("t=1700000000,v1=")
    assert webhooks.verify(secret, body, header, now=1_700_000_100)
    assert not webhooks.verify(secret, body + b" ", header, now=1_700_000_100)  # one byte more
    assert not webhooks.verify("whsec_other", body, header, now=1_700_000_100)
    assert not webhooks.verify(secret, body, header, now=1_700_000_000 + 301)  # a recording of a real delivery, sent again later
    assert not webhooks.verify(secret, body, header.replace("t=1700000000", "t=1700000200"), now=1_700_000_200)  # the time is signed too
    for nonsense in ("", "v1=abc", "t=soon,v1=abc"):
        assert not webhooks.verify(secret, body, nonsense, now=1_700_000_000)


# ----------------------------------------------------------------------------- registering


def test_only_a_public_https_address_and_known_events_can_be_registered(client, bolt):
    for url in ("http://hooks.bolt.example/x", "https://localhost/x", "https://10.0.0.5/x", "https://169.254.169.254/latest/meta-data", "https://[::1]/x",
                "https://user:pw@hooks.bolt.example/x", "https://hooks.bolt.example:8443/x", "https://intranet/x", "https://db.internal/x", "ftp://hooks.bolt.example"):
        refused = client.post("/api/webhooks", headers=bolt.admin, json={"url": url, "events": ["*"]})
        assert refused.status_code == 400, url
    assert "unknown event 'call.imagined'" in client.post("/api/webhooks", headers=bolt.admin, json={"url": HOOK, "events": ["call.imagined"]}).json()["detail"]
    assert client.post("/api/webhooks", headers=bolt.admin, json={"url": HOOK, "events": []}).status_code == 400

    made = add_endpoint(client, bolt, ["appointment.booked", "call.finalized", "call.finalized"])
    assert made["secret"].startswith("whsec_") and made["events"] == ["appointment.booked", "call.finalized"] and made["active"] is True
    listed = client.get("/api/webhooks", headers=bolt.admin)
    assert made["secret"] not in listed.text and listed.json()["endpoints"][0]["secret_ends_with"] == made["secret"][-4:]
    assert "call.finalized" in listed.json()["events"] and "appointment.booked" in listed.json()["events"]

    # A new secret replaces the old one, and is shown once.
    rotated = client.post(f"/api/webhooks/{made['id']}/secret", headers=bolt.admin).json()
    assert rotated["secret"].startswith("whsec_") and rotated["secret"] != made["secret"]
    with session_scope() as s:
        assert s.get(WebhookEndpoint, made["id"]).secret == rotated["secret"]

    for _ in range(webhooks.MAX_ENDPOINTS - 1):
        add_endpoint(client, bolt, ["*"])
    assert "remove one first" in client.post("/api/webhooks", headers=bolt.admin, json={"url": HOOK, "events": ["*"]}).json()["detail"]


def test_endpoints_are_for_admins_and_never_for_an_api_key(client, bolt):
    endpoint = add_endpoint(client, bolt, ["*"])
    client.post("/api/users", headers=bolt.admin, json={"email": "ops@bolt.example", "name": "Ops", "role": "operator", "password": PASSWORD})
    operator = sign_in(client, "ops@bolt.example")
    key = client.post("/api/keys", headers=bolt.admin, json={"name": "CRM sync", "role": "admin"}).json()["key"]
    for who in (operator, {"Authorization": f"Bearer {key}"}):
        assert client.get("/api/webhooks", headers=who).status_code == 403
        assert client.post("/api/webhooks", headers=who, json={"url": "https://elsewhere.example/tap", "events": ["*"]}).status_code == 403
        assert client.delete(f"/api/webhooks/{endpoint['id']}", headers=who).status_code == 403
    assert len(client.get("/api/webhooks", headers=bolt.admin).json()["endpoints"]) == 1


# ----------------------------------------------------------------------------- what is sent, and to whom


@pytest.mark.asyncio
async def test_an_event_goes_once_signed_to_the_endpoints_of_its_organization_that_asked_for_it(client, bolt, monkeypatch):
    cobalt = organization(client, "Cobalt", "+14155550410")
    calls = add_endpoint(client, bolt, ["call.completed"])
    bookings = add_endpoint(client, bolt, ["appointment.booked"], url="https://hooks.bolt.example/bookings")
    everything_at_cobalt = add_endpoint(client, cobalt, ["*"], url="https://hooks.cobalt.example/all")
    receiver = Receiver(monkeypatch)

    run_id = a_finished_call(bolt)
    assert [d["event"] for d in deliveries(calls["id"])] == ["call.completed"]  # call.initiated happened too; it was not asked for
    assert deliveries(bookings["id"]) == [] and deliveries(everything_at_cobalt["id"]) == []  # another kind; another organization

    # The provider reports the end of the call a second time: the same fact, no second delivery.
    with session_scope() as s:
        apply_twilio_status(s, run_id, {"CallSid": f"CA-{run_id}", "CallStatus": "completed", "CallDuration": "75"}, get_settings())
    (delivery,) = deliveries(calls["id"])
    assert delivery["status"] == "pending" and delivery["attempts"] == 0
    with session_scope() as s:
        job = s.execute(select(Job).where(Job.key == webhooks.delivery_job_key(delivery["id"]))).scalar_one()
        assert (job.kind, job.queue, job.organization_id, job.call_run_id) == ("webhook_delivery", "integrations", bolt.id, run_id)

    await webhooks.deliver({"delivery_id": delivery["id"]})
    (request,) = receiver.requests
    assert request.url == HOOK and request.public_only is True
    assert webhooks.verify(calls["secret"], request.body, request.headers["X-Callagent-Signature"])
    assert not webhooks.verify(bookings["secret"], request.body, request.headers["X-Callagent-Signature"])  # each endpoint has its own secret
    assert request.headers["X-Callagent-Event"] == "call.completed" and request.headers["X-Callagent-Delivery"] == f"whd_{delivery['id']}"
    body = json.loads(request.body)
    assert body["type"] == "call.completed" and body["id"] == delivery["event_id"] and body["organization_id"] == bolt.id and body["api_version"] == 1
    assert body["campaign"]["name"] == "Bolt outreach" and body["data"]["seconds"] == 75
    assert (body["call"]["id"], body["call"]["status"], body["call"]["duration_seconds"], body["call"]["from_number"]) == (run_id, "completed", 75, "+14155550210")
    assert (body["lead"]["phone"], body["lead"]["first_name"], body["lead"]["company"]) == ("+14155550299", "Mina", "Harbor Cafe")
    assert "transcript" not in body["call"]  # what was said is read from the API by a system that may, not pushed

    (delivered,) = client.get(f"/api/webhooks/{calls['id']}/deliveries", headers=bolt.admin).json()
    assert (delivered["status"], delivered["attempts"], delivered["response_status"], delivered["error"]) == ("delivered", 1, 200, None)
    assert client.get("/api/webhooks", headers=bolt.admin).json()["endpoints"][0]["last_status"] == 200

    # Run again (the queue delivers at least once): nothing is sent a second time.
    await webhooks.deliver({"delivery_id": delivery["id"]})
    assert len(receiver.requests) == 1


@pytest.mark.asyncio
async def test_a_booked_meeting_is_sent_with_the_meeting(client, bolt, monkeypatch):
    endpoint = add_endpoint(client, bolt, ["appointment.booked"])
    receiver = Receiver(monkeypatch)
    with session_scope() as s:
        run_id, _phone = claim_lead_for_call(s, bolt.campaign, bolt.lead)
        appointment = db.Appointment(organization_id=bolt.id, lead_id=bolt.lead, call_run_id=run_id, starts_at="2026-12-01T15:00:00+00:00", timezone="Europe/Berlin", notes="Wants pricing first")
        s.add(appointment)
        s.flush()
        events.emit(s, "AppointmentBooked", key=f"appointment:{appointment.id}:booked", subject="appointment", subject_id=appointment.id, organization_id=bolt.id,
                    campaign_id=bolt.campaign, call_run_id=run_id, payload={"starts_at": appointment.starts_at, "lead_id": bolt.lead})
    (delivery,) = deliveries(endpoint["id"])
    await webhooks.deliver({"delivery_id": delivery["id"]})
    body = json.loads(receiver.requests[0].body)
    assert body["type"] == "appointment.booked" and body["appointment"] == {"id": body["appointment"]["id"], "starts_at": "2026-12-01T15:00:00+00:00", "timezone": "Europe/Berlin",
                                                                            "notes": "Wants pricing first"}
    assert body["lead"]["phone"] == "+14155550299" and body["call"]["id"] == run_id


# ----------------------------------------------------------------------------- when the endpoint does not answer


@pytest.mark.asyncio
async def test_a_delivery_that_fails_is_tried_again_and_an_endpoint_that_keeps_failing_is_switched_off(client, bolt, monkeypatch):
    monkeypatch.setattr(webhooks, "DISABLE_AFTER", 2)
    endpoint = add_endpoint(client, bolt, ["call.completed"])
    receiver = Receiver(monkeypatch, [500, outbound.DeliveryError("no answer within 10 seconds"), 302])

    a_finished_call(bolt)
    (first,) = deliveries(endpoint["id"])
    for expected in ("the endpoint answered 500", "no answer within 10 seconds", "the endpoint answered 302; redirects are not followed"):
        with pytest.raises(RuntimeError, match=expected):  # raising is what makes the queue wait and try again
            await webhooks.deliver({"delivery_id": first["id"]})
    (first,) = deliveries(endpoint["id"])
    assert (first["status"], first["attempts"], first["response_status"]) == ("pending", 3, 302)
    assert {r.headers["X-Callagent-Delivery"] for r in receiver.requests} == {f"whd_{first['id']}"}  # the same id every time: a receiver can ignore repeats

    # The queue gives up on it. One failed delivery does not switch an endpoint off; two in a row do (fifteen outside this test).
    await webhooks._given_up({"delivery_id": first["id"]}, "the endpoint answered 302")
    assert deliveries(endpoint["id"])[0]["status"] == "failed"
    assert client.get("/api/webhooks", headers=bolt.admin).json()["endpoints"][0]["active"] is True
    with session_scope() as s:
        second = WebhookDelivery(organization_id=bolt.id, endpoint_id=endpoint["id"], event_id=None, event_type="call.completed", error="the endpoint answered 500")
        s.add(second)
        s.flush()
        second_id = second.id
    await webhooks._given_up({"delivery_id": second_id}, "the endpoint answered 500")
    described = client.get("/api/webhooks", headers=bolt.admin).json()["endpoints"][0]
    assert described["active"] is False and "2 deliveries in a row failed" in described["disabled_reason"] and "answered 500" in described["disabled_reason"]

    # Switched off, it is sent nothing, and nothing piles up for it.
    with session_scope() as s:
        events.emit(s, "CallCompleted", key="call:another:completed", subject="call", organization_id=bolt.id, campaign_id=bolt.campaign, payload={})
    assert len(deliveries(endpoint["id"])) == 2

    # An admin switches it on again: the count starts from nothing, and what was given up on can be sent again.
    on = client.patch(f"/api/webhooks/{endpoint['id']}", headers=bolt.admin, json={"active": True}).json()
    assert on["active"] is True and on["failures_in_a_row"] == 0 and on["disabled_reason"] is None
    again = client.post(f"/api/webhooks/{endpoint['id']}/deliveries/{first['id']}/redeliver", headers=bolt.admin)
    assert again.status_code == 200 and again.json()["status"] == "pending"
    with session_scope() as s:
        assert s.execute(select(Job).where(Job.key == webhooks.delivery_job_key(first["id"], 1))).scalar_one().status == "queued"
    assert client.post(f"/api/webhooks/{endpoint['id']}/deliveries/{first['id']}/redeliver", headers=bolt.admin).status_code == 400  # already on its way
    await webhooks.deliver({"delivery_id": first["id"]})
    done = deliveries(endpoint["id"])[0]
    assert (done["status"], done["attempts"], done["response_status"]) == ("delivered", 4, 200)
    assert client.get("/api/webhooks", headers=bolt.admin).json()["endpoints"][0]["failures_in_a_row"] == 0


def test_the_queue_gives_a_delivery_about_an_hour(client):
    waits = [jobs.BACKOFF_BASE_SECONDS * 2 ** attempt for attempt in range(webhooks.MAX_ATTEMPTS - 1)]
    assert 3600 <= sum(waits) <= 2 * 3600
    assert jobs.QUEUES["integrations"] >= 1 and jobs._queue_of[webhooks.DELIVERY] == "integrations"  # a slow endpoint cannot take the slots calls depend on


@pytest.mark.asyncio
async def test_a_test_delivery_says_how_it_went(client, bolt, monkeypatch):
    endpoint = add_endpoint(client, bolt, ["call.completed"])
    receiver = Receiver(monkeypatch, [200, outbound.DeliveryError("'hooks.bolt.example' is not a public website address")])
    arrived = client.post(f"/api/webhooks/{endpoint['id']}/test", headers=bolt.admin).json()
    assert (arrived["event"], arrived["status"], arrived["response_status"]) == ("ping", "delivered", 200)
    body = json.loads(receiver.requests[0].body)
    assert body["type"] == "ping" and webhooks.verify(endpoint["secret"], receiver.requests[0].body, receiver.requests[0].headers["X-Callagent-Signature"])
    refused = client.post(f"/api/webhooks/{endpoint['id']}/test", headers=bolt.admin).json()
    assert refused["status"] == "failed" and "not a public" in refused["error"]
    assert client.post("/api/webhooks/9999/test", headers=bolt.admin).status_code == 404
    # Removing the endpoint removes the record of its deliveries.
    assert client.delete(f"/api/webhooks/{endpoint['id']}", headers=bolt.admin).status_code == 200
    with session_scope() as s:
        assert s.execute(select(WebhookDelivery)).scalars().all() == []
    await webhooks.deliver({"delivery_id": arrived["id"]})  # a job still queued for it finds nothing to do
    assert len(receiver.requests) == 2


# ----------------------------------------------------------------------------- where this server refuses to send


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "fd00::5"])
async def test_a_name_that_resolves_inside_the_network_is_never_connected_to(monkeypatch, inside):
    family = socket.AF_INET6 if ":" in inside else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(family, socket.SOCK_STREAM, 6, "", (inside, 0))])

    class Client:
        def __init__(self, *a, **k):
            raise AssertionError("a connection was attempted")

    monkeypatch.setattr(outbound.httpx, "AsyncClient", Client)
    with pytest.raises(outbound.DeliveryError, match="not a public"):
        await outbound.post("https://looks-harmless.example/hook", b"{}", {}, timeout=5)


@pytest.mark.asyncio
async def test_a_public_name_is_connected_to_at_the_address_that_was_checked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    seen = {}

    class Client:
        def __init__(self, **options):
            seen["follow_redirects"] = options.get("follow_redirects")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, content, headers, extensions=None):
            seen.update(url=url, host=headers["Host"], sni=(extensions or {}).get("sni_hostname"))
            return httpx.Response(204)

    monkeypatch.setattr(outbound.httpx, "AsyncClient", Client)
    assert await outbound.post("https://hooks.bolt.example/devytalk", b"{}", {}, timeout=5) == 204
    # The name is not looked up a second time: a second answer cannot point somewhere else.
    assert seen == {"follow_redirects": False, "url": "https://93.184.216.34/devytalk", "host": "hooks.bolt.example", "sni": "hooks.bolt.example"}


def test_nothing_is_queued_or_stored_for_an_organization_that_registered_nothing(client, bolt):
    a_finished_call(bolt)
    with session_scope() as s:
        assert s.execute(select(WebhookDelivery)).scalars().all() == []
        assert s.execute(select(Job).where(Job.kind == webhooks.DELIVERY)).scalars().all() == []
        assert s.execute(select(CallRun)).scalars().first() is not None
