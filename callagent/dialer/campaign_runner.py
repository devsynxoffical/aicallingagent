"""The dialer: pulls due leads, respects calling hours and concurrency, places calls."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import ACTIVE_CALL_STATUSES, TERMINAL_CALL_STATUSES, CallRun, Campaign, Lead, is_dnc, run_db, utcnow
from . import lifecycle
from . import rules as campaign_rules
from . import twilio_client
from .rules import DEFAULT_CALLING_DAYS, campaign_settings

log = logging.getLogger(__name__)

# Lead states the dialer may call from on its own, and states nobody may call from.
AUTO_DIALABLE = ("pending", "callback")
NEVER_DIALABLE = ("calling", "queued", "dnc")


class LeadNotDialable(Exception):
    """This lead must not be called right now. The message says why, in words for the operator."""


def claim_lead_for_call(s: Session, campaign_id: int, lead_id: int, *, manual: bool = False) -> tuple[int, str]:
    """Reserve a lead and open its call record, or raise :class:`LeadNotDialable`.

    Everything that decides "may this person be rung now" happens here, in one
    transaction, so two attempts for the same lead cannot both pass:

    - the lead's status moves to ``calling`` with a conditional UPDATE, and only one
      UPDATE can win;
    - a lead with a call that has not finished is refused, whichever direction the call
      has (never ring someone who is on the line with us);
    - the do-not-call list is read again here, not only when the lead was picked;
    - the database itself allows one unfinished outbound call per lead.

    ``manual`` is an operator dialing one number on purpose: allowed from any state except
    a call in progress or a suppressed number. Returns (call_run_id, phone number).
    """
    lead = s.get(Lead, lead_id)
    if lead is None or lead.campaign_id != campaign_id:
        raise LeadNotDialable("lead not found in this campaign")
    if is_dnc(s, lead.phone_e164, lead.organization_id) or lead.status == "dnc":
        if lead.status != "dnc":
            lead.status = "dnc"
            s.commit()  # the suppression is kept even though the dial is refused
        raise LeadNotDialable(f"{lead.phone_e164} is on the do-not-call list")
    allowed = Lead.status.not_in(NEVER_DIALABLE) if manual else Lead.status.in_(AUTO_DIALABLE)
    claimed = s.execute(
        update(Lead).where(Lead.id == lead_id, Lead.campaign_id == campaign_id, allowed).values(status="calling", attempts=Lead.attempts + 1)
    ).rowcount
    if claimed != 1:
        raise LeadNotDialable("this lead is already being called or is not due for a call")
    unfinished = s.execute(select(CallRun.id).where(CallRun.lead_id == lead_id, CallRun.status.in_(ACTIVE_CALL_STATUSES)).limit(1)).first()
    if unfinished is not None:
        raise LeadNotDialable(f"call {unfinished[0]} to this lead has not finished yet")
    from ..intelligence import experiments

    s.refresh(lead)
    campaign = s.get(Campaign, campaign_id)
    # In an A/B test the lead's side, and so the playbook version, is fixed for every attempt.
    experiment_id, arm, version = experiments.assign(s, campaign, lead) if campaign else (None, None, None)
    run = CallRun(lead_id=lead.id, campaign_id=campaign_id, status="created", playbook_version=version, experiment_id=experiment_id, experiment_arm=arm)
    s.add(run)
    try:
        s.flush()
    except IntegrityError:
        raise LeadNotDialable("another call to this lead was started at the same moment") from None
    return run.id, lead.phone_e164


def in_calling_window(
    now_utc: datetime, tz_name: str | None, start: time, end: time, default_tz: str, days: tuple[int, ...] = DEFAULT_CALLING_DAYS
) -> bool:
    """Is it an acceptable local time to call this lead?"""
    try:
        tz = ZoneInfo(tz_name or default_tz)
    except Exception:
        tz = ZoneInfo(default_tz)
    local = now_utc.astimezone(tz)
    if local.weekday() not in days:  # Sundays off unless the campaign says otherwise
        return False
    t = local.time()
    return start <= t <= end


def next_window_open(
    now_utc: datetime, tz_name: str | None, start: time, default_tz: str, days: tuple[int, ...] = DEFAULT_CALLING_DAYS
) -> datetime:
    try:
        tz = ZoneInfo(tz_name or default_tz)
    except Exception:
        tz = ZoneInfo(default_tz)
    local = now_utc.astimezone(tz)
    candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    while candidate <= local or candidate.weekday() not in days:
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=start.hour, minute=start.minute)
    return candidate.astimezone(timezone.utc)


def pick_next_lead(s: Session, campaign_id: int, settings: Settings) -> Lead | None:
    now = utcnow()
    settings = campaign_settings(s, campaign_id, settings)
    days = campaign_rules.calling_days(s.get(Campaign, campaign_id))
    # Callbacks the prospect asked for are honored even past max_attempts, and go first.
    candidates = (
        s.execute(
            select(Lead)
            .where(
                Lead.campaign_id == campaign_id,
                Lead.status.in_(("pending", "callback")),
                or_(Lead.attempts < settings.max_attempts, Lead.status == "callback"),
                (Lead.next_attempt_at.is_(None)) | (Lead.next_attempt_at <= now),
            )
            .order_by(
                case((Lead.status == "callback", 0), else_=1),
                Lead.priority.desc(),
                Lead.next_attempt_at.asc().nulls_last(),
                Lead.id.asc(),
            )
            .limit(50)
        )
        .scalars()
        .all()
    )
    for lead in candidates:
        if is_dnc(s, lead.phone_e164, lead.organization_id):
            lead.status = "dnc"
            continue
        if in_calling_window(now, lead.timezone, settings.calling_window_start, settings.calling_window_end, settings.default_timezone, days):
            return lead
        # Outside their hours: push to the next window so we don't rescan it every tick.
        lead.next_attempt_at = next_window_open(now, lead.timezone, settings.calling_window_start, settings.default_timezone, days)
    return None


def count_active_calls(s: Session, campaign_id: int) -> int:
    return len(
        s.execute(
            select(CallRun.id).where(CallRun.campaign_id == campaign_id, CallRun.status.in_(ACTIVE_CALL_STATUSES))
        ).all()
    )


def release_inbound_lead(run: CallRun, lead: Lead) -> None:
    """An inbound call ended without a conversation (caller hung up, stream never came
    up). Put the lead back exactly where it was: an inbound call must never, by itself,
    queue an outbound call to that person."""
    if lead.status == "calling":
        lead.status = (run.summary or {}).get("lead_status_before") or "completed"


def reap_stale_calls(s: Session, campaign_id: int, settings: Settings | None = None) -> int:
    """Two kinds of leftovers: calls that never got a final status, and leads left in
    "calling" although their call already ended (process crash mid-call, lost webhook)."""
    settings = campaign_settings(s, campaign_id, settings or get_settings())
    older_than_minutes = max(20, settings.call_time_limit_seconds // 60 + 5)
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    stuck_leads = (
        s.execute(select(Lead).where(Lead.campaign_id == campaign_id, Lead.status == "calling")).scalars().all()
    )
    recovered = 0
    for lead in stuck_leads:
        last = s.execute(select(CallRun).where(CallRun.lead_id == lead.id).order_by(CallRun.id.desc()).limit(1)).scalar_one_or_none()
        ended = last is not None and last.status in TERMINAL_CALL_STATUSES and last.ended_at is not None
        if ended and last.ended_at < utcnow() - timedelta(minutes=5) and last.direction == "inbound":
            release_inbound_lead(last, lead)
            recovered += 1
        elif ended and last.ended_at < utcnow() - timedelta(minutes=5):
            lead.status = "pending" if lead.attempts < settings.max_attempts else "completed"
            lead.next_attempt_at = utcnow() + timedelta(minutes=settings.retry_delay_minutes)
            recovered += 1
    stale = (
        s.execute(
            select(CallRun).where(
                CallRun.campaign_id == campaign_id,
                CallRun.status.in_(ACTIVE_CALL_STATUSES),
                CallRun.started_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    if settings.twilio_account_sid and settings.twilio_auth_token:
        # A provider is configured: only its answer may end a call (see dialer/reconcile.py).
        return recovered
    for run in stale:
        lifecycle.advance(run, "failed")
        run.error = (run.error or "") + " stale: no status update"
        lead = s.get(Lead, run.lead_id)
        if lead and run.direction == "inbound":
            release_inbound_lead(run, lead)
        elif lead and lead.status == "calling":
            lead.status = "pending"
            lead.next_attempt_at = utcnow() + timedelta(minutes=30)
    return len(stale) + recovered


class CampaignRunner:
    """One asyncio task per running campaign. Lives inside the FastAPI process."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._tasks: dict[int, asyncio.Task] = {}

    def is_running(self, campaign_id: int) -> bool:
        t = self._tasks.get(campaign_id)
        return t is not None and not t.done()

    async def start(self, campaign_id: int) -> None:
        if self.is_running(campaign_id):
            return

        def _mark(s):
            c = s.get(Campaign, campaign_id)
            if c:
                c.status = "running"
                c.stop_reason = None

        await run_db(_mark)
        self._tasks[campaign_id] = asyncio.create_task(self._loop(campaign_id), name=f"campaign-{campaign_id}")

    async def stop(self, campaign_id: int, status: str = "paused") -> None:
        t = self._tasks.pop(campaign_id, None)
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        def _mark(s):
            c = s.get(Campaign, campaign_id)
            if c:
                c.status = status

        await run_db(_mark)

    async def _finish(self, campaign_id: int, status: str, reason: str | None = None) -> None:
        """Called from inside the loop task itself: mark the campaign and let the task return."""
        self._tasks.pop(campaign_id, None)

        def _mark(s):
            c = s.get(Campaign, campaign_id)
            if c:
                c.status = status
                if reason:
                    c.stop_reason = reason[:300]

        await run_db(_mark)

    async def shutdown(self) -> None:
        """The process is stopping: end the dial loops and leave every campaign's status
        alone, so campaigns that were running resume when the process starts again."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def dial_lead(self, campaign_id: int, lead_id: int, *, manual: bool = False) -> int:
        """Reserve the lead, then place the call. Returns the CallRun id.
        Raises :class:`LeadNotDialable` when the lead must not be rung now."""
        run_id, phone = await run_db(lambda s: claim_lead_for_call(s, campaign_id, lead_id, manual=manual))
        try:
            sid = await twilio_client.place_call_async(run_id, phone, self.settings)
        except Exception as e:
            log.exception("Twilio call failed for lead %s", lead_id)
            err = str(e)

            def _fail(s):
                run = s.get(CallRun, run_id)
                lead = s.get(Lead, lead_id)
                effective = campaign_settings(s, campaign_id, self.settings)
                if run and not twilio_client.definitely_not_placed(e):
                    # No answer is not a "no": the provider may have placed the call. The lead
                    # stays reserved and reconciliation asks the provider before anyone redials.
                    run.error = f"the provider did not confirm the call ({err}); awaiting reconciliation"
                    return
                if run:
                    lifecycle.advance(run, "failed")
                    run.error = err
                if lead:
                    lead.status = "failed" if lead.attempts >= effective.max_attempts else "pending"
                    lead.next_attempt_at = utcnow() + timedelta(minutes=effective.retry_delay_minutes)

            await run_db(_fail)
            raise

        def _sid(s):
            run = s.get(CallRun, run_id)
            if run:
                run.twilio_call_sid = run.twilio_call_sid or sid
                lifecycle.advance(run, "initiated")  # no effect if a status webhook already moved it further

        await run_db(_sid)
        return run_id

    async def _loop(self, campaign_id: int) -> None:
        log.info("campaign %s: dialer started", campaign_id)
        idle_ticks = 0
        while True:
            try:
                snapshot = await run_db(lambda s: self._tick_snapshot(s, campaign_id))
                if snapshot is None:
                    await self._finish(campaign_id, status="paused")
                    return
                max_conc, active, lead, stop_reason = snapshot
                if stop_reason:
                    log.info("campaign %s: stopping, %s", campaign_id, stop_reason)
                    await self._finish(campaign_id, status="paused", reason=stop_reason)
                    return
                if active >= max_conc or lead is None:
                    idle_ticks += 1
                    if lead is None and active == 0 and idle_ticks % 30 == 0:
                        remaining = await run_db(lambda s: self._remaining(s, campaign_id))
                        if remaining == 0:
                            log.info("campaign %s: all leads worked, finishing", campaign_id)
                            await self._finish(campaign_id, status="done")
                            return
                    await asyncio.sleep(5)
                    continue
                idle_ticks = 0
                try:
                    await self.dial_lead(campaign_id, lead["id"])
                except LeadNotDialable as e:
                    # Someone else has this lead (a manual dial, an inbound call): take the next one.
                    log.info("campaign %s: lead %s skipped, %s", campaign_id, lead["id"], e)
                    await asyncio.sleep(1)
                    continue
                except Exception:
                    await asyncio.sleep(10)
                    continue
                await asyncio.sleep(self.settings.seconds_between_dials)
            except asyncio.CancelledError:
                log.info("campaign %s: dialer stopped", campaign_id)
                raise
            except Exception:
                log.exception("campaign %s: dialer tick failed", campaign_id)
                await asyncio.sleep(10)

    def _tick_snapshot(self, s: Session, campaign_id: int) -> tuple[int, int, dict[str, Any] | None, str | None] | None:
        """(concurrency cap, calls in progress, next lead or None, reason to stop or None)."""
        campaign = s.get(Campaign, campaign_id)
        if campaign is None or campaign.status not in ("running",):
            return None
        reap_stale_calls(s, campaign_id, self.settings)
        max_conc = campaign.max_concurrent_calls or self.settings.max_concurrent_calls
        active = count_active_calls(s, campaign_id)
        # Budget, schedule, caps and stop conditions are checked before every single dial.
        verdict = campaign_rules.check_limits(s, campaign, self.settings, active)
        if verdict.action == "stop":
            return max_conc, active, None, verdict.reason
        if verdict.action == "wait":
            return max_conc, active, None, None
        lead = pick_next_lead(s, campaign_id, self.settings) if active < max_conc else None
        return max_conc, active, ({"id": lead.id, "phone": lead.phone_e164} if lead else None), None

    def _remaining(self, s: Session, campaign_id: int) -> int:
        max_attempts = campaign_settings(s, campaign_id, self.settings).max_attempts
        return len(
            s.execute(
                select(Lead.id).where(
                    Lead.campaign_id == campaign_id,
                    Lead.status.in_(("pending", "callback", "queued", "calling")),
                    or_(Lead.attempts < max_attempts, Lead.status == "callback"),
                )
            ).all()
        )


def apply_twilio_status(s: Session, call_run_id: int, form: dict[str, Any], settings: Settings) -> None:
    """Handle a Twilio status callback (initiated/ringing/answered/completed + final outcomes)."""
    run = s.get(CallRun, call_run_id)
    if run is None:
        return
    settings = campaign_settings(s, run.campaign_id, settings)
    status = str(form.get("CallStatus", "")).lower()
    run.twilio_call_sid = run.twilio_call_sid or form.get("CallSid")
    if form.get("AnsweredBy"):
        run.answered_by = form.get("AnsweredBy")
    if status:
        lifecycle.advance(run, status, source=lifecycle.PROVIDER)  # repeated, late and out-of-order reports change nothing
    if form.get("RecordingUrl"):
        run.recording_url = form.get("RecordingUrl")
    if status in TERMINAL_CALL_STATUSES and run.status == status:
        dur = form.get("CallDuration")
        seconds = int(dur) if dur and str(dur).isdigit() else run.duration_seconds
        # The carrier's duration is the billed one. It is written to the ledger exactly
        # once; a repeated report, even with different numbers, changes neither the
        # ledger nor the call.
        usage = lifecycle.record_usage(s, run, seconds, campaign_rules.call_cost(seconds, settings))
        run.duration_seconds = usage.quantity_seconds if (seconds is not None or run.duration_seconds is not None) else None
        run.cost_usd = float(usage.amount_usd)
        lead = s.get(Lead, run.lead_id)
        if lead and run.direction == "inbound":
            if not run.stream_connected:
                release_inbound_lead(run, lead)
        elif lead and lead.status == "calling" and not run.stream_connected:
            # The media stream never reached us: nobody (and no voicemail) picked up.
            # If it did connect, post-call finalization owns the lead's next state.
            run.disposition = run.disposition or ("busy" if status == "busy" else "no_answer")
            lead.last_disposition = run.disposition
            if lead.attempts >= settings.max_attempts:
                lead.status = "failed" if status == "failed" else "completed"
            else:
                lead.status = "pending"
                lead.next_attempt_at = utcnow() + timedelta(minutes=settings.retry_delay_minutes)
