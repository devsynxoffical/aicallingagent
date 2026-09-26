"""In-call tools the agent can use (function calling via Pipecat)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndWorkerFrame
from pipecat.services.llm_service import FunctionCallParams, FunctionCallResultProperties

from ..db import Appointment, CallRun, DoNotCall, Lead, run_db
from ..dialer import twilio_client
from .session import CallSession

log = logging.getLogger(__name__)

DISPOSITIONS = [
    "meeting_booked",
    "sale_closed",
    "interested_follow_up",
    "callback_requested",
    "not_interested",
    "not_qualified",
    "wrong_number",
    "gatekeeper",
    "voicemail_left",
    "do_not_call",
    "no_answer",
    "other",
]


def build_tools_schema(playbook_capabilities: set[str], human_transfer_available: bool) -> ToolsSchema:
    tools: list[FunctionSchema] = [
        FunctionSchema(
            name="log_call_outcome",
            description="Record how the call went. Call once near the end of every call, before end_call.",
            properties={
                "disposition": {"type": "string", "enum": DISPOSITIONS},
                "summary": {"type": "string", "description": "Two sentences on what happened."},
                "interest_level": {"type": "integer", "minimum": 0, "maximum": 10},
                "objections": {"type": "array", "items": {"type": "string"}},
                "next_step": {"type": "string", "description": "What should happen next, if anything."},
            },
            required=["disposition", "summary", "interest_level"],
        ),
        FunctionSchema(
            name="book_meeting",
            description="Book the agreed meeting. Only after the prospect confirmed a specific day and time.",
            properties={
                "starts_at": {"type": "string", "description": "ISO 8601 datetime with timezone offset."},
                "timezone": {"type": "string", "description": "IANA timezone of the prospect, e.g. America/Chicago."},
                "notes": {"type": "string"},
            },
            required=["starts_at", "timezone"],
        ),
        FunctionSchema(
            name="schedule_callback",
            description="Schedule a callback at a time the prospect asked for.",
            properties={
                "callback_at": {"type": "string", "description": "ISO 8601 datetime with timezone offset."},
                "reason": {"type": "string"},
            },
            required=["callback_at"],
        ),
        FunctionSchema(
            name="send_followup_sms",
            description="Text the prospect a short message with the info you promised.",
            properties={"message": {"type": "string", "description": "Plain text, under 300 characters."}},
            required=["message"],
        ),
        FunctionSchema(
            name="mark_do_not_call",
            description="The prospect asked not to be contacted again. Adds them to the do-not-call list.",
            properties={"reason": {"type": "string"}},
            required=[],
        ),
        FunctionSchema(
            name="end_call",
            description="Hang up. Say your goodbye in the same message before calling this.",
            properties={"reason": {"type": "string", "description": "Short reason, e.g. 'meeting booked', 'declined'."}},
            required=["reason"],
        ),
    ]
    if human_transfer_available:
        tools.append(
            FunctionSchema(
                name="transfer_to_human",
                description="Connect the prospect to a human colleague right now. Tell them first.",
                properties={"context_for_colleague": {"type": "string"}},
                required=[],
            )
        )
    return ToolsSchema(standard_tools=tools)


def register_tool_handlers(llm, session: CallSession) -> None:
    llm.register_function("log_call_outcome", _make(session, log_call_outcome))
    llm.register_function("book_meeting", _make(session, book_meeting))
    llm.register_function("schedule_callback", _make(session, schedule_callback))
    llm.register_function("send_followup_sms", _make(session, send_followup_sms))
    llm.register_function("mark_do_not_call", _make(session, mark_do_not_call))
    llm.register_function("end_call", _make(session, end_call))
    llm.register_function("transfer_to_human", _make(session, transfer_to_human))


def _make(session: CallSession, fn):
    async def handler(params: FunctionCallParams):
        try:
            result = await fn(session, params)
        except Exception as e:  # never let a tool crash the call
            log.exception("tool %s failed", params.function_name)
            result = {"ok": False, "error": str(e)}
        session.log_tool(params.function_name, params.arguments, result)
        run_llm = result.pop("_run_llm", None)
        await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=run_llm))

    return handler


# ----------------------------------------------------------------------------- handlers


async def log_call_outcome(session: CallSession, params: FunctionCallParams) -> dict[str, Any]:
    args = params.arguments
    session.disposition = args.get("disposition")
    session.outcome = dict(args)

    def _save(s):
        run = s.get(CallRun, session.call_run_id)
        if run:
            run.disposition = session.disposition
            run.summary = {**(run.summary or {}), "agent_outcome": dict(args)}

    await run_db(_save)
    return {"ok": True}


async def book_meeting(session: CallSession, params: FunctionCallParams) -> dict[str, Any]:
    args = params.arguments
    starts_at = str(args.get("starts_at", ""))
    try:
        parsed = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except ValueError:
        return {"ok": False, "error": "starts_at must be ISO 8601, e.g. 2026-10-02T14:30:00-05:00"}
    if parsed.tzinfo is None:
        return {"ok": False, "error": "starts_at needs a timezone offset"}
    if parsed < datetime.now(timezone.utc):
        return {"ok": False, "error": "that time is in the past; confirm the date with the prospect"}

    def _save(s):
        appt = Appointment(
            lead_id=session.lead["id"],
            call_run_id=session.call_run_id,
            starts_at=parsed.isoformat(),
            timezone=str(args.get("timezone", "")),
            notes=str(args.get("notes", "")),
        )
        s.add(appt)
        lead = s.get(Lead, session.lead["id"])
        if lead:
            lead.last_disposition = "meeting_booked"
        s.flush()
        return appt.id

    appt_id = await run_db(_save)
    session.disposition = session.disposition or "meeting_booked"

    webhook = session.settings.booking_webhook_url
    if webhook:
        payload = {
            "appointment_id": appt_id,
            "starts_at": parsed.isoformat(),
            "timezone": args.get("timezone"),
            "notes": args.get("notes", ""),
            "lead": {k: v for k, v in session.lead.items() if k != "extra"},
            "lead_extra": session.lead.get("extra", {}),
            "call_run_id": session.call_run_id,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(webhook, json=payload)
        except Exception as e:
            log.warning("booking webhook failed: %s", e)
    return {"ok": True, "appointment_id": appt_id, "confirmed_time": parsed.isoformat()}


async def schedule_callback(session: CallSession, params: FunctionCallParams) -> dict[str, Any]:
    raw = str(params.arguments.get("callback_at", ""))
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return {"ok": False, "error": "callback_at must be ISO 8601 with timezone"}
    if when.tzinfo is None:
        return {"ok": False, "error": "callback_at needs a timezone offset"}

    def _save(s):
        lead = s.get(Lead, session.lead["id"])
        if lead:
            lead.status = "callback"
            lead.next_attempt_at = when.astimezone(timezone.utc)
            lead.last_disposition = "callback_requested"
            reason = params.arguments.get("reason", "")
            if reason:
                lead.notes = (lead.notes + "\n" if lead.notes else "") + f"Callback requested: {reason}"

    await run_db(_save)
    session.disposition = session.disposition or "callback_requested"
    return {"ok": True, "callback_at": when.isoformat()}


async def send_followup_sms(session: CallSession, params: FunctionCallParams) -> dict[str, Any]:
    body = str(params.arguments.get("message", "")).strip()[:600]
    if not body:
        return {"ok": False, "error": "message is empty"}
    sid = await asyncio.to_thread(twilio_client.send_sms, session.lead["phone_e164"], body, session.settings)
    return {"ok": True, "message_sid": sid}


async def mark_do_not_call(session: CallSession, params: FunctionCallParams) -> dict[str, Any]:
    phone = session.lead["phone_e164"]
    reason = str(params.arguments.get("reason", "requested on call"))

    def _save(s):
        from sqlalchemy import select

        if not s.execute(select(DoNotCall).where(DoNotCall.phone_e164 == phone)).scalar_one_or_none():
            s.add(DoNotCall(phone_e164=phone, reason=reason))
        lead = s.get(Lead, session.lead["id"])
        if lead:
            lead.status = "dnc"
            lead.last_disposition = "do_not_call"

    await run_db(_save)
    session.disposition = "do_not_call"
    return {"ok": True}


async def transfer_to_human(session: CallSession, params: FunctionCallParams) -> dict[str, Any]:
    number = session.settings.human_transfer_number
    if not number:
        return {"ok": False, "error": "no human transfer number configured; offer a callback instead"}
    session.end_reason = "transferred"
    session.ended_by_agent = True
    await asyncio.to_thread(twilio_client.transfer_call, session.twilio_call_sid, number, session.settings)
    return {"ok": True, "_run_llm": False}


async def end_call(session: CallSession, params: FunctionCallParams) -> dict[str, Any]:
    session.ended_by_agent = True
    session.end_reason = str(params.arguments.get("reason", ""))
    # Downstream so the goodbye already queued for TTS is spoken before the pipeline ends.
    # The Twilio serializer hangs up the call when the EndFrame reaches it.
    await params.llm.push_frame(EndWorkerFrame(reason=session.end_reason))
    return {"ok": True, "_run_llm": False}
