"""Per-campaign calling rules: when to call, how often, how much, and when to stop.

Every field is optional. A campaign with no rules behaves exactly like the global
settings, so existing campaigns are unaffected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import ACTIVE_CALL_STATUSES, Appointment, CallRun, Campaign, Lead, utcnow

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_CALLING_DAYS = (0, 1, 2, 3, 4, 5)  # Monday to Saturday


class CampaignRules(BaseModel):
    calling_days: list[int] | None = Field(default=None, description="Weekdays calls may be placed, 0 = Monday. Default Monday to Saturday.")
    window_start: str | None = Field(default=None, description="Earliest local time to call, HH:MM.")
    window_end: str | None = Field(default=None, description="Latest local time to call, HH:MM.")
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    retry_delay_minutes: int | None = Field(default=None, ge=5, le=60 * 24 * 14)
    max_calls_per_day: int | None = Field(default=None, ge=1)
    max_calls_per_month: int | None = Field(default=None, ge=1)
    budget_usd: float | None = Field(default=None, gt=0, description="Stop dialing when estimated spend reaches this.")
    starts_at: datetime | None = Field(default=None, description="Do not dial before this moment.")
    ends_at: datetime | None = Field(default=None, description="Stop the campaign at this moment.")
    stop_after_meetings: int | None = Field(default=None, ge=1, description="Stop once this many meetings are booked.")
    max_opt_out_rate: float | None = Field(default=None, gt=0, le=1, description="Stop if more than this share of answered calls end in a do-not-call request.")
    opt_out_min_calls: int = Field(default=30, ge=5, description="Answered calls needed before the opt-out rate is judged.")
    scoring_weights: dict[str, int] | None = Field(default=None, description="Lead-score weights; see intelligence/scoring.py.")
    require_consent: bool = Field(default=False, description="Call only people with a ground for calling on record (their consent, an existing relationship). Others stay in the list uncalled.")

    @field_validator("calling_days")
    @classmethod
    def _days(cls, v):
        if v is None:
            return v
        days = sorted(set(v))
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("calling_days must be weekdays 0 (Monday) to 6 (Sunday)")
        return days

    @field_validator("window_start", "window_end")
    @classmethod
    def _clock(cls, v):
        if v is None:
            return v
        parse_clock(v)
        return v

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("give the date and time with a timezone offset")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if self.window_start and self.window_end and parse_clock(self.window_start) >= parse_clock(self.window_end):
            raise ValueError("window_start must be earlier than window_end")
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("starts_at must be before ends_at")
        if self.max_calls_per_day and self.max_calls_per_month and self.max_calls_per_day > self.max_calls_per_month:
            raise ValueError("max_calls_per_day cannot exceed max_calls_per_month")
        return self


def parse_clock(text: str) -> time:
    try:
        hh, mm = text.strip().split(":")
        return time(int(hh), int(mm))
    except Exception:
        raise ValueError(f"'{text}' is not a time in HH:MM form") from None


def parse_days(text: str) -> list[int]:
    """'mon-fri', 'mon,wed,fri', 'sat-sun' -> weekday numbers."""
    days: list[int] = []
    for part in text.lower().replace(" ", "").split(","):
        if not part:
            continue
        names = part.split("-")
        try:
            first, last = DAY_NAMES.index(names[0][:3]), DAY_NAMES.index(names[-1][:3])
        except ValueError:
            raise ValueError(f"'{part}' is not a weekday or range like mon-fri") from None
        days.extend(range(first, last + 1) if first <= last else [*range(first, 7), *range(0, last + 1)])
    return sorted(set(days))


def rules_of(campaign: Campaign) -> CampaignRules:
    return CampaignRules.model_validate(campaign.rules_json or {})


def campaign_settings(s: Session, campaign_id: int, settings: Settings) -> Settings:
    """The deployment's settings as they apply to this campaign: its organization's own
    numbers, webhook and rates, then the campaign's retry and calling-window rules.
    Code that already reads ``settings.max_attempts`` and friends gets the right values."""
    from .. import orgsettings

    campaign = s.get(Campaign, campaign_id)
    if campaign is None:
        return settings
    # First what is the organization's own (its numbers, transfer number, webhook, rates), then the campaign's rules.
    settings = orgsettings.effective(s, campaign.organization_id, settings)
    if not campaign.rules_json:
        return settings
    rules = rules_of(campaign)
    update: dict[str, Any] = {}
    if rules.max_attempts is not None:
        update["max_attempts"] = rules.max_attempts
    if rules.retry_delay_minutes is not None:
        update["retry_delay_minutes"] = rules.retry_delay_minutes
    if rules.window_start is not None:
        update["calling_window_start"] = parse_clock(rules.window_start)
    if rules.window_end is not None:
        update["calling_window_end"] = parse_clock(rules.window_end)
    return settings.model_copy(update=update) if update else settings


def calling_days(campaign: Campaign | None) -> tuple[int, ...]:
    if campaign is None or not campaign.rules_json:
        return DEFAULT_CALLING_DAYS
    return tuple(rules_of(campaign).calling_days or DEFAULT_CALLING_DAYS)


# ----------------------------------------------------------------------------- cost


def call_cost(duration_seconds: int | None, settings: Settings) -> float:
    """Estimated cost of one call: the attempt fee plus connected time billed by the started minute."""
    minutes = math.ceil((duration_seconds or 0) / 60)
    return round(settings.cost_per_call_attempt_usd + minutes * settings.cost_per_connected_minute_usd, 4)


def campaign_spend(s: Session, campaign_id: int) -> float:
    """What the campaign has cost so far: the sum of its ledger lines."""
    from .. import billing

    return round(float(billing.campaign_spend(s, campaign_id)), 4)


def budget_refusal(s: Session, campaign: Campaign, settings: Settings) -> str | None:
    """Why one more call may not be placed for budget reasons, or None.

    Checked inside the transaction that reserves the lead, with the campaign row
    locked, for every outbound call however it is started (the dial loop, a manual
    dial, another process). A call in progress counts at its worst case: the longest
    a call may last, at the configured rates. What the AI model adds is known only
    after a call and is not reserved in advance.
    """
    rules = rules_of(campaign)
    if rules.budget_usd is None:
        return None
    effective = campaign_settings(s, campaign.id, settings)
    spent = campaign_spend(s, campaign.id)
    worst_case = call_cost(effective.call_time_limit_seconds, effective)
    in_progress = int(s.execute(select(func.count()).select_from(CallRun).where(
        CallRun.campaign_id == campaign.id, CallRun.direction == "outbound", CallRun.status.in_(ACTIVE_CALL_STATUSES))).scalar() or 0)
    if spent >= rules.budget_usd:
        return f"the campaign's budget of ${rules.budget_usd:,.2f} is spent (${spent:,.2f})"
    if spent + (in_progress + 1) * worst_case > rules.budget_usd:
        return f"the campaign's remaining budget (${rules.budget_usd - spent:,.2f}) does not cover another call" + (f" while {in_progress} are in progress" if in_progress else "")
    return None


# ----------------------------------------------------------------------------- limits


@dataclass(frozen=True)
class Verdict:
    action: str  # "dial" | "wait" | "stop"
    reason: str = ""


def _calls_since(s: Session, campaign_id: int, since_utc: datetime) -> int:
    return int(
        s.execute(
            select(func.count()).select_from(CallRun).where(
                CallRun.campaign_id == campaign_id, CallRun.direction == "outbound", CallRun.started_at >= since_utc
            )
        ).scalar()
        or 0
    )


# This many answered calls in a row without a word from the agent stop the campaign.
FAILING_CALLS_TO_STOP = 5
# What a call is recorded as when nobody, or only the person called, said anything.
NO_CONVERSATION_OUTCOMES = frozenset({None, "no_answer", "busy", "other"})


def provider_failure(s: Session, campaign: Campaign) -> str | None:
    """A circuit breaker. When the speech or AI provider is down (an outage, a revoked
    key), every answered call is silent: the person hears nothing and hangs up, the call
    looks like "no answer", and the lead is tried again. Left alone, a campaign would
    ring through its whole list like that, several times each.

    If the last answered calls since the campaign was (re)started all ended with no
    agent speech at all, or with a pipeline error, the campaign stops and says why.

    A call that ended with an outcome only a conversation produces (a meeting, "not
    interested", an opt-out) is proof the agent worked, whatever its transcript holds."""
    since = campaign.updated_at  # starting a campaign updates it: earlier calls do not count against a fresh start
    query = select(CallRun.error, CallRun.transcript, CallRun.disposition).where(
        CallRun.campaign_id == campaign.id, CallRun.direction == "outbound", CallRun.stream_connected.is_(True), CallRun.ended_at.is_not(None)
    )
    if since is not None:
        query = query.where(CallRun.started_at >= since)
    recent = s.execute(query.order_by(CallRun.id.desc()).limit(FAILING_CALLS_TO_STOP)).all()
    if len(recent) < FAILING_CALLS_TO_STOP:
        return None

    def silent(error: str | None, transcript: list | None, disposition: str | None) -> bool:
        spoke = any((line or {}).get("role") == "assistant" for line in transcript or [])
        conversed = disposition not in NO_CONVERSATION_OUTCOMES
        failed = bool(error) and not str(error).startswith("review failed")  # a failed review is after the call, not during it
        return failed or not (spoke or conversed)

    if all(silent(*row) for row in recent):
        return (
            f"the last {FAILING_CALLS_TO_STOP} answered calls had no agent speech or failed during the call: "
            "the speech or AI provider is probably not working. Check Setup, then start the campaign again"
        )
    return None


def check_limits(s: Session, campaign: Campaign, settings: Settings, active_calls: int, now: datetime | None = None) -> Verdict:
    """May this campaign place one more call right now?

    ``stop`` ends the campaign with the reason shown to the owner; ``wait`` leaves it
    running and tries again later (tomorrow, next month, when a call in progress ends).
    """
    rules = rules_of(campaign)
    now = now or utcnow()

    if rules.ends_at and now >= rules.ends_at:
        return Verdict("stop", "the campaign's end date has passed")
    if rules.starts_at and now < rules.starts_at:
        return Verdict("wait", "the campaign has not reached its start date")

    if rules.stop_after_meetings:
        booked = int(
            s.execute(
                select(func.count()).select_from(Appointment).join(Lead, Lead.id == Appointment.lead_id).where(Lead.campaign_id == campaign.id)
            ).scalar()
            or 0
        )
        if booked >= rules.stop_after_meetings:
            return Verdict("stop", f"the target of {rules.stop_after_meetings} booked meetings was reached")

    if rules.max_opt_out_rate:
        answered = int(
            s.execute(select(func.count()).select_from(CallRun).where(CallRun.campaign_id == campaign.id, CallRun.stream_connected.is_(True))).scalar() or 0
        )
        if answered >= rules.opt_out_min_calls:
            opted_out = int(
                s.execute(
                    select(func.count()).select_from(CallRun).where(CallRun.campaign_id == campaign.id, CallRun.disposition == "do_not_call")
                ).scalar()
                or 0
            )
            if opted_out / answered > rules.max_opt_out_rate:
                return Verdict("stop", f"{opted_out} of {answered} answered calls asked not to be called again, above the {rules.max_opt_out_rate:.0%} limit")

    failing = provider_failure(s, campaign)
    if failing:
        return Verdict("stop", failing)

    if rules.budget_usd is not None:
        spent = campaign_spend(s, campaign.id)
        if spent >= rules.budget_usd:
            return Verdict("stop", f"the budget of ${rules.budget_usd:,.2f} was reached (spent ${spent:,.2f})")
        # A call can run to the time limit. Count calls in progress, and the one about to
        # be placed, at that worst case so the budget is never overshot.
        worst_case = call_cost(settings.call_time_limit_seconds, settings)
        if spent + (active_calls + 1) * worst_case > rules.budget_usd:
            if active_calls == 0:
                return Verdict("stop", f"the remaining budget (${rules.budget_usd - spent:,.2f}) does not cover one more call")
            return Verdict("wait", "waiting for calls in progress to finish before spending the rest of the budget")

    try:
        tz = ZoneInfo(settings.default_timezone)
    except Exception:
        tz = timezone.utc
    local = now.astimezone(tz)
    if rules.max_calls_per_day:
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if _calls_since(s, campaign.id, midnight.astimezone(timezone.utc)) >= rules.max_calls_per_day:
            return Verdict("wait", f"today's limit of {rules.max_calls_per_day} calls was reached")
    if rules.max_calls_per_month:
        first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if _calls_since(s, campaign.id, first.astimezone(timezone.utc)) >= rules.max_calls_per_month:
            return Verdict("wait", f"this month's limit of {rules.max_calls_per_month} calls was reached")
    return Verdict("dial")


def start_problems(campaign: Campaign, settings: Settings, now: datetime | None = None) -> list[str]:
    """Reasons a campaign must not be started with its current rules."""
    rules = rules_of(campaign)
    problems = []
    if rules.budget_usd is not None and settings.cost_per_connected_minute_usd <= 0:
        problems.append(
            "this campaign has a budget, but the cost of a call is not configured, so spend cannot be tracked: "
            "set COST_PER_CONNECTED_MINUTE_USD (and COST_PER_CALL_ATTEMPT_USD if attempts are billed)"
        )
    if rules.ends_at and (now or utcnow()) >= rules.ends_at:
        problems.append("the campaign's end date has already passed")
    return problems


def describe(campaign: Campaign, settings: Settings) -> dict[str, Any]:
    """The rules actually in force (campaign values, or the global default where unset)."""
    rules = rules_of(campaign)
    return {
        "calling_days": [DAY_NAMES[d] for d in (rules.calling_days or DEFAULT_CALLING_DAYS)],
        "window_start": rules.window_start or f"{settings.calling_window_start:%H:%M}",
        "window_end": rules.window_end or f"{settings.calling_window_end:%H:%M}",
        "max_attempts": rules.max_attempts or settings.max_attempts,
        "retry_delay_minutes": rules.retry_delay_minutes or settings.retry_delay_minutes,
        "max_calls_per_day": rules.max_calls_per_day,
        "max_calls_per_month": rules.max_calls_per_month,
        "budget_usd": rules.budget_usd,
        "starts_at": rules.starts_at.isoformat() if rules.starts_at else None,
        "ends_at": rules.ends_at.isoformat() if rules.ends_at else None,
        "stop_after_meetings": rules.stop_after_meetings,
        "max_opt_out_rate": rules.max_opt_out_rate,
        "opt_out_min_calls": rules.opt_out_min_calls,
        "scoring_weights": rules.scoring_weights,
    }
