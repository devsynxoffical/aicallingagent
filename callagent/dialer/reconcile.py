"""Before a call is declared dead, or its lead rung again, ask the provider what happened.

A call can go quiet on our side for reasons that say nothing about the call itself: a
lost webhook, a restart, a request that timed out after the provider had already
accepted it. Guessing "it failed" and redialing is how a person gets two calls. So:

- a call we hold an id for is looked up by that id;
- a call we hold no id for (the create request never answered) is looked for by number
  and time;
- only the provider's answer changes anything. If the provider cannot be reached, the
  call and its lead stay exactly as they are and are asked about again next time.

This runs for every campaign whatever its state, so a paused or finished campaign's
loose ends are tied up too.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import ACTIVE_CALL_STATUSES, TERMINAL_CALL_STATUSES, CallRun, Lead, run_db, utcnow
from . import lifecycle, twilio_client
from .campaign_runner import apply_twilio_status, release_inbound_lead
from .rules import campaign_settings

log = logging.getLogger(__name__)

# How long each early state may last before we ask. Ringing ends by itself within the
# dial timeout; a call in progress may legitimately last up to the call time limit.
SETUP_GRACE = timedelta(minutes=5)
NO_ID_GRACE = timedelta(minutes=2)
TALK_GRACE = timedelta(minutes=5)


def _candidates(s: Session, settings: Settings) -> list[dict[str, Any]]:
    now = utcnow()
    out = []
    for run in s.execute(select(CallRun).where(CallRun.status.in_(ACTIVE_CALL_STATUSES)).order_by(CallRun.id).limit(200)).scalars():
        age = now - run.started_at
        if run.twilio_call_sid is None:
            due = run.direction == "outbound" and age > NO_ID_GRACE
        elif run.status == "in-progress":
            due = age > timedelta(seconds=campaign_settings(s, run.campaign_id, settings).call_time_limit_seconds) + TALK_GRACE
        else:
            due = age > SETUP_GRACE
        if not due:
            continue
        lead = s.get(Lead, run.lead_id)
        out.append({"id": run.id, "sid": run.twilio_call_sid, "phone": lead.phone_e164 if lead else None, "started_at": run.started_at})
    return out


def _release_lead(s: Session, run: CallRun, settings: Settings) -> None:
    lead = s.get(Lead, run.lead_id)
    if lead is None:
        return
    if run.direction == "inbound":
        release_inbound_lead(run, lead)
    elif lead.status == "calling":
        effective = campaign_settings(s, run.campaign_id, settings)
        lead.status = "pending" if lead.attempts < effective.max_attempts else "completed"
        lead.next_attempt_at = utcnow() + timedelta(minutes=effective.retry_delay_minutes)


def _apply(s: Session, candidate: dict[str, Any], found: dict[str, Any] | None, settings: Settings) -> str:
    run = s.get(CallRun, candidate["id"])
    if run is None or run.status not in ACTIVE_CALL_STATUSES:
        return "already resolved"  # a webhook got there first
    if found is None:
        # The provider has no such call: it was never placed, or its record is gone.
        lifecycle.advance(run, "failed")
        run.error = ((run.error + " | ") if run.error else "") + "the provider has no record of this call"
        _release_lead(s, run, settings)
        return "no record"
    if run.twilio_call_sid is None:
        run.twilio_call_sid = found["sid"]  # the call we could not confirm does exist: it is ours
    status = str(found.get("status") or "").lower()
    if status in TERMINAL_CALL_STATUSES:
        apply_twilio_status(s, run.id, {"CallSid": found["sid"], "CallStatus": status, "CallDuration": found.get("duration")}, settings)
        return "ended"
    lifecycle.advance(run, status, source=lifecycle.PROVIDER)
    return "still active"


async def reconcile_calls(settings: Settings | None = None, provider=twilio_client) -> dict[str, int]:
    """One pass over calls that have been quiet too long. Returns counts by outcome."""
    settings = settings or get_settings()
    counts: dict[str, int] = {}
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        return counts  # nobody to ask (development without a provider)
    for candidate in await run_db(lambda s: _candidates(s, settings)):
        try:
            if candidate["sid"]:
                lookup = asyncio.to_thread(provider.fetch_call, candidate["sid"], settings)
            elif candidate["phone"]:
                lookup = asyncio.to_thread(provider.find_call, candidate["phone"], candidate["started_at"] - timedelta(minutes=1), settings)
            else:
                continue
            found = await asyncio.wait_for(lookup, timeout=settings.provider_timeout_seconds + 5)
        except Exception as e:
            # No answer is not an answer. Leave everything as it is and ask again later.
            log.warning("call %s: the provider could not be asked (%s); left unchanged", candidate["id"], type(e).__name__)
            counts["provider unreachable"] = counts.get("provider unreachable", 0) + 1
            continue
        outcome = await run_db(lambda s: _apply(s, candidate, found, settings))
        counts[outcome] = counts.get(outcome, 0) + 1
    if counts:
        log.info("call reconciliation: %s", counts)
    return counts


async def reconcile_forever(interval_seconds: float = 60) -> None:
    while True:
        try:
            await reconcile_calls()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call reconciliation failed")
        await asyncio.sleep(interval_seconds)
