"""Telling an organization's own systems what happened.

An organization registers addresses (endpoints) and chooses which events each one
receives. When an event is recorded, one delivery per subscribed endpoint is created
in the same transaction and queued as a job, so an event that was saved is always
attempted and one that was not is never announced.

**What the receiver can rely on**

- Every delivery is signed with the endpoint's own secret, over the moment of sending
  and the exact bytes of the body (``verify`` below is the check to copy).
- The same event has the same ``id`` and the same delivery id on every retry, so a
  receiver that saw it once can ignore it the second time.
- Deliveries are at least once and in no guaranteed order. The body carries the state
  of the call at the moment of sending, not at the moment of the event.

**What it cannot**

- Exactly-once, or order.
- Delivery to an address that is not public, or behind a redirect.

Anything but a 2xx answer within the time limit is a failure and is retried with a
growing wait for about an hour. An endpoint that fails ``DISABLE_AFTER`` deliveries
in a row is switched off and says why; an admin switches it on again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import jobs, outbound
from .db import Appointment, CallRun, Campaign, Event, Job, Lead, WebhookDelivery, WebhookEndpoint, run_db, utcnow

log = logging.getLogger(__name__)

# What an organization can be told about, under the name it is told by.
PUBLIC_EVENTS: dict[str, str] = {
    "CallInitiated": "call.initiated",
    "CallAnswered": "call.answered",
    "CallCompleted": "call.completed",
    "CallFailed": "call.failed",
    "CallFinalized": "call.finalized",  # the call is over and its outcome is known; reviewed when the model was reachable
    "CallTransferred": "call.transferred",
    "AppointmentBooked": "appointment.booked",
    "CampaignStarted": "campaign.started",
    "CampaignPaused": "campaign.paused",
    "CampaignStopped": "campaign.stopped",
    "PlaybookApproved": "playbook.approved",
}
EVENT_NAMES = tuple(sorted(PUBLIC_EVENTS.values()))
EVERYTHING = "*"
PING = "ping"

DELIVERY = "webhook_delivery"
MAX_ENDPOINTS = 10
MAX_ATTEMPTS = 8  # 30 s, 1 min, 2 min ... about an hour in all
TIMEOUT_SECONDS = 10.0
DISABLE_AFTER = 15
SIGNATURE_TOLERANCE_SECONDS = 300
API_VERSION = 1


class WebhookError(ValueError):
    """The change asked for cannot be made. The message is for the person asking."""


# ----------------------------------------------------------------------------- signing


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The value of X-Callagent-Signature: ``t=<seconds>,v1=<hex>``, where the hex is
    HMAC-SHA256 of ``"<seconds>." + body`` under the endpoint's secret."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(secret: str, body: bytes, header: str, *, tolerance: int = SIGNATURE_TOLERANCE_SECONDS, now: float | None = None) -> bool:
    """What a receiver does with a delivery: true when the signature is ours and recent.
    Compare against the raw bytes received, before any JSON parsing."""
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - timestamp) > tolerance:
        return False  # too old (or from the future): a recording of a real delivery, sent again
    return hmac.compare_digest(sign(secret, timestamp, body), f"t={timestamp},v1={parts.get('v1', '')}")


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


# ----------------------------------------------------------------------------- endpoints


def _events(names: list[str] | None) -> list[str]:
    names = [n.strip() for n in (names or []) if n and n.strip()]
    if not names:
        raise WebhookError("choose at least one event")
    if EVERYTHING in names:
        return [EVERYTHING]
    unknown = sorted(set(names) - set(EVENT_NAMES))
    if unknown:
        raise WebhookError(f"unknown event '{unknown[0]}'. The events are: {', '.join(EVENT_NAMES)}")
    return sorted(set(names))


def _address(url: str) -> str:
    try:
        return outbound.check_address(url)
    except outbound.AddressError as e:
        raise WebhookError(str(e)) from None


def describe(endpoint: WebhookEndpoint) -> dict[str, Any]:
    """An endpoint without its secret, which is shown only when it is made."""
    return {
        "id": endpoint.id, "url": endpoint.url, "description": endpoint.description, "events": list(endpoint.events or []), "active": endpoint.active,
        "disabled_reason": endpoint.disabled_reason, "failures_in_a_row": endpoint.failures, "created_by": endpoint.created_by,
        "created_at": endpoint.created_at.isoformat() if endpoint.created_at else None,
        "last_delivery_at": endpoint.last_delivery_at.isoformat() if endpoint.last_delivery_at else None, "last_status": endpoint.last_status,
        "secret_ends_with": endpoint.secret[-4:],
    }


def list_endpoints(s: Session, organization_id: int) -> list[WebhookEndpoint]:
    return list(s.execute(select(WebhookEndpoint).where(WebhookEndpoint.organization_id == organization_id).order_by(WebhookEndpoint.id)).scalars())


def get_endpoint(s: Session, organization_id: int, endpoint_id: int) -> WebhookEndpoint | None:
    endpoint = s.get(WebhookEndpoint, endpoint_id)
    return endpoint if endpoint is not None and endpoint.organization_id == organization_id else None


def create_endpoint(s: Session, organization_id: int, url: str, events: list[str], description: str, by: str) -> tuple[WebhookEndpoint, str]:
    """A new endpoint and its secret, which is returned this once."""
    if len(list_endpoints(s, organization_id)) >= MAX_ENDPOINTS:
        raise WebhookError(f"an organization can have {MAX_ENDPOINTS} endpoints; remove one first")
    secret = new_secret()
    endpoint = WebhookEndpoint(organization_id=organization_id, url=_address(url), events=_events(events), description=(description or "").strip()[:200], secret=secret,
                               created_by=by[:254])
    s.add(endpoint)
    s.flush()
    return endpoint, secret


def update_endpoint(endpoint: WebhookEndpoint, *, url: str | None = None, events: list[str] | None = None, description: str | None = None, active: bool | None = None) -> None:
    if url is not None:
        endpoint.url = _address(url)
    if events is not None:
        endpoint.events = _events(events)
    if description is not None:
        endpoint.description = description.strip()[:200]
    if active is not None:
        endpoint.active = active
        if active:  # switched on again by a person: start counting from nothing
            endpoint.failures, endpoint.disabled_reason = 0, None
        else:
            endpoint.disabled_reason = "switched off by an admin"


def rotate_secret(endpoint: WebhookEndpoint) -> str:
    """Replace the secret. Deliveries from now on are signed with the new one."""
    endpoint.secret = new_secret()
    return endpoint.secret


def delete_endpoint(s: Session, endpoint: WebhookEndpoint) -> None:
    """Remove an endpoint and the record of its deliveries. Deliveries still queued find nothing to send to."""
    s.execute(delete(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id))
    s.delete(endpoint)


# ----------------------------------------------------------------------------- deliveries


def describe_delivery(delivery: WebhookDelivery) -> dict[str, Any]:
    return {
        "id": delivery.id, "endpoint_id": delivery.endpoint_id, "event": delivery.event_type, "event_id": f"evt_{delivery.event_id}" if delivery.event_id else None,
        "status": delivery.status, "attempts": delivery.attempts, "response_status": delivery.response_status, "error": delivery.error, "duration_ms": delivery.duration_ms,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None, "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
    }


def list_deliveries(s: Session, endpoint: WebhookEndpoint, limit: int = 50, before_id: int | None = None) -> list[WebhookDelivery]:
    query = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
    if before_id is not None:
        query = query.where(WebhookDelivery.id < before_id)
    return list(s.execute(query.order_by(WebhookDelivery.id.desc()).limit(max(1, min(limit, 200)))).scalars())


def delivery_job_key(delivery_id: int, again: int = 0) -> str:
    return f"webhook:delivery:{delivery_id}" + (f":again:{again}" if again else "")


def queue_deliveries(s: Session, event: Event) -> int:
    """Called when an event is recorded, in its transaction: one delivery per endpoint
    of the event's organization that asked for this kind. Returns how many."""
    name = PUBLIC_EVENTS.get(event.type)
    if name is None or event.organization_id is None:
        return 0
    queued = 0
    for endpoint in s.execute(select(WebhookEndpoint).where(WebhookEndpoint.organization_id == event.organization_id, WebhookEndpoint.active.is_(True))).scalars():
        wanted = endpoint.events or []
        if EVERYTHING not in wanted and name not in wanted:
            continue
        delivery = WebhookDelivery(organization_id=endpoint.organization_id, endpoint_id=endpoint.id, event_id=event.id, event_type=name)
        try:
            with s.begin_nested():
                s.add(delivery)
                s.flush()
        except IntegrityError:
            continue  # this event is already on its way to this endpoint
        jobs.enqueue(s, DELIVERY, delivery_job_key(delivery.id), {"delivery_id": delivery.id}, max_attempts=MAX_ATTEMPTS, timeout_seconds=60,
                     call_run_id=event.call_run_id, organization_id=endpoint.organization_id)
        queued += 1
    return queued


def redeliver(s: Session, delivery: WebhookDelivery) -> WebhookDelivery:
    """Try a delivery that was given up on once more, with the same ids."""
    if delivery.event_id is None:
        raise WebhookError("a test delivery is not sent again; send a new test")
    if delivery.status == "pending":
        raise WebhookError("this delivery is still being attempted")
    again = int(s.execute(select(func.count()).select_from(Job).where(Job.key.like(delivery_job_key(delivery.id) + ":again:%"))).scalar() or 0) + 1
    delivery.status, delivery.error = "pending", None
    jobs.enqueue(s, DELIVERY, delivery_job_key(delivery.id, again), {"delivery_id": delivery.id}, max_attempts=MAX_ATTEMPTS, timeout_seconds=60, organization_id=delivery.organization_id)
    return delivery


# ----------------------------------------------------------------------------- what is sent


def _call(run: CallRun) -> dict[str, Any]:
    review = (run.summary or {}).get("review") or {}
    return {
        "id": run.id, "direction": run.direction or "outbound", "status": run.status, "outcome": run.disposition, "from_number": run.from_number,
        "started_at": run.started_at.isoformat() if run.started_at else None, "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "duration_seconds": run.duration_seconds, "language": run.language, "cost_usd": run.cost_usd,
        # From the review of the call, when there is one. What was said is read from the API, not sent here.
        "summary": review.get("summary"), "next_action": review.get("next_action"), "interest_level": review.get("interest_level"),
        # When the call was handed to a person: what that person should know (see voice.tools.handover_brief).
        "handover": (run.summary or {}).get("handover"),
    }


def _lead(lead: Lead) -> dict[str, Any]:
    return {"id": lead.id, "phone": lead.phone_e164, "first_name": lead.first_name, "last_name": lead.last_name, "company": lead.company, "email": lead.email,
            "status": lead.status, "score": lead.score}


def body_for(s: Session, event: Event) -> dict[str, Any]:
    """The JSON an endpoint receives for an event: the fact itself in ``data``, and the
    call, person and meeting it is about as they are now."""
    body: dict[str, Any] = {
        "id": f"evt_{event.id}", "type": PUBLIC_EVENTS.get(event.type, event.type), "api_version": API_VERSION,
        "created_at": event.occurred_at.isoformat() if event.occurred_at else None, "organization_id": event.organization_id, "data": dict(event.payload or {}),
    }
    campaign = s.get(Campaign, event.campaign_id) if event.campaign_id else None
    if campaign is not None:
        body["campaign"] = {"id": campaign.id, "name": campaign.name}
    run = s.get(CallRun, event.call_run_id) if event.call_run_id else None
    if run is not None:
        body["call"] = _call(run)
        lead = s.get(Lead, run.lead_id)
        if lead is not None:
            body["lead"] = _lead(lead)
    if event.type == "AppointmentBooked" and event.subject_id:
        appointment = s.get(Appointment, event.subject_id)
        if appointment is not None:
            body["appointment"] = {"id": appointment.id, "starts_at": appointment.starts_at, "timezone": appointment.timezone, "notes": appointment.notes}
    return body


def ping_body(endpoint: WebhookEndpoint, delivery_id: int) -> dict[str, Any]:
    return {"id": f"ping_{delivery_id}", "type": PING, "api_version": API_VERSION, "created_at": utcnow().isoformat(), "organization_id": endpoint.organization_id,
            "data": {"message": "A test delivery. Nothing happened."}}


def _encode(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def headers_for(secret: str, body: bytes, event_name: str, delivery_id: int, *, now: float | None = None) -> dict[str, str]:
    timestamp = int(time.time() if now is None else now)
    return {
        "Content-Type": "application/json", "X-Callagent-Event": event_name, "X-Callagent-Delivery": f"whd_{delivery_id}",
        "X-Callagent-Timestamp": str(timestamp), "X-Callagent-Signature": sign(secret, timestamp, body),
    }


# ----------------------------------------------------------------------------- sending


async def _send(url: str, secret: str, body: bytes, event_name: str, delivery_id: int) -> tuple[int | None, str | None, int]:
    """One attempt: (status answered, what went wrong, milliseconds)."""
    started = time.monotonic()
    try:
        status = await outbound.post(url, body, headers_for(secret, body, event_name, delivery_id), timeout=TIMEOUT_SECONDS)
    except outbound.DeliveryError as e:
        return None, str(e)[:300], int((time.monotonic() - started) * 1000)
    elapsed = int((time.monotonic() - started) * 1000)
    if 200 <= status < 300:
        return status, None, elapsed
    return status, f"the endpoint answered {status}" + ("; redirects are not followed" if 300 <= status < 400 else ""), elapsed


def _record(s: Session, delivery_id: int, status: int | None, error: str | None, elapsed: int) -> None:
    delivery = s.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return
    delivery.attempts += 1
    delivery.response_status, delivery.error, delivery.duration_ms = status, error, elapsed
    endpoint = s.get(WebhookEndpoint, delivery.endpoint_id)
    if endpoint is not None:
        endpoint.last_delivery_at, endpoint.last_status = utcnow(), status
    if error is None:
        delivery.status, delivery.delivered_at = "delivered", utcnow()
        if endpoint is not None:
            endpoint.failures = 0


async def _given_up(payload: dict[str, Any], error: str) -> None:
    """Every attempt failed. The delivery is marked, and an endpoint that keeps failing is switched off."""

    def _mark(s: Session) -> None:
        delivery = s.get(WebhookDelivery, int(payload["delivery_id"]))
        if delivery is None or delivery.status == "delivered":
            return
        delivery.status = "failed"
        endpoint = s.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None:
            return
        endpoint.failures += 1
        if endpoint.active and endpoint.failures >= DISABLE_AFTER:
            endpoint.active = False
            endpoint.disabled_reason = f"switched off after {endpoint.failures} deliveries in a row failed; the last: {delivery.error or error}"[:300]
            log.warning("webhook endpoint %s of organization %s switched off after %s failed deliveries", endpoint.id, endpoint.organization_id, endpoint.failures)

    await run_db(_mark, unrestricted=True)


@jobs.handler(DELIVERY, queue="integrations", on_dead=_given_up)
async def deliver(payload: dict[str, Any]) -> None:
    """Send one delivery. Raises when it did not arrive, so the queue tries again later."""
    delivery_id = int(payload["delivery_id"])

    def _load(s: Session):
        delivery = s.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.status == "delivered":
            return None
        endpoint = s.get(WebhookEndpoint, delivery.endpoint_id)
        event = s.get(Event, delivery.event_id) if delivery.event_id else None
        if endpoint is None or event is None:
            return None
        if not endpoint.active:
            delivery.status, delivery.error = "failed", "the endpoint was switched off before this could be sent"
            return None
        return endpoint.url, endpoint.secret, _encode(body_for(s, event)), delivery.event_type

    loaded = await run_db(_load, unrestricted=True)
    if loaded is None:
        return
    url, secret, body, name = loaded
    status, error, elapsed = await _send(url, secret, body, name, delivery_id)
    await run_db(lambda s: _record(s, delivery_id, status, error, elapsed), unrestricted=True)
    if error is not None:
        raise RuntimeError(error)


async def send_test(organization_id: int, endpoint_id: int) -> dict[str, Any] | None:
    """Send a ``ping`` to an endpoint now and say how it went. None: no such endpoint.
    A switched-off endpoint can be tested too: that is how one finds out it works again."""

    def _start(s: Session):
        endpoint = get_endpoint(s, organization_id, endpoint_id)
        if endpoint is None:
            return None
        delivery = WebhookDelivery(organization_id=organization_id, endpoint_id=endpoint.id, event_id=None, event_type=PING)
        s.add(delivery)
        s.flush()
        return endpoint.url, endpoint.secret, _encode(ping_body(endpoint, delivery.id)), delivery.id

    started = await run_db(_start)
    if started is None:
        return None
    url, secret, body, delivery_id = started
    status, error, elapsed = await _send(url, secret, body, PING, delivery_id)

    def _finish(s: Session) -> dict[str, Any]:
        _record(s, delivery_id, status, error, elapsed)
        delivery = s.get(WebhookDelivery, delivery_id)
        if error is not None:
            delivery.status = "failed"
        return describe_delivery(delivery)

    return await run_db(_finish)


def prune_deliveries(s: Session, older_than_days: int = 30) -> int:
    """Remove the record of deliveries older than this. The events themselves stay."""
    return int(s.execute(delete(WebhookDelivery).where(WebhookDelivery.created_at < utcnow() - timedelta(days=older_than_days))).rowcount or 0)

