"""Controlled A/B tests of two playbook versions.

Rules that keep a test honest:
- One running test per campaign, so two tests can never share leads.
- A lead's side is decided once, from a hash of the test and the lead, so every retry
  and callback of that lead runs the same version.
- Only leads that had never been dialed when the test started take part. A lead who
  already heard another version would muddy both sides.
- Both sides are pinned to a version number for the life of the test. Approving a new
  version meanwhile changes what other calls run, not the test.
- Results are counted per lead, and no side is called ahead until both have enough
  answered leads and the difference is unlikely to be chance.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import CallRun, Campaign, Experiment, Lead, campaign_organization_id, utcnow
from ..playbook.schema import Playbook
from ..training import versions

CONVERSIONS = ("meeting_booked", "sale_closed")
SIGNIFICANCE = 0.05
MIN_ANSWERED_FLOOR = 30


class ExperimentError(ValueError):
    pass


def running(s: Session, campaign_id: int) -> Experiment | None:
    return s.execute(select(Experiment).where(Experiment.campaign_id == campaign_id, Experiment.status == "running")).scalars().first()


def start(
    s: Session,
    campaign: Campaign,
    variant_version: int,
    *,
    name: str = "",
    variant_share: float = 0.5,
    min_answered_per_arm: int = 50,
    by: str = "",
) -> Experiment:
    """Begin testing ``variant_version`` against the version that is live right now."""
    if running(s, campaign.id) is not None:
        raise ExperimentError("this campaign already has a test running; stop it before starting another")
    if not 0.1 <= variant_share <= 0.9:
        raise ExperimentError("the variant's share of leads must be between 10% and 90%")
    if min_answered_per_arm < MIN_ANSWERED_FLOOR:
        raise ExperimentError(f"a test needs at least {MIN_ANSWERED_FLOOR} answered leads per side before it can be judged")
    control = versions.active_number(s, campaign)
    if control is None:
        raise ExperimentError("this campaign has no live playbook to test against")
    try:
        variant = versions.get_version(s, campaign, variant_version)
    except versions.VersionError as e:
        raise ExperimentError(str(e)) from None
    if variant.version == control:
        raise ExperimentError("the variant must be a different version from the live one")
    if variant.status == "rejected":
        raise ExperimentError(f"version {variant.version} was rejected and cannot be tested")
    playbook = Playbook.model_validate(variant.playbook_json)
    if not playbook.is_ready:
        questions = "; ".join(q.question for q in playbook.blocking_questions)
        raise ExperimentError(f"version {variant.version} still has blocking open questions: {questions}")
    experiment = Experiment(
        organization_id=campaign_organization_id(s, campaign),
        campaign_id=campaign.id,
        name=name.strip()[:200] or f"Version {variant.version} vs version {control}",
        control_version=control,
        variant_version=variant.version,
        variant_share=variant_share,
        min_answered_per_arm=min_answered_per_arm,
        created_by=by,
    )
    s.add(experiment)
    s.flush()
    return experiment


def arm_for(experiment: Experiment, lead_id: int) -> str:
    """The side a lead is on. A pure function of the test and the lead: the same answer on
    every attempt, on every server, with nothing to keep in sync."""
    digest = hashlib.sha256(f"experiment:{experiment.id}:lead:{lead_id}".encode()).digest()
    position = int.from_bytes(digest[:8], "big") / 2**64
    return "variant" if position < experiment.variant_share else "control"


def assign(s: Session, campaign: Campaign, lead: Lead) -> tuple[int | None, str | None, int | None]:
    """(experiment id, arm, playbook version) for the next outbound call to this lead.

    Outside a test, or for a lead that is not eligible, this is ``(None, None, live version)``.
    """
    live = versions.active_number(s, campaign)
    experiment = running(s, campaign.id)
    if experiment is None:
        return None, None, live
    previous = s.execute(select(CallRun.experiment_id).where(CallRun.lead_id == lead.id, CallRun.direction == "outbound")).scalars().all()
    if previous and experiment.id not in previous:
        # Dialed before this test began (or in an earlier test): not part of this one.
        return None, None, live
    arm = arm_for(experiment, lead.id)
    return experiment.id, arm, experiment.variant_version if arm == "variant" else experiment.control_version


def _two_proportion_p_value(conversions_a: int, n_a: int, conversions_b: int, n_b: int) -> float | None:
    """Two-sided p-value that two conversion rates differ only by chance (pooled z-test)."""
    if n_a == 0 or n_b == 0:
        return None
    pooled = (conversions_a + conversions_b) / (n_a + n_b)
    spread = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if spread == 0:
        return 1.0
    z = (conversions_a / n_a - conversions_b / n_b) / spread
    return round(math.erfc(abs(z) / math.sqrt(2)), 4)


def results(s: Session, experiment: Experiment) -> dict[str, Any]:
    """Per-side results counted by lead, and a verdict that only names a leader when the evidence allows."""
    arms: dict[str, dict[str, Any]] = {}
    for arm, version in (("control", experiment.control_version), ("variant", experiment.variant_version)):
        runs = s.execute(select(CallRun).where(CallRun.experiment_id == experiment.id, CallRun.experiment_arm == arm)).scalars().all()
        dialed = {r.lead_id for r in runs}
        answered = {r.lead_id for r in runs if r.stream_connected and r.transcript}
        converted = {r.lead_id for r in runs if r.disposition in CONVERSIONS}
        quality = [r.summary["review"]["agent_quality_score"] for r in runs if (r.summary or {}).get("review", {}).get("agent_quality_score") is not None]
        arms[arm] = {
            "version": version,
            "leads_dialed": len(dialed),
            "leads_answered": len(answered),
            "conversions": len(converted),
            "conversion_rate": round(len(converted) / len(answered), 4) if answered else None,
            "calls": len(runs),
            "average_quality": round(sum(quality) / len(quality), 2) if quality else None,
            "answered_needed": max(0, experiment.min_answered_per_arm - len(answered)),
        }
    control, variant = arms["control"], arms["variant"]
    enough = control["answered_needed"] == 0 and variant["answered_needed"] == 0
    p_value = _two_proportion_p_value(variant["conversions"], variant["leads_answered"], control["conversions"], control["leads_answered"])
    if not enough:
        verdict, leader = "collecting", None
        summary = (
            f"Still collecting: {control['answered_needed']} more answered leads needed on the live version and "
            f"{variant['answered_needed']} on the variant before the two can be compared."
        )
    elif p_value is not None and p_value < SIGNIFICANCE and variant["conversion_rate"] != control["conversion_rate"]:
        leader = "variant" if (variant["conversion_rate"] or 0) > (control["conversion_rate"] or 0) else "control"
        verdict = f"{leader}_ahead"
        ahead, behind = (variant, control) if leader == "variant" else (control, variant)
        summary = (
            f"Version {ahead['version']} converts {ahead['conversion_rate']:.1%} of answered leads against {behind['conversion_rate']:.1%} "
            f"for version {behind['version']}. A gap this size would appear by chance about {p_value:.1%} of the time."
        )
    else:
        verdict, leader = "no_clear_difference", None
        summary = "Both sides have enough answered leads, and the difference between them is within what chance alone produces."
    return {
        "id": experiment.id,
        "name": experiment.name,
        "status": experiment.status,
        "variant_share": experiment.variant_share,
        "min_answered_per_arm": experiment.min_answered_per_arm,
        "started_at": experiment.started_at.isoformat() if experiment.started_at else None,
        "ended_at": experiment.ended_at.isoformat() if experiment.ended_at else None,
        "created_by": experiment.created_by,
        "control": control,
        "variant": variant,
        "enough_data": enough,
        "p_value": p_value if enough else None,
        "verdict": verdict,
        "leader": leader,
        "summary": summary,
    }


def get(s: Session, campaign: Campaign, experiment_id: int) -> Experiment:
    experiment = s.get(Experiment, experiment_id)
    if experiment is None or experiment.campaign_id != campaign.id:
        raise ExperimentError("no such test in this campaign")
    return experiment


def stop(s: Session, campaign: Campaign, experiment_id: int, *, by: str = "") -> Experiment:
    """End the test without changing what is live. Leads go back to the live version."""
    experiment = get(s, campaign, experiment_id)
    if experiment.status != "running":
        raise ExperimentError(f"this test has already ended ({experiment.status})")
    experiment.status, experiment.ended_at, experiment.ended_by = "stopped", utcnow(), by
    s.flush()
    return experiment


def promote(s: Session, campaign: Campaign, experiment_id: int, *, by: str = "", force: bool = False) -> Experiment:
    """End the test and make the variant the live version. A person's decision, never automatic.

    Refused while the evidence does not favour the variant, unless ``force`` says the
    person has seen that and wants it anyway.
    """
    experiment = get(s, campaign, experiment_id)
    if experiment.status != "running":
        raise ExperimentError(f"this test has already ended ({experiment.status})")
    outcome = results(s, experiment)
    if outcome["verdict"] != "variant_ahead" and not force:
        raise ExperimentError("the results do not show the variant ahead yet: " + outcome["summary"])
    try:
        versions.approve(s, campaign, experiment.variant_version, by=by, force=force)
    except versions.VersionError as e:
        raise ExperimentError(str(e)) from None
    experiment.status, experiment.ended_at, experiment.ended_by = "promoted", utcnow(), by
    s.flush()
    return experiment


def list_for(s: Session, campaign: Campaign) -> list[Experiment]:
    return list(s.execute(select(Experiment).where(Experiment.campaign_id == campaign.id).order_by(Experiment.id.desc())).scalars())


def versions_in_test(s: Session, campaign_id: int) -> set[int]:
    experiment = running(s, campaign_id)
    return {experiment.control_version, experiment.variant_version} if experiment else set()
