"""Turn a campaign's script and documents into a draft playbook for review."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Campaign, KnowledgeChunk, TrainingDocument, TrainingRun, run_db, utcnow
from ..llm import AITransientError, attributed
from ..playbook.schema import Playbook
from . import versions

# Characters of source material handed to the model in one training run. Material
# beyond this stays searchable on calls; it is only left out of the playbook draft.
MATERIAL_BUDGET = 180_000


class TrainingError(ValueError):
    pass


def gather_material(s: Session, campaign: Campaign, budget: int = MATERIAL_BUDGET) -> tuple[str, list[str], list[str]]:
    """(material, documents used, documents cut short). The owner's own script always
    comes first and is never cut; documents share what is left, in upload order."""
    parts: list[str] = []
    script = (campaign.script_text or "").strip()
    if script:
        parts.append(f"## Sales script written by the business\n{script}")
    remaining = budget - len(script)
    documents = list(
        s.execute(
            select(TrainingDocument)
            .where(TrainingDocument.campaign_id == campaign.id, TrainingDocument.status == "ready")
            .order_by(TrainingDocument.id)
        ).scalars()
    )
    used, shortened = [], []
    share = max(2000, remaining // len(documents)) if documents else 0
    for document in documents:
        if remaining <= 500:
            shortened.append(document.filename)
            continue
        rows = s.execute(
            select(KnowledgeChunk.source_label, KnowledgeChunk.text).where(KnowledgeChunk.document_id == document.id).order_by(KnowledgeChunk.ordinal)
        ).all()
        text = "\n\n".join(f"[{r.source_label}]\n{r.text}" for r in rows)
        allowed = min(share, remaining)
        if len(text) > allowed:
            text = text[:allowed].rsplit("\n", 1)[0] + "\n[... the rest of this document was not included ...]"
            shortened.append(document.filename)
        parts.append(f"## Document: {document.filename}\n{text}")
        used.append(document.filename)
        remaining -= len(text)
    return "\n\n".join(parts), used, shortened


def build_draft(s: Session, campaign: Campaign, analyzer, by: str = "") -> "versions.PlaybookVersion":
    """Ask the model for a playbook from everything the campaign has, and store it as a draft."""
    material, used, shortened = gather_material(s, campaign)
    if not material.strip():
        raise TrainingError("there is nothing to learn from yet: add a script or upload at least one document")
    previous = Playbook.model_validate(campaign.playbook_json) if campaign.playbook_json else None
    playbook = analyzer.analyze(material, previous=previous)
    if previous is not None:
        # Settings a person chose are not the model's to change.
        playbook.additional_languages = previous.additional_languages
        playbook.disclose_ai_upfront = previous.disclose_ai_upfront
        playbook.persona.language = previous.persona.language
        playbook.localized = previous.localized
    note = "Trained from " + (", ".join(used) if used else "the script only") + "."
    if shortened:
        note += " Not fully read (still searchable on calls): " + ", ".join(shortened) + "."
    return versions.propose(s, campaign, playbook.model_dump(), source="training", note=note, by=by)


async def run_training(run_id: int, analyzer_factory=None) -> None:
    """Background job behind POST /train. The result, or the reason it failed, lands on the TrainingRun."""

    def _work(s: Session):
        run = s.get(TrainingRun, run_id)
        if run is None:
            return
        campaign = s.get(Campaign, run.campaign_id)
        try:
            if analyzer_factory is None:
                from ..playbook.analyzer import PlaybookAnalyzer

                analyzer = PlaybookAnalyzer()
            else:
                analyzer = analyzer_factory()
            with attributed(organization_id=campaign.organization_id, campaign_id=campaign.id):
                version = build_draft(s, campaign, analyzer)
            run.status, run.version = "done", version.version
        except AITransientError:
            # The model was overloaded or unreachable: that is no verdict on the material.
            # The run stays "running" and the job queue tries again later.
            s.rollback()
            raise
        except Exception as e:
            s.rollback()
            run = s.get(TrainingRun, run_id)
            run.status, run.error = "failed", str(e)[:2000]
        run.finished_at = utcnow()

    await run_db(_work)
