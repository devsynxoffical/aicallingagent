"""Self-training: the agent rehearses the pitch against simulated prospects, then a
coach reviews the transcripts and proposes concrete playbook edits.

Runs entirely in text, so it costs seconds instead of phone minutes. Tools are stubbed
and recorded so the coach can also judge tool discipline (did it log the outcome? did it
try to book before the prospect agreed?).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .llm import live_call_completion, make_client, parse_structured, text_completion, text_of
from .playbook.prompt_builder import build_system_prompt
from .playbook.schema import Playbook
from .voice.tools import build_tools_schema

DEFAULT_PERSONAS = [
    "Busy owner of a small business, polite but wants to hang up within a minute unless hooked.",
    "Skeptical finance lead who asks about price immediately and pushes back on every claim.",
    "Friendly but indecisive office manager who is not the decision maker.",
    "Someone who says 'we already use a competitor' and asks how you are different.",
    "Annoyed person who asks 'how did you get my number' and 'are you a robot?'",
]


@dataclass
class RehearsalTranscript:
    persona: str
    turns: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    ended: bool = False

    def as_text(self, agent_name: str) -> str:
        return "\n".join(f"{agent_name if t['role'] == 'agent' else 'Prospect'}: {t['text']}" for t in self.turns)


class CoachReport(BaseModel):
    overall_score: int = Field(description="0-100 across all rehearsals.")
    strengths: list[str]
    weaknesses: list[str]
    sounded_robotic_examples: list[str] = Field(description="Verbatim agent lines that did not sound human.")
    tool_discipline_issues: list[str]
    playbook_edits: list[str] = Field(description="Concrete edits: 'Change objection X response to ...', 'Add proof point ...'.")
    ready_for_real_calls: bool
    readiness_notes: str


def _tools_as_anthropic(schema) -> list[dict[str, Any]]:
    out = []
    for t in schema.standard_tools:
        out.append(
            {
                "name": t.name,
                "description": t.description,
                "input_schema": {"type": "object", "properties": t.properties, "required": t.required},
            }
        )
    return out


PROSPECT_SYSTEM = """You are role-playing a real person who just picked up their phone.
Persona: {persona}
Context: you are being cold-called about: {offer}. You have never heard of the company.
Speak only as this person, one to two natural sentences per turn, like on a real phone
call (interruptions, half-sentences and 'uh-huh' are fine). React realistically to what
the caller says: reward relevance and warmth, punish monologues and pushiness. If you
decide to hang up, say a short goodbye and end your message with [HANGS UP]. Never break
character, never explain yourself, never write for the caller."""


def rehearse_once(
    client: anthropic.Anthropic,
    playbook: Playbook,
    persona: str,
    settings: Settings,
    max_turns: int = 12,
) -> RehearsalTranscript:
    lead = {"first_name": "Sam", "last_name": "Taylor", "company": "Taylor & Co", "timezone": settings.default_timezone}
    agent_system = build_system_prompt(
        playbook, lead, callback_number="+1 555 010 0100", human_transfer_available=False,
        now=datetime.now(timezone.utc),
    )
    tools = _tools_as_anthropic(build_tools_schema({c.key for c in playbook.required_capabilities}, False))
    rt = RehearsalTranscript(persona=persona)

    agent_messages: list[dict[str, Any]] = [{"role": "user", "content": "Hello?"}]
    prospect_messages: list[dict[str, Any]] = []
    rt.turns.append({"role": "prospect", "text": "Hello?"})

    for _ in range(max_turns):
        # --- agent turn (may call tools; stub them)
        while True:
            reply = live_call_completion(
                client, system=agent_system, messages=agent_messages, tools=tools, settings=settings
            )
            agent_messages.append({"role": "assistant", "content": reply.content})
            spoken = text_of(reply)
            if spoken:
                rt.turns.append({"role": "agent", "text": spoken})
            tool_uses = [b for b in reply.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                rt.tool_calls.append({"tool": tu.name, "input": tu.input})
                if tu.name == "end_call":
                    rt.ended = True
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps({"ok": True, "simulated": True})})
            agent_messages.append({"role": "user", "content": results})
            if rt.ended or reply.stop_reason != "tool_use":
                break
        if rt.ended:
            break

        # --- prospect turn
        prospect_messages.append({"role": "user", "content": spoken or "(silence)"})
        prospect_reply = text_completion(
            client,
            model=settings.onboarding_model,
            system=PROSPECT_SYSTEM.format(persona=persona, offer=playbook.offer_summary),
            messages=prospect_messages,
            effort="low",
            max_tokens=300,
            settings=settings,
        )
        p_text = text_of(prospect_reply)
        prospect_messages.append({"role": "assistant", "content": p_text})
        hung_up = "[HANGS UP]" in p_text
        p_text_clean = p_text.replace("[HANGS UP]", "").strip()
        rt.turns.append({"role": "prospect", "text": p_text_clean or "..."})
        if hung_up:
            rt.ended = True
            break
        agent_messages.append({"role": "user", "content": p_text_clean or "(silence)"})
    return rt


COACH_SYSTEM = """You are a veteran sales coach reviewing rehearsal calls made by an AI voice
agent before it is allowed to call real prospects. You know the playbook it ran. Be
specific and quote lines. The two things that matter most: does it sound like a
relaxed, competent human on the phone, and does it move the call toward the goal
without being pushy. Also check tool discipline: log_call_outcome before end_call,
book_meeting only after explicit agreement, end_call after a goodbye."""


def coach(client: anthropic.Anthropic, playbook: Playbook, transcripts: list[RehearsalTranscript], settings: Settings) -> CoachReport:
    blocks = []
    for i, t in enumerate(transcripts, 1):
        tools = "\n".join(f"  - {c['tool']}({json.dumps(c['input'])})" for c in t.tool_calls) or "  - (none)"
        blocks.append(f"<rehearsal n=\"{i}\" persona=\"{t.persona}\">\n{t.as_text(playbook.persona.agent_name)}\nTools used:\n{tools}\n</rehearsal>")
    user = f"""<playbook>
{playbook.model_dump_json(indent=2)}
</playbook>

{chr(10).join(blocks)}

Write the coach report."""
    return parse_structured(
        client,
        model=settings.onboarding_model,
        system=COACH_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_model=CoachReport,
        effort=settings.onboarding_effort,
        settings=settings,
    )


APPLY_SYSTEM = """You maintain the Playbook of an AI calling agent. Apply the coach's edits
faithfully and conservatively: change only what the edits ask for, keep everything else
byte-for-byte where possible, never invent facts the business did not provide. If an
edit would require facts you do not have, add an open_question for the business instead."""


def apply_coach_edits(client: anthropic.Anthropic, playbook: Playbook, report: CoachReport, settings: Settings) -> Playbook:
    edits = "\n".join(f"- {e}" for e in report.playbook_edits)
    user = f"<playbook>\n{playbook.model_dump_json(indent=2)}\n</playbook>\n\n<edits>\n{edits}\n</edits>\n\nReturn the updated Playbook."
    return parse_structured(
        client,
        model=settings.onboarding_model,
        system=APPLY_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_model=Playbook,
        effort="medium",
        settings=settings,
    )


def run_rehearsal(
    playbook: Playbook,
    personas: list[str] | None = None,
    max_turns: int = 12,
    settings: Settings | None = None,
    client: anthropic.Anthropic | None = None,
    on_transcript=None,
) -> tuple[list[RehearsalTranscript], CoachReport]:
    settings = settings or get_settings()
    client = client or make_client(settings)
    transcripts = []
    for persona in personas or DEFAULT_PERSONAS[:3]:
        t = rehearse_once(client, playbook, persona, settings, max_turns=max_turns)
        transcripts.append(t)
        if on_transcript:
            on_transcript(t)
    return transcripts, coach(client, playbook, transcripts, settings)
