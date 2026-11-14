"""The life of a call, in one place.

    created ──> queued ──> initiated ──> ringing ──> in-progress ──┐
        │          │            │            │             │        ├──> completed
        └──────────┴────────────┴────────────┴─────────────┴────────┤    busy
                                                                    │    no-answer
                                                                    │    failed
                                                                    └──> canceled

Rules:

- A call only moves forward. A late "ringing" cannot reopen a call that has ended.
- The five end states are final, with one exception: when *we* concluded that a call
  had ended (it went silent, the pipeline could not start) and the provider later
  reports how it really ended, the provider's answer replaces ours. The provider knows;
  we guessed.
- Every writer goes through :func:`advance`. A move that is not allowed changes nothing
  and is logged, so an out-of-order or repeated webhook is harmless.

The usage ledger is written here too, at the single moment a call reaches an end state
with the provider's final duration: one row per call, enforced by the database, however
many times that moment is reported.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import ACTIVE_CALL_STATUSES, TERMINAL_CALL_STATUSES, CallRun, UsageRecord, campaign_organization_id, utcnow
from ..db import Campaign

log = logging.getLogger(__name__)

ORDER = {status: rank for rank, status in enumerate(ACTIVE_CALL_STATUSES)}
STATUSES = (*ACTIVE_CALL_STATUSES, *TERMINAL_CALL_STATUSES)

PROVIDER = "provider"  # a status the telephony provider reported
SYSTEM = "system"  # a status we set from our own observations


def can_advance(current: str, new: str, *, source: str, current_source: str | None) -> bool:
    if new not in STATUSES or current == new:
        return False
    if current in TERMINAL_CALL_STATUSES:
        # Final, unless the provider is correcting an ending we inferred ourselves.
        return new in TERMINAL_CALL_STATUSES and source == PROVIDER and current_source != PROVIDER
    if new in TERMINAL_CALL_STATUSES:
        return True
    return ORDER[new] > ORDER.get(current, -1)


def advance(run: CallRun, new: str, *, source: str = SYSTEM) -> bool:
    """Move a call to ``new`` if the lifecycle allows it. Returns whether it moved."""
    current = run.status
    if not can_advance(current, new, source=source, current_source=run.status_source):
        if current != new:
            log.info("call %s: ignored %s -> %s from %s", run.id, current, new, source)
        return False
    run.status, run.status_source = new, source
    if new in TERMINAL_CALL_STATUSES:
        run.ended_at = run.ended_at or utcnow()
    return True


def record_usage(s: Session, run: CallRun, seconds: int | None, amount_usd: float) -> UsageRecord:
    """Write a call's ledger line with a stated amount, once. The engine itself goes
    through ``billing.record_call``, which works the amount out from the configured
    rates; this remains for callers that already hold one."""
    from .. import billing

    campaign = s.get(Campaign, run.campaign_id)
    minutes = -(-int(seconds or 0) // 60)
    line = billing._insert(s, UsageRecord(
        organization_id=campaign_organization_id(s, campaign) if campaign else None, campaign_id=run.campaign_id, call_run_id=run.id, kind="call",
        key=f"call:{run.id}:call", quantity_seconds=int(seconds or 0), quantity=minutes, unit="minute",
        amount_usd=Decimal(str(amount_usd)).quantize(Decimal("0.0001")), basis="estimate", detail={"seconds": int(seconds or 0), "minutes": minutes, "source": "stated"},
    ))
    billing._refresh_call_cost(s, run)
    return line
