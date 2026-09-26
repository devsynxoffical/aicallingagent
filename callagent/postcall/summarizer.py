"""After each call: structured review, lead update, and coaching notes for the playbook."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import CallRun, Lead, run_db
from ..llm import make_client, parse_structured
from ..playbook.schema import Playbook
from ..voice.tools import DISPOSITIONS

Disposition = Literal[
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


class CallReview(BaseModel):
    disposition: Disposition
    summary: str = Field(description="Three sentences max, written for the sales manager.")
    interest_level: int = Field(description="0-10")
    objections_raised: list[str]
    commitments_made_by_agent: list[str] = Field(description="Anything the agent promised (send info, callback, etc.).")
    prospect_facts: list[str] = Field(description="Useful facts learned about the prospect or their business.")
    next_action: str
    retry_recommended: bool = Field(description="True if another attempt is worthwhile (no answer, busy, callback).")
    retry_after_hours: int = Field(description="Hours to wait before retrying, 0 if not retrying.")
    agent_quality_score: int = Field(description="0-10 for how human, relevant and effective the agent was.")
    coaching_notes: list[str] = Field(description="Specific, actionable notes to improve future calls.")
    playbook_suggestions: list[str] = Field(description="Concrete edits to the playbook this call suggests. Empty if none.")


REVIEW_SYSTEM = """You review transcripts of outbound sales calls made by an AI voice agent.
You know the playbook the agent was running. Be honest and specific: the goal is to
improve the next thousand calls. Judge the disposition from what the prospect actually
said, not from what the agent logged. If the call was cut short (transcript ends
abruptly, prospect never spoke), say so in the summary and recommend a retry."""


def review_call(
    client: anthropic.Anthropic,
    playbook: Playbook,
    transcript_text: str,
    tool_events: list[dict],
    mode: str,
    settings: Settings | None = None,
) -> CallReview:
    settings = settings or get_settings()
    tool_text = "\n".join(f"- {e['tool']}({e['arguments']}) -> {e['result']}" for e in tool_events) or "- (none)"
    user_msg = f"""<playbook_summary>
Company: {playbook.company_name}
Goal: {playbook.call_goal} - {playbook.call_goal_details}
Agent persona: {playbook.persona.agent_name}, {playbook.persona.role_title}
Call mode: {mode}
</playbook_summary>

<transcript>
{transcript_text or "(empty - nobody spoke)"}
</transcript>

<tools_used>
{tool_text}
</tools_used>

Valid dispositions: {", ".join(DISPOSITIONS)}.
Review the call."""
    return parse_structured(
        client,
        model=settings.postcall_model_id,
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        output_model=CallReview,
        effort="medium",
        max_tokens=6000,
        settings=settings,
    )


async def finalize_call(
    call_run_id: int,
    playbook: Playbook,
    transcript: list[dict],
    tool_events: list[dict],
    mode: str,
    agent_disposition: str | None,
    settings: Settings | None = None,
    latency: dict[str, Any] | None = None,
) -> CallReview | None:
    """Persist transcript, run the review, update the lead's status/retry schedule."""
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)

    def _persist_transcript(s):
        run = s.get(CallRun, call_run_id)
        if run is None:
            return None
        run.transcript = transcript
        run.tool_events = tool_events
        if latency:
            run.summary = {**(run.summary or {}), "latency": latency}
        run.ended_at = run.ended_at or now
        if run.started_at:
            run.duration_seconds = int((run.ended_at - run.started_at).total_seconds())
        return run.lead_id

    lead_id = await run_db(_persist_transcript)
    if lead_id is None:
        return None

    review: CallReview | None = None
    if transcript or mode == "voicemail":
        try:
            who = {"user": "Prospect", "assistant": playbook.persona.agent_name}
            text = "\n".join(f"{who.get(t['role'], t['role'])}: {t['text']}" for t in transcript)
            client = make_client(settings)
            review = await __import__("asyncio").to_thread(
                review_call, client, playbook, text, tool_events, mode, settings
            )
        except Exception as e:  # review is best-effort; the call record must still close
            review = None
            err = str(e)

            def _err(s):
                run = s.get(CallRun, call_run_id)
                if run:
                    run.error = f"review failed: {err}"

            await run_db(_err)

    disposition = (review.disposition if review else None) or agent_disposition
    if disposition is None:
        disposition = "voicemail_left" if mode == "voicemail" else ("other" if transcript else "no_answer")

    def _apply(s):
        run = s.get(CallRun, call_run_id)
        lead = s.get(Lead, lead_id)
        if run is None or lead is None:
            return
        run.disposition = disposition
        if review:
            run.summary = {**(run.summary or {}), "review": review.model_dump()}
        lead.last_disposition = disposition
        if lead.status == "dnc":
            return
        if lead.status == "callback" and lead.next_attempt_at and lead.next_attempt_at > now:
            return  # callback already scheduled by the in-call tool
        terminal = {"meeting_booked", "sale_closed", "not_interested", "not_qualified", "wrong_number", "do_not_call"}
        if disposition in terminal:
            lead.status = "completed"
        elif review and review.retry_recommended and lead.attempts < settings.max_attempts:
            lead.status = "pending"
            lead.next_attempt_at = now + timedelta(hours=max(1, review.retry_after_hours))
        elif disposition in {"no_answer", "voicemail_left", "gatekeeper", "callback_requested", "other"} and lead.attempts < settings.max_attempts:
            lead.status = "pending"
            lead.next_attempt_at = now + timedelta(minutes=settings.retry_delay_minutes)
        else:
            lead.status = "completed"
        if review and review.prospect_facts:
            facts = "; ".join(review.prospect_facts)
            lead.notes = (lead.notes + "\n" if lead.notes else "") + f"[{now:%Y-%m-%d}] {facts}"

    await run_db(_apply)
    return review
