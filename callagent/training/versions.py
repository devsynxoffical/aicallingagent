"""Playbook history: every change is a version, calls run only the active one.

- A change made directly by the owner (onboarding, an import, a manual edit) becomes
  the active version at once, and the one it replaced is archived.
- A change proposed by the system (training from documents) is a draft. It does not
  touch live calls until someone approves it.
- Rolling back is approving an older version again. Nothing is ever deleted.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import Campaign, PlaybookVersion, campaign_organization_id, utcnow
from ..playbook.schema import Playbook


class VersionError(ValueError):
    pass


def _next_number(s: Session, campaign: Campaign) -> int:
    current = s.execute(select(func.max(PlaybookVersion.version)).where(PlaybookVersion.campaign_id == campaign.id)).scalar()
    return (current or 0) + 1


def _active(s: Session, campaign: Campaign) -> PlaybookVersion | None:
    return s.execute(
        select(PlaybookVersion).where(PlaybookVersion.campaign_id == campaign.id, PlaybookVersion.status == "active")
    ).scalars().first()


def active_number(s: Session, campaign: Campaign) -> int | None:
    """Number of the version calls are running right now."""
    ensure_baseline(s, campaign)
    current = _active(s, campaign)
    return current.version if current is not None else None


def ensure_baseline(s: Session, campaign: Campaign) -> None:
    """A campaign that predates versioning gets its current playbook recorded as version 1."""
    if not campaign.playbook_json:
        return
    exists = s.execute(select(PlaybookVersion.id).where(PlaybookVersion.campaign_id == campaign.id).limit(1)).first()
    if exists is None:
        s.add(
            PlaybookVersion(
                organization_id=campaign_organization_id(s, campaign),
                campaign_id=campaign.id,
                version=1,
                playbook_json=dict(campaign.playbook_json),
                status="active",
                source="existing",
                note="Playbook in use before version history was introduced.",
                activated_at=utcnow(),
            )
        )
        s.flush()


def _publish(s: Session, campaign: Campaign, version: PlaybookVersion, approved_by: str) -> None:
    previous = _active(s, campaign)
    if previous is not None and previous.id != version.id:
        previous.status = "archived"
    version.status = "active"
    version.approved_by = approved_by
    version.activated_at = utcnow()
    campaign.playbook_json = dict(version.playbook_json)
    # Drafts become dialable once nothing blocks; running or paused campaigns keep their state.
    if campaign.status in ("draft", "ready"):
        campaign.status = "ready" if Playbook.model_validate(version.playbook_json).is_ready else "draft"
    s.flush()


def record_active(s: Session, campaign: Campaign, playbook: dict[str, Any], *, source: str, note: str = "", by: str = "") -> PlaybookVersion:
    """The owner changed the playbook directly: record it and make it the active version.
    Saving an identical playbook again does not create a new version."""
    ensure_baseline(s, campaign)
    current = _active(s, campaign)
    if current is not None and current.playbook_json == playbook:
        if campaign.playbook_json != playbook:
            campaign.playbook_json = dict(playbook)
        return current
    version = PlaybookVersion(
        organization_id=campaign_organization_id(s, campaign),
        campaign_id=campaign.id,
        version=_next_number(s, campaign),
        playbook_json=dict(playbook),
        status="draft",
        source=source,
        note=note,
        created_by=by,
    )
    s.add(version)
    s.flush()
    _publish(s, campaign, version, approved_by=by)
    return version


def propose(
    s: Session, campaign: Campaign, playbook: dict[str, Any], *, source: str, note: str = "", by: str = "", changes: list[dict[str, Any]] | None = None
) -> PlaybookVersion:
    """Add a draft for review. Live calls are not affected."""
    ensure_baseline(s, campaign)
    version = PlaybookVersion(
        organization_id=campaign_organization_id(s, campaign),
        campaign_id=campaign.id,
        version=_next_number(s, campaign),
        playbook_json=dict(playbook),
        status="draft",
        source=source,
        note=note,
        created_by=by,
        changes_json=list(changes or []),
    )
    s.add(version)
    s.flush()
    return version


# Fields compared, in the order a reviewer reads a playbook. Lists are compared item by item.
_DIFF_FIELDS = (
    ("opening_line", "Opening line"), ("permission_line", "Permission line"), ("stages", "Call stages"),
    ("qualification_questions", "Qualification questions"), ("objections", "Objection answers"), ("closing_script", "Closing"),
    ("voicemail_script", "Voicemail"), ("callback_policy", "Callback policy"), ("value_props", "Value propositions"),
    ("proof_points", "Proof points"), ("pricing_and_terms", "Pricing and terms"), ("must_say", "Must say"), ("must_not_say", "Must not say"),
    ("offer_summary", "Offer"), ("target_customer", "Target customer"), ("call_goal_details", "Call goal"), ("persona", "Persona"),
    ("additional_languages", "Languages"), ("disclose_ai_upfront", "AI disclosure up front"), ("open_questions", "Open questions"),
)


def _lines(value: Any) -> list[str]:
    """A field as readable lines, so a list of objections diffs one objection at a time."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append(" | ".join(f"{k}: {v}" for k, v in item.items() if v not in ("", None, [])))
            else:
                out.append(str(item))
        return out
    if isinstance(value, dict):
        return [f"{k}: {v}" for k, v in value.items() if v not in ("", None, [])]
    return [str(value)]


def diff(base: dict[str, Any], other: dict[str, Any]) -> list[dict[str, Any]]:
    """What ``other`` changes compared with ``base``: one entry per changed field, with the
    lines removed and added. Unchanged fields are left out."""
    changes = []
    for field, label in _DIFF_FIELDS:
        before, after = _lines(base.get(field)), _lines(other.get(field))
        if before == after:
            continue
        changes.append(
            {"field": field, "label": label, "removed": [l for l in before if l not in after], "added": [l for l in after if l not in before]}
        )
    return changes


def get_version(s: Session, campaign: Campaign, number: int) -> PlaybookVersion:
    ensure_baseline(s, campaign)
    version = s.execute(
        select(PlaybookVersion).where(PlaybookVersion.campaign_id == campaign.id, PlaybookVersion.version == number)
    ).scalars().first()
    if version is None:
        raise VersionError(f"campaign '{campaign.name}' has no version {number}")
    return version


def approve(s: Session, campaign: Campaign, number: int, *, by: str = "", force: bool = False) -> PlaybookVersion:
    """Make a version the one calls run: a reviewed draft, or an older version (rollback).

    A playbook with blocking open questions is refused unless ``force`` is set, so an
    incomplete training cannot go live by a slip of the hand.
    """
    version = get_version(s, campaign, number)
    if version.status == "active":
        return version
    if version.status == "rejected":
        raise VersionError(f"version {number} was rejected and cannot be activated")
    playbook = Playbook.model_validate(version.playbook_json)
    if not playbook.is_ready and not force:
        questions = "; ".join(q.question for q in playbook.blocking_questions)
        raise VersionError(f"version {number} still has blocking open questions: {questions}")
    if version.status == "archived":
        version.note = (version.note + "\n" if version.note else "") + f"Restored on {utcnow():%Y-%m-%d}."
    _publish(s, campaign, version, approved_by=by)
    return version


def reject(s: Session, campaign: Campaign, number: int, *, by: str = "", reason: str = "") -> PlaybookVersion:
    version = get_version(s, campaign, number)
    if version.status != "draft":
        raise VersionError(f"only a draft can be rejected; version {number} is {version.status}")
    version.status = "rejected"
    version.approved_by = by
    if reason:
        version.note = (version.note + "\n" if version.note else "") + f"Rejected: {reason}"
    s.flush()
    return version


def list_versions(s: Session, campaign: Campaign) -> list[PlaybookVersion]:
    ensure_baseline(s, campaign)
    return list(
        s.execute(select(PlaybookVersion).where(PlaybookVersion.campaign_id == campaign.id).order_by(PlaybookVersion.version.desc())).scalars()
    )


def describe(version: PlaybookVersion) -> dict[str, Any]:
    pb = version.playbook_json or {}
    blocking = [q.get("question", "") for q in pb.get("open_questions", []) if q.get("blocking")]
    return {
        "version": version.version,
        "status": version.status,
        "source": version.source,
        "note": version.note,
        "changes": list(version.changes_json or []),
        "created_by": version.created_by,
        "approved_by": version.approved_by,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "activated_at": version.activated_at.isoformat() if version.activated_at else None,
        "readiness_score": int(pb.get("readiness_score", 0) or 0),
        "blocking_questions": blocking,
        "open_questions": len(pb.get("open_questions", [])),
    }
