"""HTTP API of the training studio: documents, search preview, training runs, versions."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .. import background as work
from .. import jobs
from ..apideps import acting_as, get_campaign, require_api_token
from ..config import Settings, get_settings
from ..db import TrainingRun, campaign_organization_id, run_db
from . import service, versions
from .builder import run_training
from .ingest import MAX_UPLOAD_BYTES, IngestError
from .search import load_index

router = APIRouter(prefix="/api/campaigns/{name}", dependencies=[Depends(require_api_token)], tags=["training"])


class WebsiteIn(BaseModel):
    url: str = Field(max_length=1000)
    max_pages: int = Field(default=8, ge=1, le=25)


class ApprovalIn(BaseModel):
    approved_by: str = Field(default="", max_length=200)
    force: bool = False
    reason: str = Field(default="", max_length=1000)


def _bad_request(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


# ----------------------------------------------------------------------------- documents


@router.post("/documents", status_code=201)
async def upload_document(name: str, background: BackgroundTasks, file: UploadFile = File(...), settings: Settings = Depends(get_settings)):
    """Upload a PDF, Word, PowerPoint, HTML, CSV or text file. It is read in the background;
    poll GET /documents for its status."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)

    def _add(s):
        try:
            return service.describe(service.add_file(s, get_campaign(s, name), file.filename or "", data, settings))
        except IngestError as e:
            raise _bad_request(e) from None

    document = await run_db(_add)
    background.add_task(service.process_document, document["id"])
    return document


@router.post("/sources", status_code=201)
async def add_website(name: str, body: WebsiteIn, background: BackgroundTasks):
    """Learn from a website: the page given and pages it links to on the same site."""

    def _add(s):
        try:
            return service.describe(service.add_website(s, get_campaign(s, name), body.url, body.max_pages))
        except IngestError as e:
            raise _bad_request(e) from None

    document = await run_db(_add)
    background.add_task(service.process_document, document["id"])
    return document


@router.get("/documents")
async def list_documents(name: str, include_superseded: bool = False):
    return await run_db(lambda s: [service.describe(d) for d in service.list_documents(s, get_campaign(s, name), include_superseded)])


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(name: str, document_id: int):
    if not await run_db(lambda s: service.remove_document(s, get_campaign(s, name), document_id)):
        raise HTTPException(404, "document not found in this campaign")


@router.get("/knowledge/search")
async def search_knowledge(name: str, q: str, limit: int = 4):
    """What the agent would find for this question on a call. Use it to check the training."""

    def _search(s):
        campaign = get_campaign(s, name)
        index = load_index(s, campaign_organization_id(s, campaign), campaign.id)
        hits = index.search(q, limit=max(1, min(limit, 10)))
        return {"passages_indexed": len(index), "results": [{"source": h.source, "text": h.text, "score": h.score} for h in hits]}

    return await run_db(_search)


# ----------------------------------------------------------------------------- training runs


def _describe_run(run: TrainingRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "error": run.error,
        "draft_version": run.version,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.post("/train", status_code=202)
async def start_training(name: str, background: BackgroundTasks, settings: Settings = Depends(get_settings)):
    """Build a draft playbook from the script and every ready document. The draft waits
    for approval; live calls keep running the current version."""
    if not settings.anthropic_api_key:
        raise HTTPException(400, "cannot train: ANTHROPIC_API_KEY is not set")

    def _start(s):
        campaign = get_campaign(s, name)
        from sqlalchemy import select

        busy = s.execute(select(TrainingRun).where(TrainingRun.campaign_id == campaign.id, TrainingRun.status == "running")).scalars().first()
        if busy is not None:
            raise HTTPException(409, "a training run is already in progress for this campaign")
        pending = [d.filename for d in service.list_documents(s, campaign) if d.status in ("pending", "processing")]
        if pending:
            raise HTTPException(409, "still reading: " + ", ".join(pending) + ". Try again when they are ready.")
        run = TrainingRun(organization_id=campaign_organization_id(s, campaign), campaign_id=campaign.id)
        s.add(run)
        s.flush()
        # Saved with the run: a restart in the middle of training no longer leaves the
        # run "in progress" forever, the queue runs it again.
        jobs.enqueue(s, work.TRAINING_RUN, work.training_run_key(run.id), {"run_id": run.id}, max_attempts=2, timeout_seconds=900)
        return _describe_run(run)

    run = await run_db(_start)
    background.add_task(work.run_now, work.training_run_key(run["id"]))
    return run


@router.get("/train")
async def latest_training(name: str):
    def _latest(s):
        from sqlalchemy import select

        campaign = get_campaign(s, name)
        run = s.execute(select(TrainingRun).where(TrainingRun.campaign_id == campaign.id).order_by(TrainingRun.id.desc()).limit(1)).scalars().first()
        if run is None:
            raise HTTPException(404, "no training run yet")
        return _describe_run(run)

    return await run_db(_latest)


# ----------------------------------------------------------------------------- versions


@router.get("/versions")
async def list_versions(name: str):
    return await run_db(lambda s: [versions.describe(v) for v in versions.list_versions(s, get_campaign(s, name))])


@router.get("/versions/{number}")
async def get_version(name: str, number: int):
    def _get(s):
        try:
            v = versions.get_version(s, get_campaign(s, name), number)
        except versions.VersionError as e:
            raise HTTPException(404, str(e)) from None
        return {**versions.describe(v), "playbook": v.playbook_json}

    return await run_db(_get)


@router.get("/versions/{number}/diff")
async def diff_version(name: str, number: int):
    """What this version changes compared with the live one, field by field."""

    def _diff(s):
        campaign = get_campaign(s, name)
        try:
            v = versions.get_version(s, campaign, number)
        except versions.VersionError as e:
            raise HTTPException(404, str(e)) from None
        live = versions.active_number(s, campaign)
        return {"version": v.version, "compared_with": live, "changes": versions.diff(campaign.playbook_json or {}, v.playbook_json or {})}

    return await run_db(_diff)


@router.post("/versions/{number}/approve")
async def approve_version(name: str, number: int, request: Request, body: ApprovalIn | None = None):
    """Make this version the one calls run. Approving an older version is a rollback."""
    body = body or ApprovalIn()

    def _approve(s):
        try:
            return versions.describe(versions.approve(s, get_campaign(s, name), number, by=acting_as(request, body.approved_by), force=body.force))
        except versions.VersionError as e:
            raise _bad_request(e) from None

    return await run_db(_approve)


@router.post("/versions/{number}/reject")
async def reject_version(name: str, number: int, request: Request, body: ApprovalIn | None = None):
    body = body or ApprovalIn()

    def _reject(s):
        try:
            return versions.describe(versions.reject(s, get_campaign(s, name), number, by=acting_as(request, body.approved_by), reason=body.reason))
        except versions.VersionError as e:
            raise _bad_request(e) from None

    return await run_db(_reject)
