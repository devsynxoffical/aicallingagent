"""FastAPI app: Twilio webhooks, the media-stream WebSocket, and a small control API."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import hmac
import csv
import io
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import secrets as _secrets

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from pipecat.runner.utils import parse_telephony_websocket
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from . import background, billing, events, jobs, llm, observability
from .apideps import get_campaign as _get_campaign
from .accounts.api import public as accounts_public_router
from .accounts.api import router as accounts_router
from .apideps import acting_as, current_principal, organization_id, require_api_token
from .intake import intake_router, requests_router
from .config import Settings, get_settings, resolve_api_token
from .llm import AIError, AITransientError, attributed
from .db import ACTIVE_CALL_STATUSES, Appointment, AuditEntry, get_engine, row_level_security, CallRun, Campaign, Lead, campaign_stats, get_campaign_by_name, is_dnc, run_db, utcnow, run_blocking
from .dialer import twilio_client
from .dialer import rules as campaign_rules
from .dialer.reconcile import reconcile_forever
from .dialer.campaign_runner import TERMINAL_CALL_STATUSES, CampaignRunner, LeadNotDialable, apply_twilio_status
from .leads.importer import import_leads
from .leads.importer import normalize_phone
from .languages import LANGUAGE_NAMES, base_language
from .playbook.brief import CampaignBrief, playbook_from_brief
from .playbook.schema import Playbook
from .providers import SpeechPlan, get_stt, get_tts, speech_plan_problems
from .intelligence.api import router as intelligence_router
from .training import versions
from .training.api import router as training_router
from .training.service import resume_unfinished
from .voice.pipeline import run_call

log = logging.getLogger("callagent.server")

runner: CampaignRunner | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner
    settings = get_settings()
    observability.configure_logging(settings.log_level, settings.log_format, settings.voice_log_level)
    runner = CampaignRunner(settings)
    missing = settings.missing_for_calls()
    if missing:
        log.warning("Not configured for live calls yet, missing: %s", ", ".join(missing))
    # Resume campaigns that were running when the process last stopped.
    ids = await run_db(lambda s: [c.id for c in s.execute(select(Campaign).where(Campaign.status == "running")).scalars()])
    for cid in ids:
        await runner.start(cid)
    # Training documents that were waiting or half-read when the process stopped.
    await resume_unfinished()
    # Background jobs that were queued, or left running by a process that stopped.
    await background.recover_training_runs()
    background.start_worker()
    # Calls that went quiet are resolved by asking the provider, for every campaign.
    reconciler = asyncio.create_task(reconcile_forever(), name="call-reconciler")
    yield
    reconciler.cancel()
    try:
        await reconciler
    except (asyncio.CancelledError, Exception):
        pass
    await background.stop_worker()
    await runner.shutdown()


app = FastAPI(title="callagent", lifespan=lifespan)
app.add_middleware(observability.RequestContext)  # request id, timing, audit trail


@app.exception_handler(AIError)
async def ai_request_failed(_request: Request, error: AIError) -> JSONResponse:
    """A model request made on behalf of a console action failed. Say which kind: a busy
    provider is worth another click in a minute, a rejected request is not."""
    if isinstance(error, AITransientError):
        return JSONResponse({"detail": "The AI provider is busy or unreachable right now. Nothing was changed; try again in a minute."}, status_code=503, headers={"Retry-After": "60"})
    log.error("an AI request was rejected: %s", error)
    return JSONResponse({"detail": "The AI provider did not accept this request. Nothing was changed."}, status_code=502)
app.include_router(training_router)
app.include_router(intelligence_router)
app.include_router(intake_router)
app.include_router(requests_router)
app.include_router(accounts_public_router)
app.include_router(accounts_router)


def _runner() -> CampaignRunner:
    assert runner is not None
    return runner


# ----------------------------------------------------------------------------- Twilio


async def _twilio_form(request: Request, settings: Settings) -> dict[str, str]:
    form = {k: str(v) for k, v in (await request.form()).items()}
    url = str(request.url)
    if settings.public_base_url:
        # Twilio signs the public URL, which may differ from what uvicorn sees behind a tunnel.
        url = settings.public_base_url + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not twilio_client.validate_signature(settings, url, form, request.headers.get("X-Twilio-Signature")):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return form


@app.post("/twilio/voice")
async def twilio_voice(request: Request, call_run_id: int = Query(...), settings: Settings = Depends(get_settings)):
    form = await _twilio_form(request, settings)
    answered_by = (form.get("AnsweredBy") or "unknown").lower()
    log.info("call_run %s answered_by=%s", call_run_id, answered_by)

    def _record(s):
        run = s.get(CallRun, call_run_id)
        if run:
            run.answered_by = answered_by
            run.twilio_call_sid = run.twilio_call_sid or form.get("CallSid")
        return run is not None

    if not await run_db(_record):
        return Response(content=twilio_client.hangup_twiml(), media_type="application/xml")

    if answered_by == "fax":
        return Response(content=twilio_client.hangup_twiml(), media_type="application/xml")
    if answered_by in ("machine_end_beep", "machine_end_silence", "machine_end_other"):
        mode = "voicemail"
    elif answered_by == "machine_start":
        # DetectMessageEnd should not produce this, but if it does we have no beep: hang up and retry later.
        return Response(content=twilio_client.hangup_twiml(), media_type="application/xml")
    else:
        mode = "live"

    def _mode(s):
        run = s.get(CallRun, call_run_id)
        if run.stream_token is None:
            run.stream_token = _secrets.token_urlsafe(24)
        run.mode = mode
        return run.stream_token

    token = await run_db(_mode)
    return Response(content=twilio_client.stream_twiml(call_run_id, mode, settings, token), media_type="application/xml")


@app.post("/twilio/status")
async def twilio_status(request: Request, call_run_id: int | None = Query(None), settings: Settings = Depends(get_settings)):
    form = await _twilio_form(request, settings)

    def _apply(s):
        run_id = call_run_id
        if run_id is None:
            # Inbound calls: the number's status callback carries no call_run_id, so find the call by its SID.
            sid = form.get("CallSid")
            run = s.execute(select(CallRun).where(CallRun.twilio_call_sid == sid)).scalars().first() if sid else None
            if run is None:
                return
            run_id = run.id
        apply_twilio_status(s, run_id, form, settings)

    await run_db(_apply)
    return PlainTextResponse("ok")


NO_AGENT_MESSAGE = "Sorry, nobody is available to take your call on this number right now. Please try again later. Goodbye."


def find_inbound_campaign(s, to_number: str | None, settings: Settings) -> Campaign | None:
    """The campaign that answers calls to this number: the one that owns the number,
    otherwise the INBOUND_CAMPAIGN fallback."""
    if to_number:
        owned = s.execute(select(Campaign).where(Campaign.inbound_number == to_number)).scalars().first()
        if owned is not None:
            return owned
    fallback = (settings.inbound_campaign or "").strip()
    if not fallback:
        return None
    return s.get(Campaign, int(fallback)) if fallback.isdigit() else get_campaign_by_name(s, fallback)


def open_inbound_call(s, form: dict[str, str], settings: Settings) -> tuple[int, str] | None:
    """Create the call record for an incoming call. Returns (call_run_id, stream_token),
    or None when no campaign answers the dialed number."""
    campaign = find_inbound_campaign(s, form.get("To"), settings)
    if campaign is None or not campaign.playbook_json:
        return None
    call_sid = form.get("CallSid") or ""
    existing = s.execute(select(CallRun).where(CallRun.twilio_call_sid == call_sid)).scalars().first() if call_sid else None
    if existing is not None:
        # Twilio retried the webhook: answer with the same stream instead of opening a second call.
        return existing.id, existing.stream_token or ""
    caller = normalize_phone(form.get("From") or "", settings.default_phone_region)
    # Withheld caller ID: a per-call placeholder keeps anonymous callers from sharing one lead.
    phone = caller or f"anon:{call_sid[-24:] or _secrets.token_hex(8)}"
    lead = s.execute(select(Lead).where(Lead.campaign_id == campaign.id, Lead.phone_e164 == phone)).scalars().first()
    if lead is None:
        lead = Lead(campaign_id=campaign.id, phone_e164=phone, raw_phone=form.get("From") or "", status="completed", extra={"source": "inbound call"})
        s.add(lead)
        s.flush()
    status_before = lead.status
    if lead.status != "dnc":
        lead.status = "calling"  # keeps the dialer from ringing them while they are on the line with us
    token = _secrets.token_urlsafe(24)
    run = CallRun(
        lead_id=lead.id,
        campaign_id=campaign.id,
        twilio_call_sid=call_sid or None,
        status="in-progress",
        mode="inbound",
        direction="inbound",
        playbook_version=versions.active_number(s, campaign),
        answered_by="human",
        stream_token=token,
        started_at=utcnow(),
        summary={"lead_status_before": status_before},
    )
    s.add(run)
    s.flush()
    return run.id, token


@app.post("/twilio/inbound")
async def twilio_inbound(request: Request, settings: Settings = Depends(get_settings)):
    """Point a Twilio number's "A call comes in" webhook here to have the agent answer it."""
    form = await _twilio_form(request, settings)
    if not settings.public_base_url:
        # Without it the call's audio cannot be streamed here. Say so to the caller and
        # open no call, instead of failing after a record was created.
        log.error("inbound call to %s cannot be answered: PUBLIC_BASE_URL is not set", form.get("To"))
        return Response(content=twilio_client.say_and_hangup_twiml(NO_AGENT_MESSAGE), media_type="application/xml")
    try:
        opened = await run_db(lambda s: open_inbound_call(s, form, settings))
    except IntegrityError:
        # The same webhook arrived twice at once and the other delivery opened the call
        # (one record per provider call is enforced by the database): answer with that one.
        opened = await run_db(lambda s: open_inbound_call(s, form, settings))
    if opened is None:
        log.warning("inbound call to %s: no campaign answers this number", form.get("To"))
        return Response(content=twilio_client.say_and_hangup_twiml(NO_AGENT_MESSAGE), media_type="application/xml")
    call_run_id, token = opened
    log.info("inbound call_run %s from %s", call_run_id, form.get("From"))
    return Response(content=twilio_client.stream_twiml(call_run_id, "inbound", settings, token), media_type="application/xml")


@app.websocket("/ws")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    settings = get_settings()
    try:
        # A connection that says nothing is dropped: it has not authenticated and must not hold a socket open.
        transport_type, call_data = await asyncio.wait_for(parse_telephony_websocket(websocket), timeout=WS_HANDSHAKE_SECONDS)
    except asyncio.TimeoutError:
        log.warning("websocket closed: no handshake within %s seconds", WS_HANDSHAKE_SECONDS)
        await websocket.close(code=4008)
        return
    except ValueError as e:
        log.warning("websocket handshake failed: %s", e)
        await websocket.close()
        return
    if transport_type != "twilio":
        log.warning("unexpected telephony provider %s", transport_type)
        await websocket.close()
        return
    body = dict(call_data.get("body", {}) or {})
    reason = await run_db(lambda s: authorize_stream(s, body, call_data.get("call_id")))
    if reason:
        log.warning("rejected media stream: %s", reason)
        await websocket.close(code=4003)
        return
    try:
        await run_call(websocket, call_data["stream_id"], call_data["call_id"], body, settings)
    except Exception:
        log.exception("call session crashed")


WS_HANDSHAKE_SECONDS = 10


def authorize_stream(s, body: dict, call_sid: str | None) -> str | None:
    """The WebSocket is public. Only Twilio, carrying the per-call token from our own TwiML
    for a call that is live and not yet streamed, gets a pipeline. Returns a rejection reason."""
    try:
        call_run_id = int(body.get("call_run_id", ""))
    except (TypeError, ValueError):
        return "missing call_run_id"
    run = s.get(CallRun, call_run_id)
    if run is None:
        return "unknown call_run_id"
    token = str(body.get("stream_token", ""))
    if not run.stream_token or not token or not _secrets.compare_digest(token, run.stream_token):
        return "bad stream token"
    if run.status in TERMINAL_CALL_STATUSES:
        return "call already ended"
    if run.twilio_call_sid and call_sid and call_sid != run.twilio_call_sid:
        return "call sid mismatch"
    # Claimed in one statement: of two sockets presenting the same valid token at the
    # same moment, exactly one gets the call.
    claimed = s.execute(update(CallRun).where(CallRun.id == call_run_id, CallRun.stream_connected.is_not(True)).values(stream_connected=True)).rowcount
    if claimed != 1:
        return "stream already connected for this call"
    return None


# ----------------------------------------------------------------------------- control API


class CampaignIn(BaseModel):
    name: str
    playbook: Playbook
    script_text: str = ""


class CampaignOut(BaseModel):
    id: int
    name: str
    status: str
    languages: list[str] = []
    inbound_number: str | None = None
    stop_reason: str | None = None
    spend_usd: float = 0.0
    budget_usd: float | None = None
    readiness_score: int
    stats: dict[str, int]
    running: bool


def _campaign_out(s, c: Campaign) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        status=c.status,
        languages=_languages(c),
        inbound_number=c.inbound_number,
        stop_reason=c.stop_reason,
        spend_usd=campaign_rules.campaign_spend(s, c.id),
        budget_usd=campaign_rules.rules_of(c).budget_usd,
        readiness_score=int((c.playbook_json or {}).get("readiness_score", 0)),
        stats=campaign_stats(s, c.id),
        running=_runner().is_running(c.id),
    )


def _languages(c: Campaign) -> list[str]:
    try:
        return Playbook.model_validate(c.playbook_json).languages
    except Exception:
        return []


def _speech_problems(pb: Playbook, settings: Settings) -> list[str]:
    return speech_plan_problems(settings, SpeechPlan.from_playbook(pb))


@app.get("/health")
async def health():
    """Kept for callers that only ask "is it up". What is not configured is shown to
    signed-in callers at /api/status, not here."""
    return {"ok": True}


@app.get("/health/live")
async def health_live():
    """The process is running and answering. Says nothing about whether it can work."""
    return {"alive": True}


@app.get("/health/ready")
async def health_ready():
    """Whether this process can do its work: the database answers, its schema is the one
    this release needs, and the background worker runs. 503 with the failing check otherwise."""
    try:
        result = await asyncio.wait_for(run_db(lambda s: observability.readiness(s, background.worker_running()), unrestricted=True), timeout=5)
    except Exception as e:
        observability.READY.set(0)
        result = {"ready": False, "checks": {"database": f"unreachable ({type(e).__name__})"}}
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@app.get("/metrics", include_in_schema=False)
async def metrics(authorization: str | None = Header(default=None), settings: Settings = Depends(get_settings)):
    """Figures for a monitoring system, in Prometheus text format. They cover every
    organization of the deployment, so they are for its operator: METRICS_TOKEN, or
    the engine's API token."""
    given = (authorization or "").removeprefix("Bearer ").strip().encode()
    allowed = [token.encode() for token in (settings.metrics_token, resolve_api_token(settings)) if token]
    if not given or not any(hmac.compare_digest(given, token) for token in allowed):
        raise HTTPException(401, "a monitoring token is required", headers={"WWW-Authenticate": "Bearer"})
    snapshot = await run_db(observability.gather, unrestricted=True)
    return Response(observability.render(snapshot, background.worker_running()), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/audit", dependencies=[Depends(require_api_token)])
async def audit_log(actor: str | None = None, action: str | None = None, limit: int = 100, before_id: int | None = None):
    """Who changed what in the caller's organization, newest first. For its admins."""

    def _q(s):
        query = select(AuditEntry).where(AuditEntry.organization_id == organization_id())
        if actor:
            query = query.where(AuditEntry.actor == actor.strip().lower())
        if action:
            query = query.where(AuditEntry.action == action)
        if before_id is not None:
            query = query.where(AuditEntry.id < before_id)
        return [
            {"id": e.id, "at": e.created_at.isoformat(), "actor": e.actor, "role": e.actor_role, "action": e.action, "method": e.method, "route": e.route,
             "target": e.target or {}, "status": e.status, "request_id": e.request_id, "address": e.address}
            for e in s.execute(query.order_by(AuditEntry.id.desc()).limit(max(1, min(limit, 500)))).scalars()
        ]

    return await run_db(_q)


@app.get("/api/jobs", dependencies=[Depends(require_api_token)])
async def list_jobs(request: Request, status: str | None = None, limit: int = 50):
    """Background work of the caller's organization: counts by state, and the most recent
    jobs (``status=dead`` for failures that gave up). The deployment's operator also sees
    housekeeping jobs that belong to no organization."""
    org, operator = organization_id(), current_principal(request).platform

    def _q(s):
        mine = or_(jobs.Job.organization_id == org, jobs.Job.organization_id.is_(None)) if operator else jobs.Job.organization_id == org
        query = select(jobs.Job).where(mine).order_by(jobs.Job.id.desc()).limit(max(1, min(limit, 200)))
        if status:
            query = query.where(jobs.Job.status == status)
        out = {"stats": jobs.stats(s, org, include_unowned=operator), "jobs": [jobs.describe(j) for j in s.execute(query).scalars()]}
        if operator:
            out["queues"] = jobs.queue_stats(s)  # the deployment's load, for its operator only
        return out

    return await run_db(_q)


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_api_token)])
async def retry_job(job_id: int, request: Request):
    """Run a dead job again, after its cause was fixed."""
    org, operator = organization_id(), current_principal(request).platform

    def _retry(s):
        job = s.get(jobs.Job, job_id)
        if job is None or not (job.organization_id == org or (operator and job.organization_id is None)):
            raise HTTPException(404, "job not found")  # someone else's job does not exist as far as the caller can tell
        return jobs.retry_dead(s, job_id)

    if not await run_db(_retry):
        raise HTTPException(409, "only a dead job can be retried")
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(require_api_token)])
async def list_events(type: str | None = None, call_run_id: int | None = None, campaign: str | None = None, limit: int = 100, before_id: int | None = None):
    """What happened, newest first: the caller's organization's durable event log.
    ``call_run_id`` gives everything recorded about one call."""

    def _q(s):
        query = select(events.Event).where(events.Event.organization_id == organization_id())
        if type:
            query = query.where(events.Event.type == type)
        if call_run_id is not None:
            query = query.where(events.Event.call_run_id == call_run_id)
        if campaign:
            query = query.where(events.Event.campaign_id == _get_campaign(s, campaign).id)
        if before_id is not None:
            query = query.where(events.Event.id < before_id)
        return [events.describe(e) for e in s.execute(query.order_by(events.Event.id.desc()).limit(max(1, min(limit, 500)))).scalars()]

    return await run_db(_q)


@app.get("/api/ai-usage", dependencies=[Depends(require_api_token)])
async def ai_usage(days: int = 30, campaign: str | None = None):
    """What the caller's organization used of the AI model over the last ``days`` days,
    by workflow and model: requests, tokens and cost at the price table in force."""
    from datetime import timedelta

    since = utcnow() - timedelta(days=max(1, min(days, 366)))

    def _q(s):
        return llm.usage_summary(s, organization_id(), since, _get_campaign(s, campaign).id if campaign else None)

    return await run_db(_q)


@app.get("/api/usage", dependencies=[Depends(require_api_token)])
async def usage_ledger(days: int = 30, campaign: str | None = None, limit: int = 100, before_id: int | None = None):
    """The caller's organization's usage ledger over the last ``days`` days: totals by
    kind and by how far each amount can be trusted, and the newest lines."""
    from datetime import timedelta

    since = utcnow() - timedelta(days=max(1, min(days, 366)))

    def _q(s):
        return billing.summary(s, organization_id(), since, _get_campaign(s, campaign).id if campaign else None, limit=limit, before_id=before_id)

    return await run_db(_q)


class AdjustmentIn(BaseModel):
    amount_usd: Decimal = Field(description="The difference to apply; negative lowers the total.")
    note: str = Field(min_length=3, max_length=300, description="Why. Shown with the line for good.")


@app.post("/api/usage/{line_id}/adjustments", status_code=201, dependencies=[Depends(require_api_token)])
async def adjust_usage(line_id: int, body: AdjustmentIn, request: Request):
    """Correct a ledger line by adding one. The line itself is never changed. For the
    deployment's operator, acting in the organization the line belongs to."""
    who = acting_as(request, "operator")

    def _adjust(s):
        try:
            return billing.describe(billing.adjust(s, line_id, body.amount_usd, note=body.note, by=who, organization_id=organization_id()))
        except billing.LedgerError as e:
            raise HTTPException(404 if "no such" in str(e) else 400, str(e)) from None

    return await run_db(_adjust)


@app.get("/api/timeline", dependencies=[Depends(require_api_token)])
async def timeline(days: int = 14, settings: Settings = Depends(get_settings)):
    """Calls per day over the last ``days`` days, across campaigns, counted from the call
    records: placed, answered and converted. Days are calendar days in DEFAULT_TIMEZONE;
    a day with no calls is listed with zeros, so the series has no gaps."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from .intelligence.report import CONVERSIONS

    span = max(1, min(days, 90))
    try:
        tz = ZoneInfo(settings.default_timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    today = utcnow().astimezone(tz).date()
    first = today - timedelta(days=span - 1)

    def _q(s):
        buckets = {first + timedelta(days=i): {"calls": 0, "answered": 0, "conversions": 0} for i in range(span)}
        since = utcnow() - timedelta(days=span + 1)
        rows = s.execute(
            select(CallRun.started_at, CallRun.stream_connected, CallRun.transcript, CallRun.disposition).where(CallRun.organization_id == organization_id(), CallRun.started_at >= since)
        )
        for started_at, connected, transcript, disposition in rows:
            bucket = buckets.get(started_at.astimezone(tz).date())
            if bucket is None:
                continue
            bucket["calls"] += 1
            bucket["answered"] += bool(connected and transcript)
            bucket["conversions"] += disposition in CONVERSIONS
        return {"timezone": str(tz), "days": [{"date": day.isoformat(), **counts} for day, counts in buckets.items()]}

    return await run_db(_q)


@app.get("/api/campaigns", response_model=list[CampaignOut], dependencies=[Depends(require_api_token)])
async def list_campaigns():
    return await run_db(lambda s: [_campaign_out(s, c) for c in s.execute(select(Campaign).where(Campaign.organization_id == organization_id()).order_by(Campaign.id)).scalars()])


@app.post("/api/campaigns", response_model=CampaignOut, dependencies=[Depends(require_api_token)])
async def create_campaign(body: CampaignIn):
    def _create(s):
        org = organization_id()
        c = get_campaign_by_name(s, body.name, org)
        if c is None:
            c = Campaign(organization_id=org, name=body.name, status="draft")
            s.add(c)
        c.script_text = body.script_text or c.script_text or ""
        s.flush()
        # A playbook sent to the API is the owner's own: it becomes the active version at once.
        versions.record_active(s, c, body.playbook.model_dump(), source="api")
        s.flush()
        return _campaign_out(s, c)

    return await run_db(_create)


@app.get("/api/status", dependencies=[Depends(require_api_token)])
async def setup_status(settings: Settings = Depends(get_settings)):
    """What is connected and what is still missing before real calls can be placed or
    answered. Names of settings only: no value of any key is ever returned."""
    stt, tts = get_stt(settings), get_tts(settings)

    def check(key: str, label: str, purpose: str, required: dict) -> dict:
        missing = [name for name, value in required.items() if not value]
        return {"key": key, "label": label, "purpose": purpose, "ok": not missing, "missing": missing}

    checks = [
        check("model", "AI model (Anthropic)", "Holds the conversation, reviews calls, coaches and builds playbooks.", {"ANTHROPIC_API_KEY": settings.anthropic_api_key}),
        check("telephony", "Phone provider (Twilio)", "Places and receives the phone calls.", {"TWILIO_ACCOUNT_SID": settings.twilio_account_sid, "TWILIO_AUTH_TOKEN": settings.twilio_auth_token, "TWILIO_FROM_NUMBER": settings.twilio_from_number}),
        check("stt", f"Speech recognition ({stt.name.title()})", "Turns what the prospect says into text.", stt.required_settings(settings)),
        check("tts", f"Voice ({tts.name.title()})", "Speaks the agent's replies.", tts.required_settings(settings)),
        check("public_url", "Public address of this engine", "Where the phone provider reaches the engine during a call.", {"PUBLIC_BASE_URL": settings.public_base_url}),
    ]
    base = settings.public_base_url or None
    return {
        "ready_for_calls": all(c["ok"] for c in checks),
        "checks": checks,
        "from_number": settings.twilio_from_number or None,
        # Paste into the phone number's "A call comes in" setting at the phone provider to have the agent answer it.
        "inbound_webhook": f"{base}/twilio/inbound" if base else None,
        "languages": [{"code": code, "name": label} for code, label in LANGUAGE_NAMES.items()],
        "website_intake": bool(settings.intake_token),
        # Whether the database itself keeps organizations apart: enforced | bypassed | missing | unavailable.
        "row_level_security": await run_blocking(row_level_security, get_engine()),
        "default_timezone": settings.default_timezone,
    }


@app.post("/api/campaign-briefs", response_model=CampaignOut, status_code=201, dependencies=[Depends(require_api_token)])
async def create_campaign_from_brief(body: CampaignBrief, request: Request):
    """Create a campaign from the short form: a working playbook without the AI model.
    Refused if a campaign with this name exists (use the playbook screens to change one)."""
    playbook = playbook_from_brief(body)

    def _create(s):
        org = organization_id()
        if get_campaign_by_name(s, body.name, org) is not None:
            raise HTTPException(409, f"a campaign named '{body.name}' already exists")
        c = Campaign(organization_id=org, name=body.name, status="draft", script_text=body.script_text.strip())
        s.add(c)
        s.flush()
        versions.record_active(s, c, playbook.model_dump(), source="brief", by=acting_as(request, "api token"))
        s.flush()
        return _campaign_out(s, c)

    return await run_db(_create)


def _language_state(c: Campaign, settings: Settings) -> dict:
    pb = Playbook.model_validate(c.playbook_json)
    return {
        "languages": pb.languages,
        # Languages with phrasing written for them. The others are spoken from the main language's playbook.
        "localized": [code for code in pb.languages[1:] if pb.localized_for(code) is not None],
        "supported": [{"code": code, "name": label} for code, label in LANGUAGE_NAMES.items()],
        "provider_gaps": _speech_problems(pb, settings),
        "can_localize": bool(settings.anthropic_api_key),
    }


@app.get("/api/campaigns/{name}/languages", dependencies=[Depends(require_api_token)])
async def get_languages(name: str, settings: Settings = Depends(get_settings)):
    return await run_db(lambda s: _language_state(_get_campaign(s, name), settings))


class LanguagesIn(BaseModel):
    languages: list[str] = Field(min_length=1, max_length=5, description="Opening language first.")
    localize: bool = Field(default=False, description="Have the AI model write native phrasing for each added language.")


@app.put("/api/campaigns/{name}/languages", dependencies=[Depends(require_api_token)])
async def set_languages(name: str, body: LanguagesIn, request: Request, settings: Settings = Depends(get_settings)):
    """Set the languages a campaign speaks, opening language first. With more than one,
    the agent follows the language the prospect speaks."""
    codes = list(dict.fromkeys(base_language(code) for code in body.languages if base_language(code)))
    unknown = [code for code in codes if code not in LANGUAGE_NAMES]
    if unknown or not codes:
        raise HTTPException(400, f"unsupported language(s): {', '.join(unknown) or 'none given'}. Supported: {', '.join(LANGUAGE_NAMES)}")
    if body.localize and not settings.anthropic_api_key:
        raise HTTPException(400, "cannot write native phrasing: ANTHROPIC_API_KEY is not set")

    current = await run_db(lambda s: Playbook.model_validate(_get_campaign(s, name).playbook_json))
    pb = current.model_copy(deep=True)
    if base_language(pb.persona.language) != codes[0]:
        pb.persona.language = codes[0]
    pb.additional_languages = codes[1:]
    pb.localized = [phrasing for phrasing in pb.localized if base_language(phrasing.language) in codes[1:]]
    if body.localize:
        from .playbook.analyzer import PlaybookAnalyzer

        analyzer = PlaybookAnalyzer()
        campaign_id = await run_db(lambda s: _get_campaign(s, name).id)
        with attributed(campaign_id=campaign_id):
            for code in codes[1:]:
                if pb.localized_for(code) is None:
                    pb.localized.append(await run_blocking(analyzer.localize, pb, code))
    who = acting_as(request, "api token")

    def _save(s):
        c = _get_campaign(s, name)
        # The admin is changing the playbook directly: recorded as a new version and made active.
        versions.record_active(s, c, pb.model_dump(), source="languages", by=who)
        s.flush()
        return _language_state(c, settings)

    return await run_db(_save)


@app.get("/api/campaigns/{name}", response_model=CampaignOut, dependencies=[Depends(require_api_token)])
async def get_campaign(name: str):
    return await run_db(lambda s: _campaign_out(s, _get_campaign(s, name)))


@app.post("/api/campaigns/{name}/leads", dependencies=[Depends(require_api_token)])
async def upload_leads(name: str, file: UploadFile = File(...), settings: Settings = Depends(get_settings)):
    suffix = Path(file.filename or "leads.csv").suffix or ".csv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        path = tmp.name
    try:
        report = await run_db(lambda s: import_leads(s, _get_campaign(s, name), path, settings.default_phone_region), slow=True)  # parsing a sheet can take a while
    finally:
        Path(path).unlink(missing_ok=True)
    return report.as_dict()


@app.post("/api/campaigns/{name}/start", response_model=CampaignOut, dependencies=[Depends(require_api_token)])
async def start_campaign(name: str, concurrency: int | None = None, force: bool = False, settings: Settings = Depends(get_settings)):
    await run_db(lambda s: _get_campaign(s, name).id)  # whose it is comes first: 404 before anything about configuration
    missing = settings.missing_for_calls()
    if missing:
        raise HTTPException(400, f"cannot place calls, missing settings: {', '.join(missing)}")

    def _prep(s):
        c = _get_campaign(s, name)
        pb = Playbook.model_validate(c.playbook_json)
        if not pb.is_ready and not force:
            qs = "; ".join(q.question for q in pb.blocking_questions)
            raise HTTPException(400, f"playbook has blocking open questions: {qs}. Answer them (callagent onboard) or pass force=true.")
        problems = _speech_problems(pb, settings)
        if problems:
            # Not overridable: dialing people in a language the agent cannot hear is never the right call.
            raise HTTPException(400, "cannot run this campaign's languages with the configured providers: " + "; ".join(problems))
        rule_problems = campaign_rules.start_problems(c, settings)
        if rule_problems:
            raise HTTPException(400, "; ".join(rule_problems))
        if concurrency:
            c.max_concurrent_calls = concurrency
        return c.id

    cid = await run_db(_prep)
    await _runner().start(cid)
    return await run_db(lambda s: _campaign_out(s, _get_campaign(s, name)))


@app.post("/api/campaigns/{name}/pause", response_model=CampaignOut, dependencies=[Depends(require_api_token)])
async def pause_campaign(name: str, request: Request):
    def _pause(s):
        c = _get_campaign(s, name)
        events.emit(s, "CampaignPaused", key=f"campaign:{c.id}:paused:{utcnow().isoformat()}", subject="campaign", subject_id=c.id,
                    organization_id=c.organization_id, campaign_id=c.id, payload={"by": acting_as(request, "api token")})
        return c.id

    cid = await run_db(_pause)
    await _runner().stop(cid, status="paused")
    return await run_db(lambda s: _campaign_out(s, _get_campaign(s, name)))


@app.get("/api/campaigns/{name}/rules", dependencies=[Depends(require_api_token)])
async def get_rules(name: str, settings: Settings = Depends(get_settings)):
    """The calling rules in force for this campaign, with spend so far."""

    def _get(s):
        c = _get_campaign(s, name)
        return {
            "rules": campaign_rules.describe(c, settings),
            "stored": dict(c.rules_json or {}),
            "spend_usd": campaign_rules.campaign_spend(s, c.id),
            "stop_reason": c.stop_reason,
        }

    return await run_db(_get)


@app.put("/api/campaigns/{name}/rules", dependencies=[Depends(require_api_token)])
async def set_rules(name: str, body: campaign_rules.CampaignRules, settings: Settings = Depends(get_settings)):
    """Replace this campaign's calling rules. Fields left out go back to the global default.
    Takes effect on the next dial; a running campaign does not need a restart."""

    def _set(s):
        c = _get_campaign(s, name)
        c.rules_json = body.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        s.flush()
        return {
            "rules": campaign_rules.describe(c, settings),
            "stored": dict(c.rules_json or {}),
            "spend_usd": campaign_rules.campaign_spend(s, c.id),
            "stop_reason": c.stop_reason,
        }

    return await run_db(_set)


@app.post("/api/campaigns/{name}/inbound", response_model=CampaignOut, dependencies=[Depends(require_api_token)])
async def set_inbound_number(name: str, number: str | None = None, settings: Settings = Depends(get_settings)):
    """Make this campaign answer calls to ``number``. Omit ``number`` to stop answering."""
    e164: str | None = None
    if number:
        e164 = normalize_phone(number, settings.default_phone_region)
        if not e164:
            raise HTTPException(400, f"invalid phone number: {number}")

    def _set(s):
        c = _get_campaign(s, name)
        if e164:
            taken = s.execute(select(Campaign).where(Campaign.inbound_number == e164, Campaign.id != c.id)).scalars().first()
            if taken is not None:
                # Another organization's campaign is never named.
                owner = f"campaign '{taken.name}'" if taken.organization_id == c.organization_id else "another campaign"
                raise HTTPException(409, f"{e164} is already answered by {owner}")
            problems = _speech_problems(Playbook.model_validate(c.playbook_json), settings)
            if problems:
                raise HTTPException(400, "cannot run this campaign's languages with the configured providers: " + "; ".join(problems))
        c.inbound_number = e164
        try:
            s.flush()
        except IntegrityError:
            # Taken by a campaign this transaction cannot see (another organization's).
            raise HTTPException(409, f"{e164} is already answered by another campaign") from None
        return _campaign_out(s, c)

    return await run_db(_set)


@app.post("/api/campaigns/{name}/dial", dependencies=[Depends(require_api_token)])
async def dial_one(name: str, phone: str, first_name: str = "", settings: Settings = Depends(get_settings)):
    """Place a single test call to one number (added to the campaign as a lead)."""
    await run_db(lambda s: _get_campaign(s, name).id)  # whose it is comes first
    missing = settings.missing_for_calls()
    if missing:
        raise HTTPException(400, f"cannot place calls, missing settings: {', '.join(missing)}")
    e164 = normalize_phone(phone, settings.default_phone_region)
    if not e164:
        raise HTTPException(400, f"invalid phone number: {phone}")

    def _lead(s):
        c = _get_campaign(s, name)
        problems = _speech_problems(Playbook.model_validate(c.playbook_json), settings)
        if problems:
            raise HTTPException(400, "cannot run this campaign's languages with the configured providers: " + "; ".join(problems))
        if is_dnc(s, e164, c.organization_id):
            raise HTTPException(400, f"{e164} is on the do-not-call list")
        lead = s.execute(select(Lead).where(Lead.campaign_id == c.id, Lead.phone_e164 == e164)).scalar_one_or_none()
        if lead is None:
            lead = Lead(campaign_id=c.id, phone_e164=e164, raw_phone=phone, first_name=first_name)
            s.add(lead)
            s.flush()
        return c.id, lead.id

    cid, lid = await run_db(_lead)
    try:
        run_id = await _runner().dial_lead(cid, lid, manual=True)
    except LeadNotDialable as e:
        raise HTTPException(409, str(e)) from None
    return {"call_run_id": run_id}


@app.get("/api/campaigns/{name}/calls", dependencies=[Depends(require_api_token)])
async def list_calls(name: str, limit: int = 50, direction: str | None = None):
    def _q(s):
        c = _get_campaign(s, name)
        query = select(CallRun).where(CallRun.campaign_id == c.id)
        if direction in ("outbound", "inbound"):
            query = query.where(CallRun.direction == direction)
        rows = s.execute(query.order_by(CallRun.id.desc()).limit(max(1, min(limit, 500)))).scalars()
        out = []
        for r in rows:
            lead = s.get(Lead, r.lead_id)
            out.append(
                {
                    "call_run_id": r.id,
                    "lead": lead.full_name if lead else "",
                    "phone": lead.phone_e164 if lead else "",
                    "status": r.status,
                    "mode": r.mode,
                    "direction": r.direction,
                    "language": r.language,
                    "answered_by": r.answered_by,
                    "disposition": r.disposition,
                    "duration_seconds": r.duration_seconds,
                    "cost_usd": r.cost_usd,
                    "playbook_version": r.playbook_version,
                    "summary": (r.summary or {}).get("review", {}).get("summary") or (r.summary or {}).get("agent_outcome", {}).get("summary"),
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                }
            )
        return out

    return await run_db(_q)


@app.get("/api/calls/{call_run_id}", dependencies=[Depends(require_api_token)])
async def get_call(call_run_id: int):
    def _q(s):
        r = s.get(CallRun, call_run_id)
        if r is None or r.organization_id != organization_id():
            raise HTTPException(404, "call not found")
        return {
            "call_run_id": r.id,
            "status": r.status,
            # True while the call is ringing or in progress: the transcript below is what
            # has been said so far, and grows.
            "live": r.status in ACTIVE_CALL_STATUSES and r.ended_at is None,
            "mode": r.mode,
            "direction": r.direction,
            "language": r.language,
            "answered_by": r.answered_by,
            "disposition": r.disposition,
            "duration_seconds": r.duration_seconds,
            "transcript": r.transcript,
            "tool_events": r.tool_events,
            "summary": r.summary,
            "recording_url": r.recording_url,
            "error": r.error,
        }

    return await run_db(_q)


def spreadsheet_safe(value):
    """A cell that starts with =, +, - or @ is run as a formula by spreadsheet programs.
    Names, notes and summaries come from lead sheets and from what people said on calls,
    so such cells get a leading apostrophe and are shown as text. Phone numbers keep
    their plus sign: a plus followed only by digits is not a formula."""
    if not isinstance(value, str) or not value or value[0] not in "=+-@\t\r":
        return value
    if value[0] == "+" and value[1:].isdigit():
        return value
    return "'" + value


@app.get("/api/campaigns/{name}/export.csv", dependencies=[Depends(require_api_token)])
async def export_csv(name: str):
    def _rows(s):
        c = _get_campaign(s, name)
        leads = s.execute(select(Lead).where(Lead.campaign_id == c.id).order_by(Lead.id)).scalars().all()
        appts = {a.lead_id: a for a in s.execute(select(Appointment)).scalars()}
        out = []
        for l in leads:
            last = s.execute(select(CallRun).where(CallRun.lead_id == l.id).order_by(CallRun.id.desc()).limit(1)).scalar_one_or_none()
            review = ((last.summary or {}).get("review") if last else None) or {}
            appt = appts.get(l.id)
            out.append(
                {
                    "phone": l.phone_e164,
                    "first_name": l.first_name,
                    "last_name": l.last_name,
                    "company": l.company,
                    "email": l.email,
                    "status": l.status,
                    "attempts": l.attempts,
                    "disposition": l.last_disposition or "",
                    "interest_level": review.get("interest_level", ""),
                    "summary": review.get("summary", ""),
                    "next_action": review.get("next_action", ""),
                    "meeting_at": appt.starts_at if appt else "",
                    "next_attempt_at": l.next_attempt_at.isoformat() if l.next_attempt_at else "",
                    "notes": l.notes,
                }
            )
        return out

    rows = await run_db(_rows)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["phone"])
    writer.writeheader()
    writer.writerows([{key: spreadsheet_safe(value) for key, value in row.items()} for row in rows])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}-results.csv"'})
