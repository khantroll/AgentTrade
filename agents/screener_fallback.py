"""Screener-ensemble entry path and analysis failure handling.

When research fails or returns a valid empty set, candidates are ordered by
``signal_strength`` (legacy ``total_score`` when the explicit field is absent).
Equal strength keeps the screener universe order. That research-stage degraded
path is distinct from analysis: once an analysis LLM is actually invoked, an
unusable/failed result fails closed to SKIP.

A schema-valid model vote is left alone. Protective sells stay on the
position-review path, and Tier 2 still sizes any BUY.
"""

import logging

import agent_config as cfg
from buckets import Bucket
from signal_attribution import (
    PIPELINE_HIT_SCALE,
    PIPELINE_WEIGHTS,
    resolve_signal_mix,
    resolve_signal_strength,
)

# Legacy compatibility constant retained for callers/tests that imported it.
# It no longer authorizes BUY after an invoked analysis fails.
ANALYSIS_FALLBACK_MIN_STRENGTH = PIPELINE_WEIGHTS["news_sentiment"] * PIPELINE_HIT_SCALE

_ANALYSIS_ACTIONS = {"BUY", "SELL", "HOLD", "SKIP"}
_ANALYSIS_FAILURE_PREFIXES = (
    "All tiered analysis models failed",
    "All configured LLM providers failed",
    "All models failed",
    "No configured/healthy tiered analysis models",
)

log = logging.getLogger(__name__)


def _rank_confidence(index: int) -> float:
    """Rank prior for logs and the analysis funnel. Sizing does not use it."""
    return round(max(0.56, 0.78 - 0.04 * index), 2)


def screener_fallback_candidates(
    universe: list,
    sources: dict,
    bucket_name: str,
    top_n: int,
) -> list:
    """Build trade candidates ordered by signal strength.

    ``screener_rank`` stays the ensemble position (1-based index in
    ``universe``). Output order follows strength, then that ensemble position.
    Names with no strength number keep their relative universe order after
    any scored name.
    """
    attribution = {}
    if isinstance(sources, dict):
        raw_attr = sources.get("attribution") or {}
        if isinstance(raw_attr, dict):
            attribution = raw_attr

    limit = max(0, int(top_n or 0))
    universe_n = len(universe or [])
    pending = []
    seen = set()
    for index, raw in enumerate(universe or []):
        ticker = str(raw or "").upper().strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        attr = attribution.get(ticker) or {}
        mix = resolve_signal_mix(attr)
        pipelines = attr.get("pipelines") if isinstance(attr.get("pipelines"), dict) else {}
        pipeline_names = list(pipelines.keys()) or [k for k, v in mix.items() if v]
        strength = resolve_signal_strength(attr)
        ensemble_rank = index + 1
        bits = [f"Screener ensemble rank {ensemble_rank}/{universe_n}"]
        if strength is not None:
            bits.append(f"signal_strength {strength}")
        if pipeline_names:
            bits.append("pipelines: " + ", ".join(str(name) for name in pipeline_names[:6]))
        pending.append((strength, index, {
            "ticker": ticker,
            "reason": "; ".join(bits),
            "source": "screener",
            "candidate_source": "screener",
            "entry_source": "screener",
            "screener_rank": ensemble_rank,
            "signal_strength": strength,
            "signal_mix": mix,
            "total_score": strength,
            "signal_components": mix,
            "signal_attribution": attr,
            "bucket": bucket_name,
        }))

    def _order(item):
        strength, index, _row = item
        if strength is None:
            return (1, 0.0, index)
        return (0, -float(strength), index)

    pending.sort(key=_order)
    rows = []
    for _strength, _index, row in pending:
        row["confidence"] = _rank_confidence(len(rows))
        rows.append(row)
        if limit and len(rows) >= limit:
            break
    return rows


def resolve_entry_candidates(outcome, universe: list, sources: dict, bucket_name: str, top_n: int) -> tuple[list, str]:
    """Choose LLM research picks, or the screener ensemble when research did not.

    Returns ``(candidates, entry_source)`` where entry_source is
    ``research``, ``screener``, or ``none``.
    """
    status = getattr(outcome, "status", None)
    picked = list(getattr(outcome, "candidates", None) or [])
    if status == "ok" and picked:
        rank = {str(t).upper(): i + 1 for i, t in enumerate(universe or [])}
        rows = []
        for candidate in picked:
            row = dict(candidate)
            ticker = str(row.get("ticker") or "").upper().strip()
            row["ticker"] = ticker
            row["source"] = "research"
            row["candidate_source"] = "research"
            row["entry_source"] = "research"
            if ticker in rank:
                row["screener_rank"] = rank[ticker]
            rows.append(row)
        return rows, "research"

    if universe:
        rows = screener_fallback_candidates(universe, sources, bucket_name, top_n)
        if rows:
            return rows, "screener"
    return [], "none"


def analysis_result_failed(result) -> bool:
    """True when analysis produced no schema-valid BUY/SELL/HOLD/SKIP.

    A real model SKIP (including tiered ``no_buy_consensus``) is a decision.
    Provider errors, invalid JSON, and empty parses are not.
    """
    if not isinstance(result, dict) or not result:
        return True
    if str(result.get("analysis_status") or "").lower() == "failed":
        return True
    if str(result.get("tiered_status") or "").lower() == "error":
        return True
    if str(result.get("dual_status") or "").lower() == "error":
        return True
    action = str(result.get("action") or result.get("decision") or "").upper().strip()
    if action not in _ANALYSIS_ACTIONS:
        return True
    if action == "SKIP":
        rationale = str(result.get("rationale") or "")
        if rationale.startswith(_ANALYSIS_FAILURE_PREFIXES):
            return True
    return False


def deterministic_signal_decision(
    candidate: dict,
    bucket: Bucket,
    market: dict = None,
    failure=None,
) -> dict:
    """Fail closed when an invoked analysis LLM produces no usable decision.

    Signal strength is preserved for diagnostics, but it cannot authorize a BUY
    after provider exhaustion, invalid JSON, or any other analysis failure.
    The research-stage screener fallback remains a separate degraded path.
    """
    ticker = str((candidate or {}).get("ticker") or "").upper().strip()
    attr = {}
    if isinstance(candidate, dict) and isinstance(candidate.get("signal_attribution"), dict):
        attr = candidate.get("signal_attribution") or {}
    strength = resolve_signal_strength(attr)
    if strength is None:
        strength = resolve_signal_strength(candidate if isinstance(candidate, dict) else None)
    mix = resolve_signal_mix(attr) or resolve_signal_mix(candidate if isinstance(candidate, dict) else None)

    if isinstance(market, dict):
        data = market
    else:
        data = _market_snapshot(ticker, bucket) if ticker else {}

    reason = "analysis_failed"
    if isinstance(failure, dict):
        reason = str(
            failure.get("analysis_reason")
            or failure.get("tiered_status")
            or failure.get("dual_status")
            or "analysis_failed"
        )

    entry_source = "screener"
    if isinstance(candidate, dict):
        entry_source = candidate.get("entry_source") or candidate.get("source") or "screener"
    atr = data.get("atr14")
    if atr in (None, "", 0, 0.0):
        atr = data.get("atr")

    rationale = f"Analysis LLM failed ({reason}); fail-closed SKIP"
    return {
        "ticker": ticker,
        "action": "SKIP",
        "decision": "SKIP",
        "confidence": (candidate or {}).get("confidence") if isinstance(candidate, dict) else None,
        "rationale": rationale,
        "reason": (candidate or {}).get("reason") or rationale if isinstance(candidate, dict) else rationale,
        "source": (candidate or {}).get("source") or entry_source if isinstance(candidate, dict) else entry_source,
        "candidate_source": (
            (candidate or {}).get("candidate_source") or entry_source
            if isinstance(candidate, dict) else entry_source
        ),
        "entry_source": entry_source,
        "analysis_path": "fail_closed",
        "analysis_status": "failed",
        "analysis_reason": reason,
        "bucket": bucket.name,
        "current_price": data.get("current_price"),
        "atr_pct": data.get("atr_pct"),
        "atr": atr,
        "ohlcv": data.get("ohlcv"),
        "total_score": strength,
        "signal_strength": strength,
        "signal_mix": mix,
        "signal_components": mix,
        "reddit_detail": (candidate or {}).get("reddit_detail") if isinstance(candidate, dict) else None,
        "skip_reason": "analysis_failed",
        "blocked_reason": "analysis_failed",
        "dual_agree": bool((candidate or {}).get("dual_agree")) if isinstance(candidate, dict) else False,
    }

def _market_snapshot(ticker: str, bucket: Bucket) -> dict:
    from market_data import fetch_crypto_data, fetch_stock_data, is_crypto_bucket

    fetcher = fetch_crypto_data if is_crypto_bucket(bucket) else fetch_stock_data
    try:
        data = fetcher(ticker)
    except Exception as exc:
        log.warning("[ScreenerFallback] %s quote failed: %s", ticker, exc)
        return {}
    return data or {}


def deterministic_screener_decisions(candidates: list, bucket: Bucket) -> list:
    """Annotate screener-fallback candidates without an analysis LLM.

    Priced names up to ``MAX_ANALYSIS_CANDIDATES`` are BUY so the existing
    risk and execution gates still decide size and whether an order is sent.
    Names without a quote are skipped and the walk continues, so one missing
    price does not erase the entry path.
    """
    buy_slots = max(1, int(getattr(cfg, "MAX_ANALYSIS_CANDIDATES", 2) or 2))
    buys = 0
    decisions = []
    for candidate in candidates or []:
        ticker = str(candidate.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        data = _market_snapshot(ticker, bucket)
        price = 0.0
        try:
            price = float(data.get("current_price") or 0)
        except (TypeError, ValueError):
            price = 0.0

        if price > 0 and buys < buy_slots:
            action = "BUY"
            buys += 1
            skip_reason = ""
            rationale = candidate.get("reason") or "Screener ensemble fallback"
        elif price <= 0:
            action = "SKIP"
            skip_reason = "screener_fallback_no_price"
            rationale = f"Screener fallback skipped {ticker}: no price for risk sizing"
        else:
            action = "SKIP"
            skip_reason = "below_screener_fallback_cutoff"
            rationale = (
                f"Below screener fallback buy slots ({buy_slots}); "
                f"rank {candidate.get('screener_rank')}"
            )

        decisions.append({
            "ticker": ticker,
            "action": action,
            "decision": action,
            "confidence": candidate.get("confidence"),
            "rationale": rationale,
            "reason": candidate.get("reason") or rationale,
            "source": "screener",
            "candidate_source": "screener",
            "entry_source": "screener",
            "analysis_path": "deterministic_screener",
            "screener_rank": candidate.get("screener_rank"),
            "bucket": bucket.name,
            "current_price": price or None,
            "atr_pct": data.get("atr_pct"),
            "atr": data.get("atr14"),
            "ohlcv": data.get("ohlcv"),
            "total_score": candidate.get("signal_strength", candidate.get("total_score")),
            "signal_strength": candidate.get("signal_strength", candidate.get("total_score")),
            "signal_mix": candidate.get("signal_mix") or candidate.get("signal_components") or {},
            "signal_components": candidate.get("signal_components") or candidate.get("signal_mix") or {},
            "skip_reason": skip_reason,
            "blocked_reason": skip_reason,
        })
    log.info(
        "[%s/ScreenerFallback] Deterministic analysis: %d decisions, %d BUY (no analysis LLM)",
        bucket.name,
        len(decisions),
        buys,
    )
    return decisions
