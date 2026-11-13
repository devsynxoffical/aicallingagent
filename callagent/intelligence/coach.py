"""The sales coach: turns patterns across many calls into a draft playbook for approval.

It never edits what is live. It needs a minimum number of reviewed calls before it
says anything, cites the evidence for each change, and cannot touch prices, promises,
compliance lines or language settings: those are copied from the current playbook
after the model answers, whatever it wrote.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import CallRun, Campaign
from ..playbook.schema import Playbook
from ..training import versions
from .report import campaign_report

MIN_REVIEWED_CALLS = 20
# What the business decided. The coach may improve how things are said, never these.
LOCKED_FIELDS = (
    "company_name", "company_description", "offer_summary", "target_customer", "proof_points", "call_goal", "call_goal_details",
    "pricing_and_terms", "must_say", "must_not_say", "disclose_ai_if_asked", "disclose_ai_upfront", "additional_languages",
    "required_capabilities", "localized", "open_questions",
)


class CoachError(ValueError):
    pass


class CoachChange(BaseModel):
    change: str = Field(description="What was changed in the playbook, in one sentence.")
    evidence: str = Field(description="The pattern in the calls that justifies it, with the count.")


class CoachProposal(BaseModel):
    changes: list[CoachChange] = Field(description="Empty if the calls do not justify any change.")
    playbook: Playbook


COACH_SYSTEM = """You are a sales coach reviewing the results of an AI phone agent's campaign.

You get the playbook the agent runs, a report counted from its calls (objections with
how often they were handled, reasons deals were lost, missed opportunities), and the
reviewers' notes from individual calls.

Propose an improved playbook.
- Change only what the evidence supports. A problem seen in one or two calls is noise;
  act on patterns that repeat. If nothing repeats, change nothing and return no changes.
- Improve how the agent talks: the opening, the order of questions, how early it
  pitches, how it answers the objections that actually come up, how it asks for the
  next step. Shorter and more specific beats longer.
- You may add an objection the calls show and the playbook lacks. Answer it only with
  facts already in the playbook.
- Do not change prices, terms, proof points, guarantees, compliance lines, the call
  goal, or languages. Do not invent facts. If the calls show a missing fact (prospects
  keep asking something the playbook cannot answer), say so in a change's evidence;
  do not make the answer up.
- For each change give the evidence with its count from the report."""


def _notes(s: Session, campaign: Campaign, limit: int = 80) -> dict[str, list[str]]:
    runs = s.execute(select(CallRun).where(CallRun.campaign_id == campaign.id).order_by(CallRun.id.desc()).limit(400)).scalars()
    coaching: list[str] = []
    suggestions: list[str] = []
    for run in runs:
        review = (run.summary or {}).get("review") or {}
        coaching.extend(review.get("coaching_notes") or [])
        suggestions.extend(review.get("playbook_suggestions") or [])
    return {"coaching_notes": coaching[:limit], "playbook_suggestions": suggestions[:limit]}


def propose_improvements(s: Session, campaign: Campaign, *, client=None, settings: Settings | None = None, by: str = "coach") -> dict[str, Any]:
    """Ask the coach for a draft. Returns {'version': n or None, 'changes': [...], 'reviewed_calls': n}."""
    settings = settings or get_settings()
    if not campaign.playbook_json:
        raise CoachError("this campaign has no playbook yet")
    report = campaign_report(s, campaign)
    reviewed = report["calls"]["reviewed"]
    if reviewed < MIN_REVIEWED_CALLS:
        raise CoachError(
            f"only {reviewed} reviewed calls so far; the coach waits for {MIN_REVIEWED_CALLS} so it acts on patterns, not single calls"
        )
    current = Playbook.model_validate(campaign.playbook_json)
    if client is None:
        from ..llm import make_client

        client = make_client(settings)
    from ..llm import parse_structured

    slim_report = {k: report[k] for k in ("calls", "objections", "lost_reasons", "competitors", "missed_opportunities", "unsupported_statements", "by_language")}
    proposal = parse_structured(
        client,
        model=settings.onboarding_model,
        system=COACH_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": "<playbook>\n" + current.model_dump_json(indent=2) + "\n</playbook>\n\n<report>\n"
                + json.dumps(slim_report, ensure_ascii=False, indent=2) + "\n</report>\n\n<reviewer_notes>\n"
                + json.dumps(_notes(s, campaign), ensure_ascii=False, indent=2) + "\n</reviewer_notes>\n\nPropose the improved playbook.",
            }
        ],
        output_model=CoachProposal,
        effort=settings.onboarding_effort,
        settings=settings,
    )
    if not proposal.changes:
        return {"version": None, "changes": [], "reviewed_calls": reviewed}

    improved = proposal.playbook.model_dump()
    original = current.model_dump()
    for field in LOCKED_FIELDS:
        improved[field] = original[field]
    improved["persona"]["language"] = original["persona"]["language"]
    improved["persona"]["agent_name"] = original["persona"]["agent_name"]
    if improved == original:
        return {"version": None, "changes": [], "reviewed_calls": reviewed}

    note = f"Coach proposal from {reviewed} reviewed calls.\n" + "\n".join(f"- {c.change} (evidence: {c.evidence})" for c in proposal.changes)
    draft = versions.propose(s, campaign, improved, source="coach", note=note, by=by, changes=[c.model_dump() for c in proposal.changes])
    return {"version": draft.version, "changes": [c.model_dump() for c in proposal.changes], "reviewed_calls": reviewed}
