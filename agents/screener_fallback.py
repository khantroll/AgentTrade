"""Screener-ensemble entry path when LLM research fails or returns no picks.

The screener universe is already ranked by the existing pipeline ensemble.
This module does not invent a new strength score (that is Milestone B). It
walks universe order, attaches current attribution as evidence, and can
annotate BUY/SKIP decisions without an analysis LLM so entries are not
hard-gated on a provider outage.
"""

import logging

import agent_config as cfg
from buckets import Bucket

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
    """Build trade candidates from screener universe order.

    ``universe`` is the ensemble ranking (best first). Attribution
    ``total_score`` is attached as evidence and is not used to re-rank.
    """
    attribution = {}
    if isinstance(sources, dict):
        raw_attr = sources.get("attribution") or {}
        if isinstance(raw_attr, dict):
            attribution = raw_attr

    limit = max(0, int(top_n or 0))
    rows = []
    seen = set()
    for raw in universe or []:
        ticker = str(raw or "").upper().strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        attr = attribution.get(ticker) or {}
        components = attr.get("components") if isinstance(attr.get("components"), dict) else {}
        pipelines = attr.get("pipelines") if isinstance(attr.get("pipelines"), dict) else {}
        pipeline_names = list(pipelines.keys()) or [k for k, v in components.items() if v]
        rank = len(rows) + 1
        universe_n = len(universe or [])
        bits = [f"Screener ensemble rank {rank}/{universe_n}"]
        if attr.get("total_score") is not None:
            bits.append(f"attribution total_score {attr.get('total_score')}")
        if pipeline_names:
            bits.append("pipelines: " + ", ".join(str(name) for name in pipeline_names[:6]))
        rows.append({
            "ticker": ticker,
            "reason": "; ".join(bits),
            "confidence": _rank_confidence(len(rows)),
            "source": "screener",
            "candidate_source": "screener",
            "entry_source": "screener",
            "screener_rank": rank,
            "total_score": attr.get("total_score"),
            "signal_components": components,
            "signal_attribution": attr,
            "bucket": bucket_name,
        })
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
            "total_score": candidate.get("total_score"),
            "signal_components": candidate.get("signal_components") or {},
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
