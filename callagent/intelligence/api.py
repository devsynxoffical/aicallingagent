"""HTTP API for sales intelligence: campaign report, scored leads, coach proposals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..apideps import get_campaign, require_api_token
from ..config import Settings, get_settings
from pydantic import BaseModel, Field

from ..db import CallRun, Lead, run_db
from . import coach, experiments
from .report import campaign_report

router = APIRouter(prefix="/api/campaigns/{name}", dependencies=[Depends(require_api_token)], tags=["intelligence"])


@router.get("/report")
async def get_report(name: str):
    """Outcomes, objections, lost reasons and per-version results, counted from the campaign's calls."""
    return await run_db(lambda s: campaign_report(s, get_campaign(s, name)))


def _lead_intelligence(runs: list[CallRun]) -> dict:
    """What a lead's calls say about them, newest review first."""
    reviewed = [r for r in runs if (r.summary or {}).get("review")]
    latest = reviewed[-1].summary["review"] if reviewed else {}
    objections: dict[str, dict] = {}
    for run in reviewed:
        for event in run.summary["review"].get("objection_events") or []:
            entry = objections.setdefault(event.get("category", "other"), {"category": event.get("category", "other"), "raised": 0, "handled": 0, "quotes": []})
            entry["raised"] += 1
            entry["handled"] += bool(event.get("handled"))
            if event.get("quote") and event["quote"] not in entry["quotes"]:
                entry["quotes"].append(event["quote"])
    lost = [run.summary["review"]["lost_reason"] for run in reviewed if run.summary["review"].get("lost_reason") not in (None, "none")]
    intent = latest.get("buying_intent_score")
    return {
        "calls": len(runs),
        "buying_intent_score": intent,
        "buying_intent": None if intent is None else ("high" if intent >= 70 else "medium" if intent >= 40 else "low"),
        "next_action": latest.get("next_action") or None,
        "sentiment": latest.get("customer_sentiment") or None,
        "lost_reason": lost[-1] if lost else None,
        "objections": sorted(objections.values(), key=lambda o: -o["raised"]),
        "pain_points": list(dict.fromkeys(p for run in reviewed for p in run.summary["review"].get("pain_points") or []))[:8],
    }


def _lead_row(lead: Lead, runs: list[CallRun]) -> dict:
    details = lead.score_details or {}
    return {
        "id": lead.id,
        "name": lead.full_name,
        "company": lead.company,
        "phone": lead.phone_e164,
        "email": lead.email,
        "status": lead.status,
        "priority": lead.priority,
        "attempts": lead.attempts,
        "last_disposition": lead.last_disposition,
        "score": lead.score,
        "category": details.get("category", "unscored") if lead.score is not None else "unscored",
        "evidence": details.get("evidence", {}),
        "points": details.get("points", {}),
        **_lead_intelligence(runs),
    }


@router.get("/leads")
async def list_leads(
    name: str,
    category: str | None = None,
    status: str | None = None,
    intent: str | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Leads with their qualification score and what their calls showed, best first.

    Filters: ``category`` (hot, warm, cold, unscored), ``status`` (lead status),
    ``intent`` (high, medium, low buying intent from the latest call), a score range,
    and ``q`` (name, company or phone).
    """
    if category not in (None, "hot", "warm", "cold", "unscored"):
        raise HTTPException(400, "category must be hot, warm, cold or unscored")
    if intent not in (None, "high", "medium", "low"):
        raise HTTPException(400, "intent must be high, medium or low")

    def _list(s):
        c = get_campaign(s, name)
        leads = s.execute(select(Lead).where(Lead.campaign_id == c.id).order_by(Lead.score.desc().nulls_last(), Lead.id)).scalars().all()
        runs_by_lead: dict[int, list[CallRun]] = {}
        for run in s.execute(select(CallRun).where(CallRun.campaign_id == c.id).order_by(CallRun.id)).scalars():
            runs_by_lead.setdefault(run.lead_id, []).append(run)
        needle = (q or "").strip().lower()
        rows = []
        for lead in leads:
            row = _lead_row(lead, runs_by_lead.get(lead.id, []))
            if category and row["category"] != category:
                continue
            if status and row["status"] != status:
                continue
            if intent and row["buying_intent"] != intent:
                continue
            if min_score is not None and (row["score"] is None or row["score"] < min_score):
                continue
            if max_score is not None and (row["score"] is None or row["score"] > max_score):
                continue
            if needle and needle not in f"{row['name']} {row['company']} {row['phone']}".lower():
                continue
            rows.append(row)
        size = max(1, min(limit, 200))
        start = max(0, offset)
        return {"total": len(rows), "limit": size, "offset": start, "leads": rows[start : start + size]}

    return await run_db(_list)


@router.get("/leads/{lead_id}")
async def get_lead(name: str, lead_id: int):
    """One lead: score with the evidence behind it, objections, and every call with its review."""

    def _get(s):
        c = get_campaign(s, name)
        lead = s.get(Lead, lead_id)
        if lead is None or lead.campaign_id != c.id:
            raise HTTPException(404, "lead not found in this campaign")
        runs = list(s.execute(select(CallRun).where(CallRun.lead_id == lead.id).order_by(CallRun.id)).scalars())
        calls = []
        for run in reversed(runs):
            review = (run.summary or {}).get("review") or {}
            calls.append(
                {
                    "call_run_id": run.id,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "direction": run.direction,
                    "language": run.language,
                    "duration_seconds": run.duration_seconds,
                    "disposition": run.disposition,
                    "playbook_version": run.playbook_version,
                    "summary": review.get("summary"),
                    "next_action": review.get("next_action"),
                    "missed_opportunities": review.get("missed_opportunities") or [],
                    "transcript": run.transcript or [],
                }
            )
        return {**_lead_row(lead, runs), "notes": lead.notes, "timezone": lead.timezone, "call_history": calls}

    return await run_db(_get)


@router.post("/coach")
async def run_coach(name: str, settings: Settings = Depends(get_settings)):
    """Ask the coach for an improved playbook. The result is a draft version awaiting
    approval; live calls are not changed."""
    if not settings.anthropic_api_key:
        raise HTTPException(400, "cannot run the coach: ANTHROPIC_API_KEY is not set")

    def _run(s):
        try:
            return coach.propose_improvements(s, get_campaign(s, name), settings=settings)
        except coach.CoachError as e:
            raise HTTPException(409, str(e)) from None

    return await run_db(_run)


# ----------------------------------------------------------------------------- A/B tests


class ExperimentIn(BaseModel):
    variant_version: int
    name: str = Field(default="", max_length=200)
    variant_share: float = Field(default=0.5, ge=0.1, le=0.9)
    min_answered_per_arm: int = Field(default=50, ge=30, le=100000)
    started_by: str = Field(default="", max_length=200)


class ExperimentEndIn(BaseModel):
    by: str = Field(default="", max_length=200)
    force: bool = False


@router.get("/experiments")
async def list_experiments(name: str):
    """Every A/B test of this campaign, newest first, with current results."""
    return await run_db(lambda s: [experiments.results(s, e) for e in experiments.list_for(s, get_campaign(s, name))])


@router.post("/experiments", status_code=201)
async def start_experiment(name: str, body: ExperimentIn):
    """Test a playbook version against the live one on leads that have not been dialed yet."""

    def _start(s):
        try:
            experiment = experiments.start(
                s, get_campaign(s, name), body.variant_version, name=body.name, variant_share=body.variant_share,
                min_answered_per_arm=body.min_answered_per_arm, by=body.started_by,
            )
        except experiments.ExperimentError as e:
            raise HTTPException(409 if "already has a test" in str(e) else 400, str(e)) from None
        return experiments.results(s, experiment)

    return await run_db(_start)


@router.get("/experiments/{experiment_id}")
async def get_experiment(name: str, experiment_id: int):
    def _get(s):
        try:
            return experiments.results(s, experiments.get(s, get_campaign(s, name), experiment_id))
        except experiments.ExperimentError as e:
            raise HTTPException(404, str(e)) from None

    return await run_db(_get)


@router.post("/experiments/{experiment_id}/stop")
async def stop_experiment(name: str, experiment_id: int, body: ExperimentEndIn | None = None):
    """End the test and keep the live version."""
    body = body or ExperimentEndIn()

    def _stop(s):
        try:
            return experiments.results(s, experiments.stop(s, get_campaign(s, name), experiment_id, by=body.by))
        except experiments.ExperimentError as e:
            raise HTTPException(400, str(e)) from None

    return await run_db(_stop)


@router.post("/experiments/{experiment_id}/promote")
async def promote_experiment(name: str, experiment_id: int, body: ExperimentEndIn | None = None):
    """End the test and make the variant the live version. Refused unless the results favour
    the variant, or ``force`` is set."""
    body = body or ExperimentEndIn()

    def _promote(s):
        try:
            return experiments.results(s, experiments.promote(s, get_campaign(s, name), experiment_id, by=body.by, force=body.force))
        except experiments.ExperimentError as e:
            raise HTTPException(400, str(e)) from None

    return await run_db(_promote)
