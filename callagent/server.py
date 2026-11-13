"""FastAPI app: Twilio webhooks, the media-stream WebSocket, and a small control API."""

from __future__ import annotations

import csv
import io
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import secrets as _secrets

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel
from pipecat.runner.utils import parse_telephony_websocket
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .apideps import get_campaign as _get_campaign
from .apideps import require_api_token
from .config import Settings, get_settings
from .db import Appointment, CallRun, Campaign, Lead, campaign_stats, get_campaign_by_name, is_dnc, run_db, utcnow
from .dialer import twilio_client
from .dialer import rules as campaign_rules
from .dialer.campaign_runner import TERMINAL_CALL_STATUSES, CampaignRunner, LeadNotDialable, apply_twilio_status
from .leads.importer import import_leads
from .leads.importer import normalize_phone
from .playbook.schema import Playbook
from .providers import SpeechPlan, speech_plan_problems
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
    yield
    await runner.shutdown()


app = FastAPI(title="callagent", lifespan=lifespan)
app.include_router(training_router)
app.include_router(intelligence_router)


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
        transport_type, call_data = await parse_telephony_websocket(websocket)
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
    if run.stream_connected:
        return "stream already connected for this call"
    if run.status in TERMINAL_CALL_STATUSES:
        return "call already ended"
    if run.twilio_call_sid and call_sid and call_sid != run.twilio_call_sid:
        return "call sid mismatch"
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
async def health(settings: Settings = Depends(get_settings)):
    return {"ok": True, "missing_for_calls": settings.missing_for_calls()}


@app.get("/api/campaigns", response_model=list[CampaignOut], dependencies=[Depends(require_api_token)])
async def list_campaigns():
    return await run_db(lambda s: [_campaign_out(s, c) for c in s.execute(select(Campaign)).scalars()])


@app.post("/api/campaigns", response_model=CampaignOut, dependencies=[Depends(require_api_token)])
async def create_campaign(body: CampaignIn):
    def _create(s):
        c = get_campaign_by_name(s, body.name)
        if c is None:
            c = Campaign(name=body.name, status="draft")
            s.add(c)
        c.script_text = body.script_text or c.script_text or ""
        s.flush()
        # A playbook sent to the API is the owner's own: it becomes the active version at once.
        versions.record_active(s, c, body.playbook.model_dump(), source="api")
        s.flush()
        return _campaign_out(s, c)

    return await run_db(_create)


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
        report = await run_db(lambda s: import_leads(s, _get_campaign(s, name), path, settings.default_phone_region))
    finally:
        Path(path).unlink(missing_ok=True)
    return report.as_dict()


@app.post("/api/campaigns/{name}/start", response_model=CampaignOut, dependencies=[Depends(require_api_token)])
async def start_campaign(name: str, concurrency: int | None = None, force: bool = False, settings: Settings = Depends(get_settings)):
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
async def pause_campaign(name: str):
    cid = await run_db(lambda s: _get_campaign(s, name).id)
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
                raise HTTPException(409, f"{e164} is already answered by campaign '{taken.name}'")
            problems = _speech_problems(Playbook.model_validate(c.playbook_json), settings)
            if problems:
                raise HTTPException(400, "cannot run this campaign's languages with the configured providers: " + "; ".join(problems))
        c.inbound_number = e164
        s.flush()
        return _campaign_out(s, c)

    return await run_db(_set)


@app.post("/api/campaigns/{name}/dial", dependencies=[Depends(require_api_token)])
async def dial_one(name: str, phone: str, first_name: str = "", settings: Settings = Depends(get_settings)):
    """Place a single test call to one number (added to the campaign as a lead)."""
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
        if is_dnc(s, e164):
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
async def list_calls(name: str, limit: int = 50):
    def _q(s):
        c = _get_campaign(s, name)
        rows = s.execute(select(CallRun).where(CallRun.campaign_id == c.id).order_by(CallRun.id.desc()).limit(limit)).scalars()
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
        if r is None:
            raise HTTPException(404, "call not found")
        return {
            "call_run_id": r.id,
            "status": r.status,
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
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}-results.csv"'})
