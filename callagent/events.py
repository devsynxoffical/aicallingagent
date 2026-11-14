"""Durable business events.

An event is a fact ("this call was answered") written in the same database
transaction as the change it describes, so the two are saved together or not at all
(the transactional outbox pattern). Nothing is published to a broker: the database is
the log, and a lost network message cannot lose an event.

**Consumers** subscribe to an event type. When an event is recorded, one job per
subscriber is added to the job queue in that same transaction. Each consumer
therefore has its own retries, backoff and dead letter, and one consumer failing
never delays another.

**Duplicates**: an event has a key that identifies the fact (``call:17:answered``).
Recording it again, because a webhook was repeated or a job retried, does nothing.

**Order**: jobs are delivered at least once and in no guaranteed order. A consumer
must therefore read the current state of the thing the event is about (the call, the
campaign) instead of assuming what happened before; the event tells it what to look
at, not what the state is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import jobs
from .db import Event, utcnow

log = logging.getLogger(__name__)

# The events this system records, and the version of each one's payload.
TYPES: dict[str, int] = {
    "CallInitiated": 1,
    "CallAnswered": 1,
    "CallCompleted": 1,
    "CallFailed": 1,
    "CallFinalized": 1,
    "CallTransferred": 1,
    "CampaignStarted": 1,
    "CampaignPaused": 1,
    "CampaignStopped": 1,  # the dialer stopped it on its own: budget reached, schedule ended, ...
    "PlaybookApproved": 1,
    "DocumentUploaded": 1,
    "DocumentProcessed": 1,
    "AppointmentBooked": 1,
    "UsageRecorded": 1,
}


@dataclass(frozen=True)
class Subscriber:
    consumer: str  # unique per event type; part of the job key
    kind: str  # the job kind that handles it (see jobs.handler)
    max_attempts: int = 5
    timeout_seconds: int = 300


_subscribers: dict[str, list[Subscriber]] = {}


def subscribe(event_type: str, consumer: str, *, kind: str, max_attempts: int = 5, timeout_seconds: int = 300) -> None:
    """Have jobs of ``kind`` run for every event of this type. Registering the same
    consumer again replaces it."""
    if event_type not in TYPES:
        raise ValueError(f"unknown event type '{event_type}'")
    existing = [s for s in _subscribers.get(event_type, []) if s.consumer != consumer]
    _subscribers[event_type] = existing + [Subscriber(consumer, kind, max_attempts, timeout_seconds)]


def unsubscribe(event_type: str, consumer: str) -> None:
    _subscribers[event_type] = [s for s in _subscribers.get(event_type, []) if s.consumer != consumer]


def consumer_job_key(event_id: int, consumer: str) -> str:
    return f"event:{event_id}:{consumer}"


def emit(
    s: Session,
    event_type: str,
    *,
    key: str,
    subject: str,
    subject_id: int | None = None,
    organization_id: int | None = None,
    campaign_id: int | None = None,
    call_run_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Event | None:
    """Record an event inside the caller's transaction and queue its consumers.
    Returns None when this fact was already recorded (same key): nothing is added and
    no consumer runs a second time."""
    if event_type not in TYPES:
        raise ValueError(f"unknown event type '{event_type}'")
    if s.execute(select(Event.id).where(Event.key == key)).first() is not None:
        return None
    event = Event(
        organization_id=organization_id, type=event_type, version=TYPES[event_type], subject=subject, subject_id=subject_id,
        campaign_id=campaign_id, call_run_id=call_run_id, key=key[:200], payload=dict(payload or {}), occurred_at=utcnow(),
    )
    try:
        with s.begin_nested():  # a concurrent recording of the same fact loses here, without undoing the caller's work
            s.add(event)
            s.flush()
    except IntegrityError:
        return None
    for subscriber in _subscribers.get(event_type, []):
        jobs.enqueue(
            s, subscriber.kind, consumer_job_key(event.id, subscriber.consumer),
            {"event_id": event.id, "type": event_type, "version": event.version, "subject": subject, "subject_id": subject_id, **dict(payload or {})},
            max_attempts=subscriber.max_attempts, timeout_seconds=subscriber.timeout_seconds, call_run_id=call_run_id, organization_id=organization_id,
        )
    # The organization's own systems, if it registered any for this kind of event.
    from . import webhooks

    webhooks.queue_deliveries(s, event)
    return event


def describe(event: Event) -> dict[str, Any]:
    return {
        "id": event.id, "type": event.type, "version": event.version, "subject": event.subject, "subject_id": event.subject_id,
        "campaign_id": event.campaign_id, "call_run_id": event.call_run_id, "payload": event.payload or {},
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
    }


def for_call(s: Session, call_run_id: int) -> list[Event]:
    """Everything recorded about one call, oldest first."""
    return list(s.execute(select(Event).where(Event.call_run_id == call_run_id).order_by(Event.id)).scalars())
