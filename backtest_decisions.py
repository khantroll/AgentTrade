"""
backtest_decisions.py — Rule-based research + analysis for historical replay.

Mirrors live agent thresholds without LLM calls (fast, reproducible backtests).
Optional use_llm path can be added later.
"""

from __future__ import annotations

import logging
from typing import Callable, List

from buckets import Bucket

log = logging.getLogger(__name__)


def _cfg():
    import agent_config as cfg
    return cfg


def rule_research_agent(
    universe_scores: List[tuple],
    bucket: Bucket,
    metrics_fn: Callable[[str], dict],
    top_n: int = 3,
) -> List[dict]:
    """Pick top tickers from screener ranks; confidence derived from momentum score."""
    candidates = []
    for ticker, score in universe_scores[:top_n]:
        m = metrics_fn(ticker)
        if not m.get("current_price"):
            continue
        # Map typical composite scores (~-0.2..0.4) into 0.45–0.90 confidence
        conf = min(0.92, max(0.40, 0.55 + float(score) * 1.25))
        candidates.append({
            "ticker": ticker,
            "reason": f"Backtest momentum rank (score={score:.3f})",
            "confidence": round(conf, 3),
            "bucket": bucket.name,
        })
    log.info("[%s/Research] Rule picks: %s", bucket.name, [c["ticker"] for c in candidates])
    return candidates


def rule_analysis_agent(
    candidates: List[dict],
    bucket: Bucket,
    metrics_fn: Callable[[str], dict],
) -> List[dict]:
    """Apply analysis heuristics aligned with live aggression settings."""
    decisions = []
    min_conf = _cfg().MIN_CONFIDENCE_FOR_ANALYSIS

    for candidate in candidates:
        ticker = candidate["ticker"]
        conf = float(candidate.get("confidence", 0.5))
        m = metrics_fn(ticker)
        price = m.get("current_price")

        if conf < min_conf:
            decisions.append({
                "ticker": ticker,
                "action": "SKIP",
                "rationale": f"Low research confidence ({conf:.2f})",
                "bucket": bucket.name,
            })
            continue

        if not price:
            decisions.append({
                "ticker": ticker,
                "action": "SKIP",
                "rationale": "No price data",
                "bucket": bucket.name,
            })
            continue

        rsi = m.get("rsi_14")
        sma_20 = m.get("sma_20")

        if _cfg().STRATEGY_AGGRESSION == "conservative":
            reject = (rsi is not None and rsi >= 65) or (sma_20 and price < sma_20)
        elif _cfg().STRATEGY_AGGRESSION == "aggressive":
            reject = rsi is not None and rsi > 78
        else:
            reject = rsi is not None and rsi > 75

        if reject:
            decisions.append({
                "ticker": ticker,
                "action": "SKIP",
                "rationale": f"Technical filter (RSI={rsi}, price vs SMA20)",
                "bucket": bucket.name,
                "current_price": price,
            })
            continue

        # Size hint: risk_agent will finalize shares
        portfolio_guess = 10_000  # overridden by caller context in engine
        max_pos = portfolio_guess * bucket.allocation_pct * bucket.max_position_pct
        shares = max(1, int(max_pos / price))

        decisions.append({
            "ticker": ticker,
            "action": "BUY",
            "shares": shares,
            "rationale": f"Rule BUY: momentum conf={conf:.2f}, RSI={rsi}",
            "bucket": bucket.name,
            "current_price": price,
            "atr_pct": m.get("atr_pct") or 0.5,
        })

    return decisions


def scale_decision_shares(decisions: List[dict], portfolio_value: float, bucket: Bucket) -> None:
    """Refresh share hints using actual portfolio value before risk_agent."""
    max_pos = portfolio_value * bucket.allocation_pct * bucket.max_position_pct
    for d in decisions:
        if d.get("action") != "BUY":
            continue
        price = float(d.get("current_price") or 0)
        if price > 0:
            d["shares"] = max(1, int(max_pos / price))
