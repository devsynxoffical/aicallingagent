"""Training documents: store an upload, read it, index it, and keep its status honest."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import Campaign, KnowledgeChunk, TrainingDocument, campaign_organization_id, run_db, utcnow
from ..languages import guess_latin_language, script_language
from . import web
from .ingest import IngestError, Section, chunk_sections, extract_sections, validate_upload

log = logging.getLogger(__name__)

MAX_DOCUMENTS_PER_CAMPAIGN = 200


def _upload_path(settings: Settings, organization_id: int, campaign_id: int, document_id: int, file_type: str) -> Path:
    # The stored name never contains anything the uploader typed.
    return settings.data_dir / "uploads" / str(organization_id) / str(campaign_id) / f"{document_id}.{file_type}"


def _language_of(sections: list[Section]) -> str:
    sample = " ".join(s.text for s in sections)[:6000]
    return script_language(sample) or guess_latin_language(sample) or "en"


def _next_version(s: Session, campaign_id: int, column, value: str) -> int:
    current = s.execute(select(func.max(TrainingDocument.version)).where(TrainingDocument.campaign_id == campaign_id, column == value)).scalar()
    return (current or 0) + 1


def _check_room(s: Session, campaign_id: int) -> None:
    count = s.execute(
        select(func.count()).select_from(TrainingDocument).where(TrainingDocument.campaign_id == campaign_id, TrainingDocument.status != "superseded")
    ).scalar()
    if (count or 0) >= MAX_DOCUMENTS_PER_CAMPAIGN:
        raise IngestError(f"a campaign can hold at most {MAX_DOCUMENTS_PER_CAMPAIGN} training documents; remove some first")


def add_file(s: Session, campaign: Campaign, filename: str, data: bytes, settings: Settings | None = None) -> TrainingDocument:
    """Validate and store an upload. The document starts as ``pending``; call
    :func:`process_document` to read it."""
    settings = settings or get_settings()
    file_type = validate_upload(filename, data)
    _check_room(s, campaign.id)
    clean_name = Path(filename).name[:300]
    digest = hashlib.sha256(data).hexdigest()
    duplicate = s.execute(
        select(TrainingDocument).where(
            TrainingDocument.campaign_id == campaign.id,
            TrainingDocument.sha256 == digest,
            TrainingDocument.status.in_(("pending", "processing", "ready")),
        )
    ).scalars().first()
    if duplicate is not None:
        raise IngestError(f"this exact file is already uploaded as '{duplicate.filename}'")
    organization_id = campaign_organization_id(s, campaign)
    document = TrainingDocument(
        organization_id=organization_id,
        campaign_id=campaign.id,
        kind="file",
        filename=clean_name,
        file_type=file_type,
        size_bytes=len(data),
        sha256=digest,
        version=_next_version(s, campaign.id, TrainingDocument.filename, clean_name),
    )
    s.add(document)
    s.flush()
    path = _upload_path(settings, organization_id, campaign.id, document.id, file_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    document.storage_path = str(path)
    _accepted(s, document)
    return document


def add_website(s: Session, campaign: Campaign, url: str, max_pages: int = 8, *, resolver: web.Resolver | None = None) -> TrainingDocument:
    """Register a website as a training source. Fetching happens in :func:`process_document`."""
    checked = web.check_public_url(url, resolver) if resolver else web.check_public_url(url)
    _check_room(s, campaign.id)
    document = TrainingDocument(
        organization_id=campaign_organization_id(s, campaign),
        campaign_id=campaign.id,
        kind="url",
        filename=checked[:300],
        source_url=checked[:1000],
        file_type="url",
        max_pages=max(1, min(int(max_pages or 1), web.MAX_PAGES_LIMIT)),
        version=_next_version(s, campaign.id, TrainingDocument.source_url, checked[:1000]),
    )
    s.add(document)
    s.flush()
    _accepted(s, document)
    return document


def _accepted(s: Session, document: TrainingDocument) -> None:
    """A source was accepted: in the same transaction, queue its reading and record the fact."""
    from .. import background, events

    background.enqueue_document_read(s, document)
    events.emit(s, "DocumentUploaded", key=f"document:{document.id}:uploaded", subject="document", subject_id=document.id, organization_id=document.organization_id,
                campaign_id=document.campaign_id, payload={"kind": document.kind, "name": document.filename or document.source_url or ""})


def _read(info: dict[str, Any], fetcher=None, resolver=None, warnings: list[str] | None = None) -> tuple[list[Section], int]:
    """Blocking: returns the document's sections and its size in bytes."""
    if info["kind"] == "url":
        kwargs = {k: v for k, v in (("fetcher", fetcher), ("resolver", resolver)) if v is not None}
        pages = web.crawl_site(info["source_url"], info["max_pages"], **kwargs)
        sections = web.site_sections(pages)
        if not sections:
            raise IngestError("no readable text was found on this website")
        return sections, sum(len(s.text.encode()) for s in sections)
    data = Path(info["storage_path"]).read_bytes()
    return extract_sections(info["file_type"], data, warnings), len(data)


def process_document_sync(s: Session, document_id: int, *, fetcher=None, resolver=None) -> TrainingDocument | None:
    """Read one document and replace its passages. Never raises for a bad document: the
    failure is stored on the document, in words the uploader can act on."""
    document = s.get(TrainingDocument, document_id)
    if document is None:
        return None
    document.status = "processing"
    document.error = document.warning = None
    # Committed before any reading starts. Fetching a website can take minutes; holding a
    # write transaction open that long would block every other writer on SQLite (call
    # status updates, dialing) and hold a row lock on a server database.
    s.commit()
    info = {k: getattr(document, k) for k in ("kind", "source_url", "max_pages", "storage_path", "file_type")}
    warnings: list[str] = []
    try:
        sections, size = _read(info, fetcher, resolver, warnings)
        name = document.filename if document.kind == "file" else (document.source_url or "website")
        passages = chunk_sections(sections, name if document.kind == "file" else "website")
        if not passages:
            raise IngestError("no text could be read from this source")
    except IngestError as e:
        document.status, document.error, document.processed_at = "failed", str(e), utcnow()
        return document
    except Exception as e:  # a parser bug must not leave the document stuck in "processing"
        log.exception("training document %s failed", document_id)
        document.status, document.error, document.processed_at = "failed", f"this file could not be processed ({type(e).__name__})", utcnow()
        return document

    s.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    for ordinal, passage in enumerate(passages):
        s.add(
            KnowledgeChunk(
                organization_id=document.organization_id,
                campaign_id=document.campaign_id,
                document_id=document.id,
                ordinal=ordinal,
                source_label=passage.source_label[:400],
                text=passage.text,
            )
        )
    document.status = "ready"
    document.warning = "; ".join(warnings)[:2000] or None
    document.language = _language_of(sections)
    document.char_count = sum(len(p.text) for p in passages)
    document.chunk_count = len(passages)
    document.size_bytes = document.size_bytes or size
    document.processed_at = utcnow()
    # A newer upload of the same file or address replaces the older one in search; the old row stays for audit.
    same = TrainingDocument.filename == document.filename if document.kind == "file" else TrainingDocument.source_url == document.source_url
    older = s.execute(
        select(TrainingDocument).where(
            TrainingDocument.campaign_id == document.campaign_id,
            TrainingDocument.kind == document.kind,
            same,
            TrainingDocument.id != document.id,
            TrainingDocument.version < document.version,
            TrainingDocument.status == "ready",
        )
    ).scalars()
    for previous in older:
        previous.status = "superseded"
        s.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == previous.id))
    from .. import events

    events.emit(s, "DocumentProcessed", key=f"document:{document.id}:processed:{document.processed_at.isoformat()}", subject="document", subject_id=document.id,
                organization_id=document.organization_id, campaign_id=document.campaign_id, payload={"passages": document.chunk_count, "language": document.language})
    return document


async def process_document(document_id: int) -> None:
    """Background entry point: reading files and websites never blocks the audio loop,
    and runs on the background threads, not the ones calls and webhooks use."""
    await run_db(lambda s: process_document_sync(s, document_id), slow=True)


async def resume_unfinished() -> int:
    """After a restart, pick up documents that were waiting or half-read."""
    ids = await run_db(
        lambda s: [d.id for d in s.execute(select(TrainingDocument).where(TrainingDocument.status.in_(("pending", "processing")))).scalars()]
    )
    from .. import background

    return await background.queue_unread_documents() if ids else 0


def remove_document(s: Session, campaign: Campaign, document_id: int) -> bool:
    document = s.get(TrainingDocument, document_id)
    if document is None or document.campaign_id != campaign.id:
        return False
    s.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    if document.storage_path:
        Path(document.storage_path).unlink(missing_ok=True)
    s.delete(document)
    return True


def list_documents(s: Session, campaign: Campaign, include_superseded: bool = False) -> list[TrainingDocument]:
    query = select(TrainingDocument).where(TrainingDocument.campaign_id == campaign.id).order_by(TrainingDocument.id)
    if not include_superseded:
        query = query.where(TrainingDocument.status != "superseded")
    return list(s.execute(query).scalars())


def describe(document: TrainingDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "kind": document.kind,
        "name": document.filename,
        "source_url": document.source_url,
        "file_type": document.file_type,
        "size_bytes": document.size_bytes,
        "version": document.version,
        "status": document.status,
        "error": document.error,
        "warning": document.warning,  # read, but not all of it
        "language": document.language,
        "passages": document.chunk_count,
        "characters": document.char_count,
        "uploaded_at": document.created_at.isoformat() if document.created_at else None,
        "processed_at": document.processed_at.isoformat() if document.processed_at else None,
    }
