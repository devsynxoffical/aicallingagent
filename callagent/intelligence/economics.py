"""What a result costs: per call, per connected call, per minute and per booked
meeting, by campaign and week by week.

Every amount is a sum over the usage ledger; every count is a count of stored calls.
Nothing is estimated here, and a ratio whose denominator is zero is absent, not zero.
A cost per meeting from a handful of meetings is marked as too thin to rely on.

The two sides are attributed slightly differently: a call counts in the period it
started in, its cost in the period the ledger line was written (when the call ended,
or later for a correction). Over a period of weeks the difference is the calls in
progress at its edges.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import CallRun, Campaign, UsageRecord

# The outcomes that count as a result: a meeting booked, or a sale closed on the call.
CONVERSIONS = {"meeting_booked", "sale_closed"}

# Below this many meetings, the cost of one says more about luck than about the campaign.
MIN_MEETINGS_FOR_A_COST = 5
ZERO = Decimal("0")
CENT_HUNDREDTHS = Decimal("0.0001")


def _money(amount: Decimal | None) -> str | None:
    return None if amount is None else str(amount.quantize(CENT_HUNDREDTHS))


def _per(amount: Decimal, count: int | Decimal) -> Decimal | None:
    return amount / Decimal(count) if count else None


def _figures(group: dict[str, Any]) -> dict[str, Any]:
    spend = group["call_cost"] + group["ai_cost"]
    return {
        "calls": group["calls"], "connected": group["connected"], "meetings": group["meetings"], "billed_minutes": str(group["minutes"].normalize() + ZERO),
        "connect_rate": round(group["connected"] / group["calls"], 4) if group["calls"] else None,
        "meeting_rate": round(group["meetings"] / group["connected"], 4) if group["connected"] else None,
        "spend_usd": _money(spend), "call_cost_usd": _money(group["call_cost"]), "ai_cost_usd": _money(group["ai_cost"]),
        "ai_share": round(float(group["ai_cost"] / spend), 4) if spend > 0 else None,
        "cost_per_call_usd": _money(_per(spend, group["calls"])),
        "cost_per_connected_call_usd": _money(_per(spend, group["connected"])),
        "cost_per_minute_usd": _money(_per(spend, group["minutes"])),
        "cost_per_meeting_usd": _money(_per(spend, group["meetings"])),
        # True when there are enough meetings for the cost of one to mean something.
        "enough_meetings": group["meetings"] >= MIN_MEETINGS_FOR_A_COST,
    }


def _group() -> dict[str, Any]:
    return {"calls": 0, "connected": 0, "meetings": 0, "minutes": ZERO, "call_cost": ZERO, "ai_cost": ZERO}


def week_of(moment: datetime) -> str:
    """The Monday of the week a moment falls in (UTC), as a date."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    day = moment.astimezone(timezone.utc).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def economics(s: Session, organization_id: int, since: datetime, campaign_id: int | None = None) -> dict[str, Any]:
    """An organization's unit economics since a moment, in total, by campaign and by week."""
    calls = [CallRun.organization_id == organization_id, CallRun.started_at >= since]
    lines = [UsageRecord.organization_id == organization_id, UsageRecord.created_at >= since]
    if campaign_id is not None:
        calls.append(CallRun.campaign_id == campaign_id)
        lines.append(UsageRecord.campaign_id == campaign_id)

    total = _group()
    by_campaign: dict[int | None, dict[str, Any]] = defaultdict(_group)
    by_week: dict[str, dict[str, Any]] = defaultdict(_group)

    for cid, disposition, connected, started_at in s.execute(select(CallRun.campaign_id, CallRun.disposition, CallRun.stream_connected, CallRun.started_at).where(*calls)):
        for group in (total, by_campaign[cid], by_week[week_of(started_at)]):
            group["calls"] += 1
            group["connected"] += bool(connected)
            group["meetings"] += disposition in CONVERSIONS

    # Summed in the database per campaign and kind; per week in Python, since a week is not a column.
    for cid, kind, written_at, amount, quantity in s.execute(select(UsageRecord.campaign_id, UsageRecord.kind, UsageRecord.created_at, UsageRecord.amount_usd, UsageRecord.quantity).where(*lines)):
        amount = Decimal(str(amount or 0))
        for group in (total, by_campaign[cid], by_week[week_of(written_at)]):
            if kind == "call":
                group["call_cost"] += amount
                group["minutes"] += Decimal(str(quantity or 0))
            else:
                group["ai_cost"] += amount

    names = dict(s.execute(select(Campaign.id, Campaign.name).where(Campaign.organization_id == organization_id)).all())
    return {
        "since": since.isoformat(),
        "currency": "USD",
        "total": _figures(total),
        "by_campaign": sorted(({"campaign_id": cid, "campaign": names.get(cid, "") if cid is not None else "", **_figures(group)} for cid, group in by_campaign.items()),
                              key=lambda row: -Decimal(row["spend_usd"] or "0")),
        "by_week": [{"week": week, **_figures(group)} for week, group in sorted(by_week.items())],
        "min_meetings_for_a_cost": MIN_MEETINGS_FOR_A_COST,
        "what_the_amounts_are": "sums over the usage ledger: measured usage at configured rates and list prices, corrected where the provider's own figure is known",
    }


def spend_by_call(s: Session, campaign_id: int) -> dict[int, Decimal]:
    """What each call of a campaign cost, corrections included."""
    rows = s.execute(select(UsageRecord.call_run_id, func.sum(UsageRecord.amount_usd)).where(UsageRecord.campaign_id == campaign_id, UsageRecord.call_run_id.is_not(None))
                     .group_by(UsageRecord.call_run_id))
    return {run_id: Decimal(str(amount or 0)) for run_id, amount in rows}
