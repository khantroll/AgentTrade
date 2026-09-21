"""Confidence calibration — raw LLM confidence adjusted by historical accuracy (Tier 3)."""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def historical_accuracy_for_signal(signal_profile: dict) -> float:
    """
    Estimate accuracy from stored source stats weighted by component presence.
    Falls back to 0.55 (neutral prior) when no history or DB unavailable.
    """
    try:
        from agenttrade import db as ledger

        stats = ledger.get_source_accuracy_stats()
    except Exception as exc:
        log.warning("[Confidence] source accuracy unavailable — using neutral prior: %s", exc)
        return 0.55

    if not stats:
        return 0.55

    components = signal_profile.get("components") or {}
    if not components:
        return 0.55

    weighted = 0.0
    weight_sum = 0.0
    for source, weight in components.items():
        if not weight:
            continue
        src = stats.get(source)
        if not src:
            continue
        win_rate = _float(src.get("win_rate_pct")) / 100.0
        weighted += win_rate * weight
        weight_sum += weight

    if weight_sum <= 0:
        return 0.55
    return _clamp(weighted / weight_sum, 0.25, 0.85)


def source_quality_score(signal_profile: dict) -> float:
    """Quality from Reddit mention quality, agreement, pipeline breadth."""
    components = signal_profile.get("components") or {}
    reddit = _float(components.get("reddit_sentiment"))
    agreement = _float(signal_profile.get("agreement_score"), 0.5)
    pipeline_count = len(signal_profile.get("pipelines") or {})
    breadth = min(pipeline_count / 4.0, 1.0)

    reddit_q = _float((signal_profile.get("reddit_detail") or {}).get("quality_score"))
    reddit_factor = _clamp(0.4 + reddit_q / 20.0 + reddit / 100.0, 0.3, 1.0)

    return _clamp(0.35 * reddit_factor + 0.35 * agreement + 0.30 * breadth, 0.3, 1.0)


def calibrate_confidence(
    raw_confidence: float,
    signal_profile: dict,
    *,
    historical_accuracy: Optional[float] = None,
    source_quality: Optional[float] = None,
    signal_agreement: Optional[float] = None,
) -> dict:
    """
    Produce calibrated confidence from raw LLM/strategy confidence.

    Example: 0.92 raw → ~0.61 calibrated when historical accuracy is weak.
    """
    raw = _clamp(_float(raw_confidence, 0.5))
    hist = (
        historical_accuracy
        if historical_accuracy is not None
        else historical_accuracy_for_signal(signal_profile)
    )
    quality = source_quality if source_quality is not None else source_quality_score(signal_profile)
    agreement = _clamp(
        _float(
            signal_agreement if signal_agreement is not None else signal_profile.get("agreement_score"),
            0.5,
        )
    )

    calibrated = raw * (0.35 + 0.65 * hist) * quality * (0.65 + 0.35 * agreement)
    calibrated = _clamp(calibrated)

    return {
        "raw_confidence": round(raw, 4),
        "calibrated_confidence": round(calibrated, 4),
        "historical_accuracy": round(hist, 4),
        "source_quality": round(quality, 4),
        "signal_agreement": round(agreement, 4),
        "calibration_reason": (
            f"raw={raw:.2f} × hist={hist:.2f} × quality={quality:.2f} × agreement={agreement:.2f}"
        ),
    }


def apply_calibration_to_decisions(
    decisions: list[dict],
    attribution_map: dict[str, dict],
) -> list:
    """
    Enrich analysis decisions with calibrated confidence (advisory only).

    Never raises. When calibration data is unavailable, returns copies unchanged.
    """
    if not decisions:
        return []

    try:
        from agenttrade import db as ledger

        ledger.get_source_accuracy_stats()
    except Exception as exc:
        log.warning(
            "[Confidence] calibration data unavailable — %d decision(s) unchanged: %s",
            len(decisions),
            exc,
        )
        return [dict(d) for d in decisions]

    out: list[dict] = []
    for d in decisions:
        merged = dict(d)
        try:
            sym = str(d.get("ticker") or "").upper()
            profile = dict((attribution_map or {}).get(sym) or {})
            profile["agreement_score"] = d.get("agreement_score")
            profile["reddit_detail"] = profile.get("reddit_detail") or d.get("reddit_detail")

            raw = _float(d.get("confidence"), 0.5)
            cal = calibrate_confidence(raw, profile, signal_agreement=d.get("agreement_score"))
            merged["raw_confidence"] = cal["raw_confidence"]
            merged["calibrated_confidence"] = cal["calibrated_confidence"]
            merged["confidence"] = cal["calibrated_confidence"]
            merged["confidence_calibration"] = cal
        except Exception as exc:
            log.warning(
                "[Confidence] %s: calibration skipped — returning raw confidence (%s)",
                d.get("ticker"),
                exc,
            )
        out.append(merged)
    return out
