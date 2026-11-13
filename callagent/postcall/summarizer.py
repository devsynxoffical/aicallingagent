"""After each call: structured review, lead update, and coaching notes for the playbook."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from sqlalchemy import select

from ..db import Appointment, CallRun, Lead, run_db
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
    "transferred_to_human",
    "no_answer",
    "busy",
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
    customer_sentiment: Literal["positive", "neutral", "negative", "mixed", "unknown"] = Field(
        default="unknown", description="How the prospect felt across the call, judged from their own words."
    )
    pain_points: list[str] = Field(default_factory=list, description="Problems the prospect said they have. Their words, not the pitch.")
    buying_intent_score: int = Field(default=0, description="0-100. Likelihood this prospect buys, from what they said.")
    qualification_score: int = Field(default=0, description="0-100. How well the prospect fits the playbook's target customer.")
    objections_handled: list[str] = Field(default_factory=list, description="Objections the agent answered in a way the prospect accepted.")
    missed_opportunities: list[str] = Field(
        default_factory=list, description="Buying signals or openings the agent did not act on. Quote the moment."
    )
    incorrect_statements: list[str] = Field(
        default_factory=list, description="Anything the agent said that the playbook does not support. Empty if none."
    )
    ai_disclosed: bool = Field(default=False, description="True if the agent said it is an AI assistant during the call.")
    language_was_appropriate: bool = Field(default=True, description="False if the agent used the wrong language or mixed languages.")


TERMINAL_DISPOSITIONS = {
    "meeting_booked",
    "sale_closed",
    "not_interested",
    "not_qualified",
    "wrong_number",
    "do_not_call",
    "transferred_to_human",
}

REVIEW_SYSTEM = """You review transcripts of outbound sales calls made by an AI voice agent.
You know the playbook the agent was running. Be honest and specific: the goal is to
improve the next thousand calls. Judge the disposition from what the prospect actually
said, not from what the agent logged. If the call was cut short (transcript ends
abruptly, prospect never spoke), say so in the summary and recommend a retry.
The transcript may be in English, Arabic or French; write your review in English.
List under incorrect_statements any price, guarantee, result or capability the agent
stated that the playbook summary does not support."""


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
Target customer: {playbook.target_customer}
Languages the agent may speak: {", ".join(playbook.languages)}
Must disclose being an AI up front: {playbook.disclose_ai_upfront}
Pricing the agent may state: {playbook.pricing_and_terms or "(none)"}
Proof points the agent may cite: {"; ".join(playbook.proof_points) or "(none)"}
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
    language: dict[str, Any] | None = None,
    direction: str = "outbound",
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
        if language:
            run.summary = {**(run.summary or {}), "language": language}
            run.language = language.get("detected")
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
    # What the agent actually did on the call outranks the reviewer's reading of the transcript.
    if agent_disposition in TERMINAL_DISPOSITIONS:
        disposition = agent_disposition

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
        if direction == "inbound" and not transcript:
            # Nobody spoke: leave the lead exactly as it was before they rang.
            lead.status = (run.summary or {}).get("lead_status_before") or "completed"
            return
        booked = s.execute(select(Appointment.id).where(Appointment.call_run_id == call_run_id).limit(1)).first() is not None
        if booked or disposition in TERMINAL_DISPOSITIONS:
            lead.status = "completed"
        elif direction == "inbound":
            # They called us. Calling back uninvited is not ours to decide: only a callback
            # they asked for (handled above) puts an inbound caller on the dial list.
            lead.status = "completed"
        elif review and review.retry_recommended and lead.attempts < settings.max_attempts:
            lead.status = "pending"
            lead.next_attempt_at = now + timedelta(hours=max(1, review.retry_after_hours))
        elif disposition in {"no_answer", "busy", "voicemail_left", "gatekeeper", "callback_requested", "other"} and lead.attempts < settings.max_attempts:
            lead.status = "pending"
            lead.next_attempt_at = now + timedelta(minutes=settings.retry_delay_minutes)
        else:
            lead.status = "completed"
        if review and review.prospect_facts:
            facts = "; ".join(review.prospect_facts)
            lead.notes = (lead.notes + "\n" if lead.notes else "") + f"[{now:%Y-%m-%d}] {facts}"

    await run_db(_apply)
    return review
