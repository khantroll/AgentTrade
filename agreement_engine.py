"""Measurable multi-model / multi-signal agreement (Tier 3)."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _token_set(text: str) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def signal_overlap_score(decisions: list[dict]) -> float:
    """Fraction of models agreeing on BUY vs SKIP."""
    if not decisions:
        return 0.0
    actions = [str(d.get("action") or d.get("decision", "SKIP")).upper() for d in decisions]
    if not actions:
        return 0.0
    buy_votes = sum(1 for a in actions if a == "BUY")
    return buy_votes / len(actions)


def rationale_overlap_score(decisions: list[dict]) -> float:
    """Average pairwise Jaccard similarity of rationale tokens."""
    texts = [d.get("rationale") or d.get("reason") or "" for d in decisions if d]
    if len(texts) < 2:
        return 0.5 if texts else 0.0
    sets = [_token_set(t) for t in texts]
    pairs = 0
    total = 0.0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            total += _jaccard(sets[i], sets[j])
            pairs += 1
    return total / pairs if pairs else 0.0


def confidence_overlap_score(decisions: list[dict]) -> float:
    """1 - normalized std dev of confidences (higher = more aligned)."""
    confs = [_float(d.get("confidence"), 0.5) for d in decisions if d]
    if len(confs) < 2:
        return 0.5
    mean = sum(confs) / len(confs)
    var = sum((c - mean) ** 2 for c in confs) / len(confs)
    std = var ** 0.5
    return max(0.0, 1.0 - std * 2)


def compute_agreement_score(
    decisions: list[dict],
    *,
    signal_profile: Optional[dict] = None,
) -> dict:
    """
    Measurable agreement score in [0, 1].

    Based on signal overlap, rationale overlap, confidence overlap.
    """
    sig = signal_overlap_score(decisions)
    rat = rationale_overlap_score(decisions)
    conf = confidence_overlap_score(decisions)

    pipeline_bonus = 0.0
    if signal_profile:
        n_pipes = len(signal_profile.get("pipelines") or {})
        pipeline_bonus = min(n_pipes / 5.0, 0.15)

    agreement = 0.45 * sig + 0.30 * rat + 0.25 * conf + pipeline_bonus
    agreement = min(1.0, max(0.0, agreement))

    return {
        "agreement_score": round(agreement, 4),
        "signal_overlap": round(sig, 4),
        "rationale_overlap": round(rat, 4),
        "confidence_overlap": round(conf, 4),
        "pipeline_bonus": round(pipeline_bonus, 4),
    }


def enrich_with_agreement(result: dict, model_decisions: Optional[list[dict]] = None) -> dict:
    """Attach agreement_score to LLM router result."""
    out = dict(result or {})
    decisions = model_decisions or []
    if not decisions and out.get("tiered_models_used"):
        decisions = [out]

    dual_status = str(out.get("dual_status", ""))
    if dual_status == "both_buy":
        base = compute_agreement_score(decisions)
        base["agreement_score"] = min(1.0, base["agreement_score"] + 0.15)
        out.update(base)
        return out
    if dual_status == "conflict":
        out["agreement_score"] = 0.15
        out["signal_overlap"] = 0.0
        return out

    tiered_votes = int(out.get("tiered_buy_votes") or 0)
    tiered_total = int(out.get("tiered_total_votes") or 0)
    if tiered_total > 0:
        base = compute_agreement_score(decisions)
        vote_ratio = tiered_votes / tiered_total
        base["agreement_score"] = round(base["agreement_score"] * 0.6 + vote_ratio * 0.4, 4)
        out.update(base)
        return out

    if decisions:
        out.update(compute_agreement_score(decisions))
    else:
        out.setdefault("agreement_score", 0.5)
    return out
