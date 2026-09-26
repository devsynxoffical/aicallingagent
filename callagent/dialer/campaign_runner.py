"""The dialer: pulls due leads, respects calling hours and concurrency, places calls."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import CallRun, Campaign, Lead, is_dnc, run_db, utcnow
from . import twilio_client

log = logging.getLogger(__name__)

ACTIVE_CALL_STATUSES = ("created", "initiated", "ringing", "in-progress")


def in_calling_window(now_utc: datetime, tz_name: str | None, start: time, end: time, default_tz: str) -> bool:
    """Is it an acceptable local time to call this lead?"""
    try:
        tz = ZoneInfo(tz_name or default_tz)
    except Exception:
        tz = ZoneInfo(default_tz)
    local = now_utc.astimezone(tz)
    if local.weekday() >= 6:  # Sundays off by default
        return False
    t = local.time()
    return start <= t <= end


def next_window_open(now_utc: datetime, tz_name: str | None, start: time, default_tz: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name or default_tz)
    except Exception:
        tz = ZoneInfo(default_tz)
    local = now_utc.astimezone(tz)
    candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    while candidate <= local or candidate.weekday() >= 6:
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=start.hour, minute=start.minute)
    return candidate.astimezone(timezone.utc)


def pick_next_lead(s: Session, campaign_id: int, settings: Settings) -> Lead | None:
    now = utcnow()
    candidates = (
        s.execute(
            select(Lead)
            .where(
                Lead.campaign_id == campaign_id,
                Lead.status.in_(("pending", "callback")),
                Lead.attempts < settings.max_attempts,
                (Lead.next_attempt_at.is_(None)) | (Lead.next_attempt_at <= now),
            )
            .order_by(Lead.status.desc(), Lead.next_attempt_at.asc().nulls_last(), Lead.id.asc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    for lead in candidates:
        if is_dnc(s, lead.phone_e164):
            lead.status = "dnc"
            continue
        if in_calling_window(now, lead.timezone, settings.calling_window_start, settings.calling_window_end, settings.default_timezone):
            return lead
        # Outside their hours: push to the next window so we don't rescan it every tick.
        lead.next_attempt_at = next_window_open(now, lead.timezone, settings.calling_window_start, settings.default_timezone)
    return None


def count_active_calls(s: Session, campaign_id: int) -> int:
    return len(
        s.execute(
            select(CallRun.id).where(CallRun.campaign_id == campaign_id, CallRun.status.in_(ACTIVE_CALL_STATUSES))
        ).all()
    )


def reap_stale_calls(s: Session, campaign_id: int, older_than_minutes: int = 20) -> int:
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
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
    for run in stale:
        run.status = "failed"
        run.error = (run.error or "") + " stale: no status update"
        run.ended_at = utcnow()
        lead = s.get(Lead, run.lead_id)
        if lead and lead.status == "calling":
            lead.status = "pending"
            lead.next_attempt_at = utcnow() + timedelta(minutes=30)
    return len(stale)


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

    async def stop_all(self) -> None:
        for cid in list(self._tasks):
            await self.stop(cid)

    async def dial_lead(self, campaign_id: int, lead_id: int) -> int:
        """Create a CallRun and place the call. Returns the CallRun id."""

        def _prepare(s):
            lead = s.get(Lead, lead_id)
            if lead is None:
                raise ValueError("lead not found")
            lead.status = "calling"
            lead.attempts += 1
            run = CallRun(lead_id=lead.id, campaign_id=campaign_id, status="created")
            s.add(run)
            s.flush()
            return run.id, lead.phone_e164

        run_id, phone = await run_db(_prepare)
        try:
            sid = await twilio_client.place_call_async(run_id, phone, self.settings)
        except Exception as e:
            log.exception("Twilio call failed for lead %s", lead_id)
            err = str(e)

            def _fail(s):
                run = s.get(CallRun, run_id)
                lead = s.get(Lead, lead_id)
                if run:
                    run.status = "failed"
                    run.error = err
                    run.ended_at = utcnow()
                if lead:
                    lead.status = "failed" if lead.attempts >= self.settings.max_attempts else "pending"
                    lead.next_attempt_at = utcnow() + timedelta(minutes=self.settings.retry_delay_minutes)

            await run_db(_fail)
            raise

        def _sid(s):
            run = s.get(CallRun, run_id)
            if run:
                run.twilio_call_sid = sid
                run.status = "initiated"

        await run_db(_sid)
        return run_id

    async def _loop(self, campaign_id: int) -> None:
        log.info("campaign %s: dialer started", campaign_id)
        idle_ticks = 0
        while True:
            try:
                snapshot = await run_db(lambda s: self._tick_snapshot(s, campaign_id))
                if snapshot is None:
                    await self.stop(campaign_id, status="done")
                    return
                max_conc, active, lead = snapshot
                if active >= max_conc or lead is None:
                    idle_ticks += 1
                    if lead is None and active == 0 and idle_ticks % 30 == 0:
                        remaining = await run_db(lambda s: self._remaining(s, campaign_id))
                        if remaining == 0:
                            log.info("campaign %s: all leads worked, finishing", campaign_id)
                            await self.stop(campaign_id, status="done")
                            return
                    await asyncio.sleep(5)
                    continue
                idle_ticks = 0
                try:
                    await self.dial_lead(campaign_id, lead["id"])
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

    def _tick_snapshot(self, s: Session, campaign_id: int) -> tuple[int, int, dict[str, Any] | None] | None:
        campaign = s.get(Campaign, campaign_id)
        if campaign is None or campaign.status not in ("running",):
            return None
        reap_stale_calls(s, campaign_id)
        max_conc = campaign.max_concurrent_calls or self.settings.max_concurrent_calls
        active = count_active_calls(s, campaign_id)
        lead = pick_next_lead(s, campaign_id, self.settings) if active < max_conc else None
        return max_conc, active, ({"id": lead.id, "phone": lead.phone_e164} if lead else None)

    def _remaining(self, s: Session, campaign_id: int) -> int:
        return len(
            s.execute(
                select(Lead.id).where(
                    Lead.campaign_id == campaign_id,
                    Lead.status.in_(("pending", "callback", "queued", "calling")),
                    Lead.attempts < self.settings.max_attempts,
                )
            ).all()
        )


def apply_twilio_status(s: Session, call_run_id: int, form: dict[str, Any], settings: Settings) -> None:
    """Handle a Twilio status callback (initiated/ringing/answered/completed + final outcomes)."""
    run = s.get(CallRun, call_run_id)
    if run is None:
        return
    status = str(form.get("CallStatus", "")).lower()
    run.twilio_call_sid = run.twilio_call_sid or form.get("CallSid")
    if form.get("AnsweredBy"):
        run.answered_by = form.get("AnsweredBy")
    if status:
        run.status = status
    if form.get("RecordingUrl"):
        run.recording_url = form.get("RecordingUrl")
    if status in ("completed", "busy", "no-answer", "failed", "canceled"):
        run.ended_at = run.ended_at or utcnow()
        dur = form.get("CallDuration")
        if dur and str(dur).isdigit():
            run.duration_seconds = int(dur)
        lead = s.get(Lead, run.lead_id)
        if lead and lead.status == "calling":
            # Nobody talked to the agent: schedule a retry or give up.
            if status != "completed" or not run.transcript:
                run.disposition = run.disposition or "no_answer"
                lead.last_disposition = run.disposition
                if lead.attempts >= settings.max_attempts:
                    lead.status = "failed" if status == "failed" else "completed"
                else:
                    lead.status = "pending"
                    lead.next_attempt_at = utcnow() + timedelta(minutes=settings.retry_delay_minutes)
