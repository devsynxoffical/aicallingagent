"""What a campaign's calls say, in numbers: outcomes, objections, lost reasons,
and how each playbook version and language performs.

Everything here is counted from stored call reviews. Nothing is estimated by a model.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import CallRun, Campaign, Lead
from ..dialer.rules import campaign_spend

CONVERSIONS = {"meeting_booked", "sale_closed"}
# Below this many answered calls a rate is noise, not a result.
MIN_CALLS_TO_COMPARE = 30


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _group() -> dict[str, Any]:
    return {"calls": 0, "answered": 0, "conversions": 0, "quality_sum": 0, "quality_n": 0}


def _close(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "calls": group["calls"],
        "answered": group["answered"],
        "conversions": group["conversions"],
        "conversion_rate": _rate(group["conversions"], group["answered"]),
        "average_quality": round(group["quality_sum"] / group["quality_n"], 2) if group["quality_n"] else None,
        "enough_data": group["answered"] >= MIN_CALLS_TO_COMPARE,
    }


def campaign_report(s: Session, campaign: Campaign) -> dict[str, Any]:
    runs = list(s.execute(select(CallRun).where(CallRun.campaign_id == campaign.id).order_by(CallRun.id)).scalars())
    dispositions: Counter[str] = Counter()
    lost: Counter[str] = Counter()
    competitors: Counter[str] = Counter()
    sentiment: Counter[str] = Counter()
    objections: dict[str, dict[str, Any]] = defaultdict(lambda: {"raised": 0, "handled": 0, "calls": 0, "converted_calls": 0, "examples": []})
    by_version: dict[str, dict[str, Any]] = defaultdict(_group)
    by_language: dict[str, dict[str, Any]] = defaultdict(_group)
    by_direction: dict[str, dict[str, Any]] = defaultdict(_group)
    missed: Counter[str] = Counter()
    unsupported: list[dict[str, Any]] = []
    answered = conversions = reviewed = 0

    for run in runs:
        review = (run.summary or {}).get("review") or {}
        was_answered = bool(run.stream_connected and run.transcript)
        converted = run.disposition in CONVERSIONS
        if run.disposition:
            dispositions[run.disposition] += 1
        answered += was_answered
        conversions += converted
        for key, bucket in ((str(run.playbook_version or "unknown"), by_version), (run.language or "unknown", by_language), (run.direction or "outbound", by_direction)):
            group = bucket[key]
            group["calls"] += 1
            group["answered"] += was_answered
            group["conversions"] += converted
            if review.get("agent_quality_score") is not None:
                group["quality_sum"] += review["agent_quality_score"]
                group["quality_n"] += 1
        if not review:
            continue
        reviewed += 1
        if review.get("lost_reason") and review["lost_reason"] != "none":
            lost[review["lost_reason"]] += 1
        if review.get("customer_sentiment"):
            sentiment[review["customer_sentiment"]] += 1
        for name in review.get("competitors_mentioned") or []:
            if name.strip():
                competitors[name.strip().title()] += 1
        seen_categories = set()
        for event in review.get("objection_events") or []:
            entry = objections[event.get("category", "other")]
            entry["raised"] += 1
            entry["handled"] += bool(event.get("handled"))
            if event.get("quote") and len(entry["examples"]) < 3:
                entry["examples"].append(event["quote"])
            seen_categories.add(event.get("category", "other"))
        for category in seen_categories:
            objections[category]["calls"] += 1
            objections[category]["converted_calls"] += converted
        for item in review.get("missed_opportunities") or []:
            missed[item.strip()] += 1
        for statement in review.get("incorrect_statements") or []:
            unsupported.append({"call_run_id": run.id, "statement": statement})

    objection_rows = [
        {
            "category": category,
            "raised": e["raised"],
            "handled": e["handled"],
            "handled_rate": _rate(e["handled"], e["raised"]),
            "calls": e["calls"],
            # How often a call with this objection still converted: the objections that cost the most deals rank lowest here.
            "conversion_rate_when_raised": _rate(e["converted_calls"], e["calls"]),
            "examples": e["examples"],
        }
        for category, e in sorted(objections.items(), key=lambda kv: -kv[1]["raised"])
    ]

    leads = list(s.execute(select(Lead.score, Lead.score_details).where(Lead.campaign_id == campaign.id)).all())
    lead_categories = Counter((details or {}).get("category", "unscored") if score is not None else "unscored" for score, details in leads)
    spend = campaign_spend(s, campaign.id)

    versions = {key: _close(group) for key, group in sorted(by_version.items())}
    comparable = [(key, v) for key, v in versions.items() if v["enough_data"] and key != "unknown"]
    best = max(comparable, key=lambda kv: kv[1]["conversion_rate"] or 0)[0] if len(comparable) >= 2 else None

    return {
        "campaign": campaign.name,
        "calls": {
            "total": len(runs),
            "answered": answered,
            "reviewed": reviewed,
            "conversions": conversions,
            "conversion_rate": _rate(conversions, answered),
            "dispositions": dict(dispositions.most_common()),
        },
        "cost": {
            "spend_usd": spend,
            "cost_per_answered_call": round(spend / answered, 2) if answered and spend else None,
            "cost_per_conversion": round(spend / conversions, 2) if conversions and spend else None,
        },
        "objections": objection_rows,
        "lost_reasons": dict(lost.most_common()),
        "competitors": dict(competitors.most_common(15)),
        "sentiment": dict(sentiment.most_common()),
        "missed_opportunities": [{"text": text, "calls": n} for text, n in missed.most_common(10)],
        "unsupported_statements": unsupported[:50],
        "by_playbook_version": versions,
        # Named only when at least two versions each have enough answered calls to compare.
        "best_version": best,
        "by_language": {key: _close(group) for key, group in sorted(by_language.items())},
        # Calls the agent placed (outbound) and calls it answered (inbound).
        "by_direction": {key: _close(by_direction[key]) for key in ("outbound", "inbound") if key in by_direction},
        "leads": {"total": len(leads), **{c: lead_categories.get(c, 0) for c in ("hot", "warm", "cold", "unscored")}},
    }
