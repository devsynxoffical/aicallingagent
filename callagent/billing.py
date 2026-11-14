"""The usage ledger: every line of cost, written once, corrected only by adding.

What goes in:

- one **call** line per call: the attempt fee plus the started minutes at the
  configured per-minute rate (telephony and the speech services, which are billed by
  time). Written when the call ends, from the carrier's duration when it is known and
  from the engine's own clock otherwise; when the carrier's figure arrives later and
  differs, a correction line holds the difference;
- one **ai_model** line per AI usage record that has a known price: the provider's
  token counts at the price table in force.

Every line has a unique key, so the same fact reported twice is one line. No line is
ever updated or deleted: `adjust` adds a line pointing at the one it corrects.

Spend, budgets and a call's displayed cost are sums over these lines.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .db import AIUsage, CallRun, Campaign, UsageRecord, campaign_organization_id

log = logging.getLogger(__name__)

CENT_HUNDREDTHS = Decimal("0.0001")
ZERO = Decimal("0")


class LedgerError(ValueError):
    """The correction asked for cannot be made. The message is for the person asking."""


def call_amount(seconds: int | None, settings: Settings) -> tuple[Decimal, dict[str, Any]]:
    """What a call of this length comes to, and how: (amount, breakdown)."""
    minutes = math.ceil((seconds or 0) / 60)
    fee = Decimal(str(settings.cost_per_call_attempt_usd))
    rate = Decimal(str(settings.cost_per_connected_minute_usd))
    amount = (fee + minutes * rate).quantize(CENT_HUNDREDTHS)
    return amount, {"seconds": int(seconds or 0), "minutes": minutes, "per_minute_usd": str(rate), "attempt_fee_usd": str(fee)}


def _insert(s: Session, line: UsageRecord) -> UsageRecord:
    """Add a line unless one with its key exists. Returns the line that is in the ledger."""
    existing = s.execute(select(UsageRecord).where(UsageRecord.key == line.key)).scalars().first()
    if existing is not None:
        return existing
    try:
        with s.begin_nested():
            s.add(line)
            s.flush()
    except IntegrityError:  # the same fact, written at the same moment by someone else
        return s.execute(select(UsageRecord).where(UsageRecord.key == line.key)).scalars().one()
    return line


def call_total(s: Session, call_run_id: int) -> Decimal:
    return Decimal(str(s.execute(select(func.coalesce(func.sum(UsageRecord.amount_usd), 0)).where(UsageRecord.call_run_id == call_run_id)).scalar() or 0))


def _refresh_call_cost(s: Session, run: CallRun) -> None:
    """The cost shown on a call is the sum of its ledger lines, and is only ever written here."""
    run.cost_usd = float(call_total(s, run.id))


def record_call(s: Session, run: CallRun, seconds: int | None, settings: Settings, *, source: str) -> UsageRecord:
    """Write the call's line, once. ``source`` is "carrier" (the provider's reported
    duration, which is what is billed) or "engine" (our own clock, when the call ended
    without the carrier's report).

    Written first by the engine and then reported by the carrier with another
    duration: the first line stays as it is and a correction line holds the
    difference. Reported by the carrier twice: the first report stands."""
    key = f"call:{run.id}:call"
    line = s.execute(select(UsageRecord).where(UsageRecord.key == key)).scalars().first()
    if line is None:
        amount, breakdown = call_amount(seconds, settings)
        campaign = s.get(Campaign, run.campaign_id)
        from . import events

        events.emit(s, "UsageRecorded", key=f"usage:call:{run.id}:call", subject="usage", subject_id=run.id, organization_id=run.organization_id,
                    campaign_id=run.campaign_id, call_run_id=run.id, payload={"kind": "call", "seconds": int(seconds or 0), "amount_usd": str(amount), "source": source})
        line = _insert(s, UsageRecord(
            organization_id=campaign_organization_id(s, campaign) if campaign else run.organization_id, campaign_id=run.campaign_id, call_run_id=run.id,
            kind="call", key=key, quantity_seconds=int(seconds or 0), quantity=breakdown["minutes"], unit="minute", amount_usd=amount,
            basis="estimate", detail={**breakdown, "source": source},
        ))
    elif source == "carrier" and (line.detail or {}).get("source") == "engine" and line.quantity_seconds != int(seconds or 0):
        amount, breakdown = call_amount(seconds, settings)
        recorded = Decimal(str(line.amount_usd))
        _insert(s, UsageRecord(
            organization_id=line.organization_id, campaign_id=line.campaign_id, call_run_id=run.id, kind="call", key=f"call:{run.id}:call:carrier-duration",
            quantity_seconds=int(seconds or 0) - line.quantity_seconds, quantity=breakdown["minutes"] - Decimal(str(line.quantity)), unit="minute",
            amount_usd=amount - recorded, basis="estimate", adjusts_id=line.id, detail={**breakdown, "source": "carrier"},
            note=f"the carrier reported {int(seconds or 0)} s; the line was written from the engine's clock as {line.quantity_seconds} s",
        ))
    elif source == "carrier" and line.quantity_seconds != int(seconds or 0):
        log.warning("call %s: usage already recorded as %ss, a later report said %ss; kept the first", run.id, line.quantity_seconds, seconds)
    _refresh_call_cost(s, run)
    return line


def billed_seconds(s: Session, call_run_id: int) -> int:
    """The seconds a call is billed for, corrections included."""
    return int(s.execute(select(func.coalesce(func.sum(UsageRecord.quantity_seconds), 0)).where(UsageRecord.call_run_id == call_run_id, UsageRecord.kind == "call")).scalar() or 0)


def record_ai(s: Session, usage: AIUsage) -> UsageRecord | None:
    """The ledger line of one AI usage record. None when there is nothing to charge:
    no price is known for the model, or the usage belongs to no campaign."""
    if usage.cost_usd is None or usage.campaign_id is None or usage.id is None:
        return None
    amount = Decimal(str(usage.cost_usd)).quantize(CENT_HUNDREDTHS)
    tokens = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    line = _insert(s, UsageRecord(
        organization_id=usage.organization_id, campaign_id=usage.campaign_id, call_run_id=usage.call_run_id, kind="ai_model", key=f"ai:{usage.id}",
        quantity=tokens, unit="token", amount_usd=amount, basis="estimate",
        detail={"ai_usage_id": usage.id, "workflow": usage.workflow, "model": usage.model, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens, "cache_write_tokens": usage.cache_write_tokens, "price": "list price or AI_PRICES"},
    ))
    if usage.call_run_id is not None:
        run = s.get(CallRun, usage.call_run_id)
        if run is not None:
            _refresh_call_cost(s, run)
    return line


def adjust(s: Session, line_id: int, amount_usd: Decimal, *, note: str, by: str, basis: str = "manual", key: str | None = None, organization_id: int | None = None) -> UsageRecord:
    """Correct a line by adding one. ``amount_usd`` is the difference to apply (negative
    to lower the total). The line corrected is left exactly as it was."""
    original = s.get(UsageRecord, line_id)
    if original is None or (organization_id is not None and original.organization_id != organization_id):
        raise LedgerError("no such ledger line")
    if original.adjusts_id is not None:
        raise LedgerError("this line is itself a correction; correct the line it points at")
    if not note.strip():
        raise LedgerError("a correction needs a note saying why")
    amount = Decimal(str(amount_usd)).quantize(CENT_HUNDREDTHS)
    if amount == ZERO:
        raise LedgerError("a correction of zero changes nothing")
    if key is None:
        made = int(s.execute(select(func.count()).select_from(UsageRecord).where(UsageRecord.adjusts_id == original.id)).scalar() or 0)
        key = f"{original.key}:adjust:{made + 1}"
    line = _insert(s, UsageRecord(
        organization_id=original.organization_id, campaign_id=original.campaign_id, call_run_id=original.call_run_id, kind=original.kind, key=key[:160],
        quantity=0, unit=original.unit, amount_usd=amount, currency=original.currency, basis=basis, adjusts_id=original.id, note=note.strip()[:300], created_by=by[:254],
        detail={"corrects": original.key},
    ))
    if original.call_run_id is not None:
        run = s.get(CallRun, original.call_run_id)
        if run is not None:
            _refresh_call_cost(s, run)
    return line


def record_provider_price(s: Session, run: CallRun, price_usd: Decimal, *, by: str = "provider") -> UsageRecord | None:
    """The provider's own price for a call has arrived. The estimate stays; a correction
    line brings the call's time-based total to the provider's figure. Applying the
    same price again changes nothing."""
    line = s.execute(select(UsageRecord).where(UsageRecord.key == f"call:{run.id}:call")).scalars().first()
    if line is None:
        return None
    so_far = Decimal(str(s.execute(select(func.coalesce(func.sum(UsageRecord.amount_usd), 0)).where(UsageRecord.call_run_id == run.id, UsageRecord.kind == "call")).scalar() or 0))
    difference = (abs(Decimal(str(price_usd))) - so_far).quantize(CENT_HUNDREDTHS)
    key = f"call:{run.id}:call:provider-price"
    if difference == ZERO or s.execute(select(UsageRecord.id).where(UsageRecord.key == key)).first() is not None:
        return None
    return adjust(s, line.id, difference, note=f"the provider's price for this call is ${abs(Decimal(str(price_usd)))}", by=by, basis="provider", key=key)


# ----------------------------------------------------------------------------- reading


def campaign_spend(s: Session, campaign_id: int) -> Decimal:
    """What a campaign has cost so far: the sum of its ledger lines."""
    return Decimal(str(s.execute(select(func.coalesce(func.sum(UsageRecord.amount_usd), 0)).where(UsageRecord.campaign_id == campaign_id)).scalar() or 0))


def describe(line: UsageRecord) -> dict[str, Any]:
    return {
        "id": line.id, "at": line.created_at.isoformat() if line.created_at else None, "kind": line.kind, "campaign_id": line.campaign_id, "call_run_id": line.call_run_id,
        "quantity": str(line.quantity), "unit": line.unit, "amount_usd": str(line.amount_usd), "currency": line.currency, "basis": line.basis,
        "corrects": line.adjusts_id, "note": line.note, "by": line.created_by, "detail": line.detail or {},
    }


def summary(s: Session, organization_id: int, since: datetime, campaign_id: int | None = None, limit: int = 100, before_id: int | None = None) -> dict[str, Any]:
    """An organization's ledger since a moment: totals by kind and by basis, and the newest lines."""
    scope = [UsageRecord.organization_id == organization_id, UsageRecord.created_at >= since]
    if campaign_id is not None:
        scope.append(UsageRecord.campaign_id == campaign_id)
    by_kind = s.execute(select(UsageRecord.kind, UsageRecord.basis, func.count(), func.sum(UsageRecord.amount_usd)).where(*scope).group_by(UsageRecord.kind, UsageRecord.basis)
                        .order_by(UsageRecord.kind, UsageRecord.basis)).all()
    lines = select(UsageRecord).where(*scope)
    if before_id is not None:
        lines = lines.where(UsageRecord.id < before_id)
    total = sum((Decimal(str(amount or 0)) for _kind, _basis, _count, amount in by_kind), ZERO)
    return {
        "since": since.isoformat(),
        "currency": "USD",
        "total_usd": str(total.quantize(CENT_HUNDREDTHS)),
        "by_kind": [{"kind": kind, "basis": basis, "lines": int(count), "amount_usd": str(Decimal(str(amount or 0)).quantize(CENT_HUNDREDTHS))} for kind, basis, count, amount in by_kind],
        "what_the_amounts_are": "measured usage at configured rates and list prices, corrected where the provider's own figure is known; not an invoice",
        "lines": [describe(line) for line in s.execute(lines.order_by(UsageRecord.id.desc()).limit(max(1, min(limit, 500)))).scalars()],
    }
