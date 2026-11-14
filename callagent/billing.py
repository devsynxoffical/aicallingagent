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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
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


# ----------------------------------------------------------------------------- statements


STATEMENT_NOTE = ("A statement of measured usage: calls at the rates in force when each was made, AI at list prices, corrected where the provider's own figure "
                  "is known. It is not a tax invoice: it carries no tax and no seller or buyer details, and this system collects no payment.")


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """The first moment of a calendar month and of the next one, in UTC."""
    if not (1 <= month <= 12 and 2000 <= year <= 2200):
        raise LedgerError("give the month as YYYY-MM")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    return start, datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)


def parse_period(period: str) -> tuple[int, int]:
    try:
        year, month = period.split("-")
        if len(year) != 4 or len(month) != 2:
            raise ValueError
        found = int(year), int(month)
    except ValueError:
        raise LedgerError("give the month as YYYY-MM") from None
    month_bounds(*found)
    return found


def _in_month(organization_id: int, year: int, month: int) -> list[Any]:
    start, end = month_bounds(year, month)
    return [UsageRecord.organization_id == organization_id, UsageRecord.created_at >= start, UsageRecord.created_at < end]


def statement(s: Session, organization_id: int, year: int, month: int, now: datetime | None = None) -> dict[str, Any]:
    """What an organization used in one calendar month (UTC), from the ledger.

    A line belongs to the month it was written in, corrections included: a correction
    made in March to a call of February is on March's statement. Since no line is ever
    changed or backdated, the statement of a month that is over never changes."""
    from .db import Organization

    scope = _in_month(organization_id, year, month)
    start, end = month_bounds(year, month)
    original, correction = UsageRecord.adjusts_id.is_(None), UsageRecord.adjusts_id.is_not(None)

    def total(*where: Any, column: Any = UsageRecord.amount_usd) -> Decimal:
        return Decimal(str(s.execute(select(func.coalesce(func.sum(column), 0)).where(*scope, *where)).scalar() or 0))

    def lines(*where: Any) -> int:
        return int(s.execute(select(func.count()).select_from(UsageRecord).where(*scope, *where)).scalar() or 0)

    is_call, is_ai = UsageRecord.kind == "call", UsageRecord.kind == "ai_model"
    by_campaign = s.execute(
        select(UsageRecord.campaign_id, func.sum(UsageRecord.amount_usd),
               func.sum(case((is_call & original, 1), else_=0)), func.sum(case((is_call, UsageRecord.quantity), else_=0)))
        .where(*scope).group_by(UsageRecord.campaign_id)).all()
    names = dict(s.execute(select(Campaign.id, Campaign.name).where(Campaign.id.in_([cid for cid, *_rest in by_campaign if cid is not None]))).all()) if by_campaign else {}
    by_basis = s.execute(select(UsageRecord.basis, func.count(), func.sum(UsageRecord.amount_usd)).where(*scope).group_by(UsageRecord.basis).order_by(UsageRecord.basis)).all()
    organization = s.get(Organization, organization_id)
    money = lambda amount: str(Decimal(str(amount or 0)).quantize(CENT_HUNDREDTHS))  # noqa: E731
    return {
        "number": f"{organization_id}-{year:04d}-{month:02d}",
        "organization": {"id": organization_id, "name": organization.name if organization else ""},
        "period": {"year": year, "month": month, "from": start.isoformat(), "until": end.isoformat()},
        # An open month is still being added to.
        "status": "open" if (now or datetime.now(timezone.utc)) < end else "closed",
        "currency": "USD",
        "total_usd": money(total()),
        "calls": {"count": lines(is_call, original), "minutes": str(total(is_call, column=UsageRecord.quantity).normalize() + ZERO), "amount_usd": money(total(is_call, original))},
        "ai": {"requests": lines(is_ai, original), "tokens": int(total(is_ai, original, column=UsageRecord.quantity)), "amount_usd": money(total(is_ai, original))},
        "corrections": {"lines": lines(correction), "amount_usd": money(total(correction))},
        "by_basis": [{"basis": basis, "lines": int(count), "amount_usd": money(amount)} for basis, count, amount in by_basis],
        "by_campaign": sorted(({"campaign_id": cid, "campaign": names.get(cid, "") if cid is not None else "", "calls": int(calls or 0),
                                "minutes": str(Decimal(str(minutes or 0)).normalize() + ZERO), "amount_usd": money(amount)} for cid, amount, calls, minutes in by_campaign),
                              key=lambda row: -Decimal(row["amount_usd"])),
        "lines": lines(),
        "what_this_is": STATEMENT_NOTE,
    }


def statement_months(s: Session, organization_id: int, limit: int = 24, now: datetime | None = None) -> list[dict[str, Any]]:
    """The months an organization has anything in the ledger for, newest first."""
    first = s.execute(select(func.min(UsageRecord.created_at)).where(UsageRecord.organization_id == organization_id)).scalar()
    if first is None:
        return []
    now = now or datetime.now(timezone.utc)
    year, month, out = now.year, now.month, []
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    while (year, month) >= (first.year, first.month) and len(out) < limit:
        scope = _in_month(organization_id, year, month)
        count, amount = s.execute(select(func.count(), func.coalesce(func.sum(UsageRecord.amount_usd), 0)).where(*scope)).one()
        if count:
            out.append({"period": f"{year:04d}-{month:02d}", "lines": int(count), "total_usd": str(Decimal(str(amount)).quantize(CENT_HUNDREDTHS)),
                        "status": "open" if now < month_bounds(year, month)[1] else "closed"})
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out


def statement_lines(s: Session, organization_id: int, year: int, month: int) -> list[dict[str, Any]]:
    """Every line of a month, oldest first, as flat rows for a spreadsheet."""
    rows = s.execute(select(UsageRecord, Campaign.name).outerjoin(Campaign, Campaign.id == UsageRecord.campaign_id).where(*_in_month(organization_id, year, month))
                     .order_by(UsageRecord.id)).all()
    return [{"line": line.id, "written_at": line.created_at.isoformat() if line.created_at else "", "kind": line.kind, "campaign": name or "", "call": line.call_run_id or "",
             "quantity": str(line.quantity), "unit": line.unit, "amount_usd": str(line.amount_usd), "basis": line.basis, "corrects_line": line.adjusts_id or "",
             "note": line.note or ""} for line, name in rows]

