"""HTTP API for sales intelligence: campaign report, scored leads, coach proposals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..apideps import get_campaign, require_api_token
from ..config import Settings, get_settings
from ..db import Lead, run_db
from . import coach
from .report import campaign_report

router = APIRouter(prefix="/api/campaigns/{name}", dependencies=[Depends(require_api_token)], tags=["intelligence"])


@router.get("/report")
async def get_report(name: str):
    """Outcomes, objections, lost reasons and per-version results, counted from the campaign's calls."""
    return await run_db(lambda s: campaign_report(s, get_campaign(s, name)))


@router.get("/leads")
async def list_leads(name: str, category: str | None = None, limit: int = 100, offset: int = 0):
    """Leads with their qualification score, best first. ``category``: hot, warm, cold or unscored."""
    if category not in (None, "hot", "warm", "cold", "unscored"):
        raise HTTPException(400, "category must be hot, warm, cold or unscored")

    def _list(s):
        c = get_campaign(s, name)
        rows = s.execute(select(Lead).where(Lead.campaign_id == c.id).order_by(Lead.score.desc().nulls_last(), Lead.id)).scalars().all()
        out = []
        for lead in rows:
            details = lead.score_details or {}
            lead_category = details.get("category", "unscored") if lead.score is not None else "unscored"
            if category and lead_category != category:
                continue
            out.append(
                {
                    "id": lead.id,
                    "name": lead.full_name,
                    "company": lead.company,
                    "phone": lead.phone_e164,
                    "status": lead.status,
                    "priority": lead.priority,
                    "last_disposition": lead.last_disposition,
                    "score": lead.score,
                    "category": lead_category,
                    "evidence": details.get("evidence", {}),
                    "points": details.get("points", {}),
                }
            )
        return {"total": len(out), "leads": out[max(0, offset) : max(0, offset) + max(1, min(limit, 500))]}

    return await run_db(_list)


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
