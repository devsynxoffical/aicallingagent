"""The work that runs on the durable job queue (see jobs.py), and the process's worker.

Each handler is safe to run twice: jobs are delivered at least once.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx
from sqlalchemy import select

from . import jobs
from .config import get_settings
from .db import CallRun, Campaign, Job, Lead, TrainingRun, run_db, utcnow

log = logging.getLogger(__name__)

worker: jobs.Worker | None = None
_kicks: set[asyncio.Task] = set()


def start_worker() -> jobs.Worker:
    global worker
    worker = jobs.Worker()
    worker.start()
    return worker


async def stop_worker() -> None:
    global worker
    if worker is not None:
        await worker.stop()
        worker = None


def kick(key: str) -> None:
    """Run a job that was just committed now, without making the caller wait for it.
    If there is no worker, or the process stops first, the queue still holds the job."""
    if worker is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(worker.run_job_now(key))
    except RuntimeError:
        return
    _kicks.add(task)
    task.add_done_callback(_kicks.discard)


async def run_now(key: str) -> None:
    """Run a committed job in this task and wait for it (for FastAPI background tasks)."""
    await (worker or jobs.Worker()).run_job_now(key)


# ----------------------------------------------------------------------------- training runs

TRAINING_RUN = "training_run"
TRAINING_INTERRUPTED = "the training run was interrupted and could not be completed; start it again"


def training_run_key(run_id: int) -> str:
    return f"{TRAINING_RUN}:{run_id}"


async def _training_gave_up(payload: dict[str, Any], error: str) -> None:
    def _mark(s):
        run = s.get(TrainingRun, int(payload["run_id"]))
        if run is not None and run.status == "running":
            run.status, run.error, run.finished_at = "failed", TRAINING_INTERRUPTED, utcnow()

    await run_db(_mark)


@jobs.handler(TRAINING_RUN, on_dead=_training_gave_up)
async def run_training_job(payload: dict[str, Any]) -> None:
    """Build a draft playbook from the campaign's material. If the process stops halfway,
    the run is still marked running and this job is taken over and run again."""
    from .training import api as training_api

    run_id = int(payload["run_id"])
    if not await run_db(lambda s: getattr(s.get(TrainingRun, run_id), "status", None) == "running"):
        return  # already finished by an earlier attempt
    await training_api.run_training(run_id)


async def recover_training_runs() -> int:
    """A run marked running that no job stands behind can never finish (it was started
    by a release before the job queue, and that process is gone). Close it, so the
    campaign can be trained again."""

    def _close(s):
        closed = 0
        for run in s.execute(select(TrainingRun).where(TrainingRun.status == "running")).scalars():
            job = s.execute(select(Job).where(Job.key == training_run_key(run.id))).scalars().first()
            if job is None or job.status in ("done", "dead"):
                run.status, run.error, run.finished_at = "failed", TRAINING_INTERRUPTED, utcnow()
                closed += 1
        return closed

    return await run_db(_close)


# ----------------------------------------------------------------------------- booking notifications

BOOKING_WEBHOOK = "booking_webhook"


def booking_webhook_key(appointment_id: int) -> str:
    return f"{BOOKING_WEBHOOK}:appointment:{appointment_id}"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@jobs.handler(BOOKING_WEBHOOK)
async def deliver_booking_webhook(payload: dict[str, Any]) -> None:
    """Tell the customer's system about a booked meeting. Anything but a 2xx answer is a
    failure and is retried. The receiver can de-duplicate on X-Callagent-Delivery, which
    is the same for every retry of one booking."""
    settings = get_settings()
    url = settings.booking_webhook_url
    if not url:
        return  # the webhook was switched off after the booking: nothing to deliver
    body = json.dumps(payload["body"], ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Callagent-Event": "appointment.booked", "X-Callagent-Delivery": payload["delivery_id"]}
    if settings.booking_webhook_secret:
        headers["X-Callagent-Signature"] = sign(settings.booking_webhook_secret, body)
    async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
        response = await client.post(url, content=body, headers=headers)
    if response.status_code >= 300:
        raise RuntimeError(f"the booking webhook answered {response.status_code}")


# ----------------------------------------------------------------------------- call review safety net

CALL_REVIEW = "call_review"
REVIEW_SAFETY_NET_DELAY_SECONDS = 600


def call_review_key(call_run_id: int) -> str:
    return f"{CALL_REVIEW}:call:{call_run_id}"


@jobs.handler(CALL_REVIEW)
async def review_call_later(payload: dict[str, Any]) -> None:
    """Review a call whose review did not finish when the call ended (the model was
    unreachable, or the process stopped). Works from the stored transcript.

    The lead's place in the dial queue was already decided when the call ended, and the
    lead may have been called again since, so a late review adds what it learned (the
    review, the evidence and score, the facts) and leaves the lead's status alone.
    """
    from .dialer.rules import rules_of
    from .intelligence.scoring import apply_evidence
    from .playbook.schema import Playbook
    from .postcall import summarizer
    from .voice.pipeline import _playbook_for

    call_run_id = int(payload["call_run_id"])
    settings = get_settings()

    def _load(s):
        run = s.get(CallRun, call_run_id)
        if run is None or (run.summary or {}).get("review") or not (run.transcript or run.mode == "voicemail"):
            return None
        campaign = s.get(Campaign, run.campaign_id)
        if campaign is None or not campaign.playbook_json:
            return None
        playbook: Playbook = _playbook_for(s, campaign, run)
        who = {"user": "Prospect", "assistant": playbook.persona.agent_name}
        text = "\n".join(f"{who.get(t['role'], t['role'])}: {t['text']}" for t in run.transcript or [])
        return playbook, text, list(run.tool_events or []), run.mode

    loaded = await run_db(_load)
    if loaded is None:
        return
    playbook, text, tool_events, mode = loaded
    client = summarizer.make_client(settings)
    review = await asyncio.to_thread(summarizer.review_call, client, playbook, text, tool_events, mode, settings)

    def _store(s):
        run = s.get(CallRun, call_run_id)
        if run is None or (run.summary or {}).get("review"):
            return
        run.summary = {**(run.summary or {}), "review": review.model_dump()}
        if run.error and run.error.startswith("review failed"):
            run.error = None
        if run.disposition not in summarizer.TERMINAL_DISPOSITIONS:
            run.disposition = review.disposition
        lead = s.get(Lead, run.lead_id)
        if lead is None:
            return
        latest = s.execute(select(CallRun.id).where(CallRun.lead_id == lead.id).order_by(CallRun.id.desc()).limit(1)).scalar()
        if latest == run.id and lead.status != "dnc":
            lead.last_disposition = run.disposition
        campaign = s.get(Campaign, run.campaign_id)
        apply_evidence(lead, review.evidence.model_dump(), rules_of(campaign).scoring_weights if campaign else None)
        if review.prospect_facts:
            lead.notes = (lead.notes + "\n" if lead.notes else "") + f"[{run.started_at:%Y-%m-%d}] " + "; ".join(review.prospect_facts)

    await run_db(_store)
