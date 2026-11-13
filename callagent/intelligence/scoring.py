"""Lead qualification score, computed from evidence, not guessed.

The call review records what the prospect actually said about fit (need, authority,
timing, budget, interest), each as yes / no / unknown. The score is a weighted sum of
those answers. "Unknown" earns nothing, so a lead with no evidence has no score at all
instead of a number that looks like knowledge.

Weights are a starting point, not a universal formula; a campaign can set its own.
"""

from __future__ import annotations

from typing import Any

DEFAULT_WEIGHTS = {
    "fits_target_customer": 20,
    "has_need": 25,
    "decision_maker": 20,
    "timeline": 15,
    "budget_fit": 10,
    "expressed_interest": 10,
}
TIMELINE_CREDIT = {"now": 1.0, "this_quarter": 0.7, "later": 0.3, "none": 0.0, "unknown": 0.0}
FACTORS = tuple(DEFAULT_WEIGHTS)


def merge_evidence(previous: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Combine evidence across a lead's calls: a definite answer is kept until a later
    call gives a different definite answer; 'unknown' never erases what was learned."""
    merged = {factor: previous.get(factor, "unknown") for factor in FACTORS}
    for factor in FACTORS:
        value = new.get(factor, "unknown")
        if value and value != "unknown":
            merged[factor] = value
    quotes = [*previous.get("quotes", []), *new.get("quotes", [])]
    merged["quotes"] = list(dict.fromkeys(q for q in quotes if q))[-8:]
    return merged


def score(evidence: dict[str, Any], weights: dict[str, int] | None = None) -> dict[str, Any]:
    """{'score': 0-100 or None, 'category': hot|warm|cold|unscored, 'points': per factor, 'known': n}."""
    weights = {**DEFAULT_WEIGHTS, **{k: v for k, v in (weights or {}).items() if k in DEFAULT_WEIGHTS}}
    total = sum(weights.values()) or 1
    points: dict[str, float] = {}
    known = 0
    for factor in FACTORS:
        value = evidence.get(factor, "unknown")
        if value != "unknown":
            known += 1
        credit = TIMELINE_CREDIT.get(value, 0.0) if factor == "timeline" else (1.0 if value == "yes" else 0.0)
        points[factor] = round(weights[factor] * credit, 1)
    if known == 0:
        return {"score": None, "category": "unscored", "points": points, "known": 0}
    value = round(sum(points.values()) * 100 / total)
    # A prospect who is not a fit or has no need is cold whatever else they said.
    disqualified = evidence.get("fits_target_customer") == "no" or evidence.get("has_need") == "no"
    category = "cold" if disqualified or value < 40 else ("hot" if value >= 70 else "warm")
    return {"score": value, "category": category, "points": points, "known": known}


def apply_evidence(lead, evidence: dict[str, Any], weights: dict[str, int] | None = None) -> dict[str, Any]:
    """Fold one call's evidence into the lead and recompute its score."""
    merged = merge_evidence((lead.score_details or {}).get("evidence", {}), evidence)
    result = score(merged, weights)
    lead.score = result["score"]
    lead.score_details = {"evidence": merged, **{k: result[k] for k in ("category", "points", "known")}}
    return result
