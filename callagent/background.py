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
from .db import CallRun, Campaign, Job, Lead, TrainingDocument, TrainingRun, default_organization_id, run_blocking, run_db, utcnow
from .llm import attributed

log = logging.getLogger(__name__)

worker: jobs.Worker | None = None
_kicks: set[asyncio.Task] = set()


def start_worker() -> jobs.Worker:
    global worker
    worker = jobs.Worker()
    worker.start()
    return worker


def worker_running() -> bool:
    return worker is not None and worker.running


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
            # When the model was the reason, say so: "try again later" is then the right advice.
            reason = error.split(": ", 1)[-1][:2000] if error.startswith("AITransientError") else TRAINING_INTERRUPTED
            run.status, run.error, run.finished_at = "failed", reason, utcnow()

    await run_db(_mark)


@jobs.handler(TRAINING_RUN, queue="knowledge", on_dead=_training_gave_up)
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


@jobs.handler(BOOKING_WEBHOOK, queue="calls")
async def deliver_booking_webhook(payload: dict[str, Any]) -> None:
    """Tell the customer's system about a booked meeting. Anything but a 2xx answer is a
    failure and is retried. The receiver can de-duplicate on X-Callagent-Delivery, which
    is the same for every retry of one booking."""
    from . import orgsettings

    from . import outbound
    from .db import Organization

    organization_id = payload.get("organization_id")  # absent on jobs queued before organizations had their own webhook

    def _settings(s):
        org_id = organization_id if organization_id is not None else default_organization_id(s)
        # An address typed in by an organization's admin must be a public one. The one in
        # the deployment's own environment was set by its operator, who may point inside.
        own = bool(orgsettings.of(s.get(Organization, org_id)).booking_webhook_url)
        return orgsettings.effective(s, org_id, get_settings()), own

    settings, set_by_the_organization = await run_db(_settings)
    url = settings.booking_webhook_url
    if not url:
        return  # the webhook was switched off after the booking: nothing to deliver
    body = json.dumps(payload["body"], ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Callagent-Event": "appointment.booked", "X-Callagent-Delivery": payload["delivery_id"]}
    if settings.booking_webhook_secret:
        headers["X-Callagent-Signature"] = sign(settings.booking_webhook_secret, body)
    try:
        status = await outbound.post(url, body, headers, timeout=settings.provider_timeout_seconds, public_only=set_by_the_organization)
    except outbound.DeliveryError as e:
        raise RuntimeError(f"the booking webhook could not be delivered: {e}") from None
    if status >= 300:
        raise RuntimeError(f"the booking webhook answered {status}")


# ----------------------------------------------------------------------------- call review safety net

CALL_REVIEW = "call_review"
REVIEW_SAFETY_NET_DELAY_SECONDS = 600


def call_review_key(call_run_id: int) -> str:
    return f"{CALL_REVIEW}:call:{call_run_id}"


@jobs.handler(CALL_REVIEW, queue="analysis")
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
        return playbook, text, list(run.tool_events or []), run.mode, campaign.id

    loaded = await run_db(_load)
    if loaded is None:
        return
    playbook, text, tool_events, mode, campaign_id = loaded
    client = summarizer.make_client(settings)
    with attributed(campaign_id=campaign_id, call_run_id=call_run_id):
        review = await run_blocking(summarizer.review_call, client, playbook, text, tool_events, mode, settings)

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


# ----------------------------------------------------------------------------- reading documents and websites

DOCUMENT_READ = "document_read"


def document_read_key(document_id: int) -> str:
    return f"{DOCUMENT_READ}:{document_id}"


def enqueue_document_read(s, document: TrainingDocument):
    """Queue the reading of a document in the transaction that stores it, so an upload
    that was accepted is always read, whatever happens to this process."""
    return jobs.enqueue(s, DOCUMENT_READ, document_read_key(document.id), {"document_id": document.id}, max_attempts=3, timeout_seconds=900, organization_id=document.organization_id)


@jobs.handler(DOCUMENT_READ, queue="knowledge")
async def read_document(payload: dict[str, Any]) -> None:
    """Read one uploaded file or website and index its passages. A document that cannot
    be read is marked failed with the reason (that is an answer, not a job failure); a
    document that is already read, or was removed, is left alone."""
    from .training import service

    document_id = int(payload["document_id"])
    if not await run_db(lambda s: getattr(s.get(TrainingDocument, document_id), "status", None) in ("pending", "processing")):
        return
    await service.process_document(document_id)


async def queue_unread_documents() -> int:
    """Documents waiting to be read that no job stands behind (uploaded under a release
    that read them outside the queue): give each one its job."""

    def _queue(s):
        waiting = list(s.execute(select(TrainingDocument).where(TrainingDocument.status.in_(("pending", "processing")))).scalars())
        for document in waiting:
            enqueue_document_read(s, document)
        return len(waiting)

    return await run_db(_queue)


# ----------------------------------------------------------------------------- finishing interrupted calls

CALL_FINALIZE = "call_finalize"
# How long after the longest a call can last the safety net looks at it.
FINALIZE_MARGIN_SECONDS = 120
# A call whose normal ending started this recently may still be finishing (its review can take a while).
FINALIZE_IN_PROGRESS_SECONDS = 600


def call_finalize_key(call_run_id: int) -> str:
    return f"{CALL_FINALIZE}:call:{call_run_id}"


def enqueue_call_finalize(s, run: CallRun, settings) -> None:
    """Called when a call's audio stream connects, in that same transaction. If the
    process then stops, or the normal end-of-call handling fails, this job gives the
    call its outcome later from the transcript saved so far, and releases the lead.
    The normal ending completes the job, so it usually never runs."""
    jobs.enqueue(
        s, CALL_FINALIZE, call_finalize_key(run.id), {"call_run_id": run.id},
        delay_seconds=settings.call_time_limit_seconds + FINALIZE_MARGIN_SECONDS, max_attempts=6, timeout_seconds=420, call_run_id=run.id,
    )


@jobs.handler(CALL_FINALIZE, queue="calls")
async def finalize_interrupted_call(payload: dict[str, Any]) -> None:
    """Finish a call that connected but never got an outcome: the process stopped
    during it, or its ending failed. Works whatever state its campaign is in."""
    from .postcall import summarizer
    from .voice.pipeline import _playbook_for

    call_run_id = int(payload["call_run_id"])
    settings = get_settings()

    def _load(s):
        run = s.get(CallRun, call_run_id)
        if run is None or run.disposition is not None:
            return None  # finished normally, or by an earlier attempt
        if run.ended_at is not None and (utcnow() - run.ended_at).total_seconds() < FINALIZE_IN_PROGRESS_SECONDS:
            return "wait"
        campaign = s.get(Campaign, run.campaign_id)
        if campaign is None or not campaign.playbook_json:
            return None
        return _playbook_for(s, campaign, run), list(run.transcript or []), list(run.tool_events or []), run.mode or "live", run.direction or "outbound"

    loaded = await run_db(_load)
    if loaded is None:
        return
    if loaded == "wait":
        raise RuntimeError("the call's normal ending may still be in progress; looking again later")
    playbook, transcript, tool_events, mode, direction = loaded
    log.warning("call %s was interrupted before it was finalized; finishing it from the %s transcript lines saved", call_run_id, len(transcript))
    await summarizer.finalize_call(call_run_id, playbook, transcript, tool_events, mode, None, settings, direction=direction)


# ----------------------------------------------------------------------------- the provider's price for a call

PROVIDER_PRICE = "provider_price"
PROVIDER_PRICE_DELAY_SECONDS = 600


def enqueue_provider_price(s, run: CallRun) -> Job:
    return jobs.enqueue(s, PROVIDER_PRICE, f"provider-price:{run.id}", {"call_run_id": run.id}, delay_seconds=PROVIDER_PRICE_DELAY_SECONDS,
                        max_attempts=6, timeout_seconds=60, call_run_id=run.id)


@jobs.handler(PROVIDER_PRICE, queue="default")
async def fetch_provider_price(payload: dict[str, Any], provider=None) -> None:
    """Ask the phone provider what it charged for a call and correct the ledger to it.
    Not priced yet is a failure of this attempt, so the queue asks again later."""
    from decimal import Decimal, InvalidOperation

    from . import billing
    from .dialer import twilio_client

    call_run_id = int(payload["call_run_id"])
    settings = get_settings()
    sid = await run_db(lambda s: getattr(s.get(CallRun, call_run_id), "twilio_call_sid", None))
    if not sid:
        return
    found = await asyncio.wait_for(asyncio.to_thread((provider or twilio_client).fetch_call, sid, settings), timeout=settings.provider_timeout_seconds + 5)
    if found is None:
        return  # the provider has no such call: nothing to correct
    if found.get("price") in (None, ""):
        raise RuntimeError("the provider has not priced this call yet")
    if str(found.get("price_unit") or "USD").upper() != "USD":
        log.error("call %s: the provider priced it in %s; the ledger is in USD and was left as estimated", call_run_id, found.get("price_unit"))
        return
    try:
        price = Decimal(str(found["price"]))
    except InvalidOperation:
        raise RuntimeError(f"the provider returned a price that is not a number: {found['price']!r}") from None

    def _apply(s):
        run = s.get(CallRun, call_run_id)
        if run is not None:
            billing.record_provider_price(s, run, price)

    await run_db(_apply)

