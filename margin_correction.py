"""
margin_correction.py — Smart margin correction: rank positions for sale
using the same weighted signal logic as the buy pipeline (inverted for exit scoring).

Sell priority (higher score = sell first) is determined by:
  1. P/L performance     (45%) — biggest losers sell first
  2. Signal weakness     (40%) — same PIPELINE_WEIGHTS as screener, inverted
  3. Stop-loss proximity (15%) — positions already past or near stop sell first

Positions that score highest are the weakest holds; selling them maximises the
retained value of the remaining portfolio while raising the cash needed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# Mirror the canonical weights from signal_attribution.py
PIPELINE_WEIGHTS: dict[str, float] = {
    "reddit_sentiment": 2.5,
    "news_sentiment":   2.0,
    "momentum":         1.0,
    "congress":         3.0,
    "technicals":       2.0,
    "fundamentals":     1.5,
    "volume":           1.0,
}

# Default stop-loss % applied when no stored stop price is found
DEFAULT_STOP_PCT = 0.07


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _plpc_to_pct(plpc: float) -> float:
    """Normalise Alpaca's unrealized_plpc to a plain percentage.
    Alpaca sometimes returns 0.05 (meaning 5%) and sometimes 5.0.
    """
    return plpc * 100 if abs(plpc) <= 1 else plpc


def _signal_weakness(sym: str, signal_attrs: dict, pos_breakdown: dict) -> float:
    """
    Return a weakness score in [0, 1].  1.0 = completely no signal support.

    Uses signal_attributions[sym].components if available, falling back to
    position_signal_breakdown[sym].components.
    """
    comps: dict[str, float] = {}

    src = signal_attrs.get(sym) or {}
    if src:
        comps = src.get("components") or {}

    if not comps:
        src2 = pos_breakdown.get(sym) or {}
        comps = src2.get("components") or {}

    if not comps:
        # No signal data at all — treat as moderately weak
        return 0.55

    # Weighted average of component scores (each 0..100 in the normalised form)
    w_sum = 0.0
    w_total = 0.0
    for key, weight in PIPELINE_WEIGHTS.items():
        v = _float(comps.get(key, 0))
        w_sum   += v * weight
        w_total += weight

    weighted_avg = w_sum / w_total if w_total > 0 else 50.0
    # Normalise: 100 = strong = low weakness; 0 = no signal = high weakness
    return 1.0 - min(weighted_avg / 100.0, 1.0)


def _stop_proximity(pos: dict, stop_prices: dict) -> float:
    """
    Return a stop-proximity score in [0, 1].
    1.0 = price is at or below the stop (breach confirmed).
    0.0 = price is far above the stop.
    """
    sym      = pos.get("symbol", "")
    current  = _float(pos.get("current_price") or pos.get("price"))
    cost     = _float(pos.get("avg_entry_price") or pos.get("entry"))

    stop = _float(stop_prices.get(sym, 0))
    if not stop and cost > 0:
        stop = cost * (1 - DEFAULT_STOP_PCT)

    if not stop or current <= 0:
        return 0.0

    if current <= stop:
        return 1.0

    # Fraction of the "cost → stop" gap already consumed
    if cost > stop:
        consumed = (cost - current) / (cost - stop)
        return max(0.0, min(consumed, 1.0))

    return 0.0


def score_position_for_sale(
    pos: dict,
    signal_attrs: dict,
    pos_breakdown: dict,
    stop_prices: dict,
) -> dict:
    """
    Score one position for margin-correction sale priority.

    Returns a dict with the original position data plus:
      sell_score   — overall priority [0, 1]; higher = sell sooner
      score_detail — breakdown of the three components
    """
    sym = pos.get("symbol", "")

    # ── Component 1: P/L (45%) ────────────────────────────────────
    plpc     = _float(pos.get("unrealized_plpc") or pos.get("change_today"))
    pl_pct   = _plpc_to_pct(plpc)            # e.g. -8.5 for -8.5%
    pl_pct   = max(-100.0, min(100.0, pl_pct))
    # Transform: range is [-100, +100] → [0, 1] where -100% → 1.0
    pl_score = (-pl_pct + 100.0) / 200.0

    # ── Component 2: Signal weakness (40%) ───────────────────────
    sig_score = _signal_weakness(sym, signal_attrs, pos_breakdown)

    # ── Component 3: Stop proximity (15%) ────────────────────────
    stop_score = _stop_proximity(pos, stop_prices)

    sell_score = (
        pl_score   * 0.45 +
        sig_score  * 0.40 +
        stop_score * 0.15
    )

    return {
        **pos,
        "sell_score": round(sell_score, 4),
        "score_detail": {
            "pl_pct":       round(pl_pct, 2),
            "pl_score":     round(pl_score, 4),
            "signal_weakness": round(sig_score, 4),
            "stop_proximity":  round(stop_score, 4),
        },
    }


def _as_attr_map(signal_attrs) -> dict:
    """Accept list (SQLite) or dict (legacy projection) of signal attributions."""
    if isinstance(signal_attrs, dict):
        if not signal_attrs:
            return {}
        first = next(iter(signal_attrs.values()), None)
        if isinstance(first, dict) and (
            "components" in first or "total_score" in first or "symbol" in first
        ):
            return {str(k).upper(): v for k, v in signal_attrs.items() if isinstance(v, dict)}
        return {}
    if isinstance(signal_attrs, list):
        out = {}
        for row in signal_attrs:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or row.get("ticker") or "").upper()
            if not sym:
                continue
            comps = row.get("components")
            if comps is None:
                try:
                    comps = json.loads(row.get("components_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    comps = {}
            out[sym] = {**row, "components": comps or {}}
        return out
    return {}


def _load_correction_state() -> dict:
    """SQLite-first context for signal/stop data. JSON is non-authoritative fallback."""
    state: dict = {}
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            state = ledger.load_application_state()
    except Exception as e:
        log.warning("[MarginCorrection] SQLite state unavailable: %s", e)
    if state.get("signal_attributions") or state.get("stop_prices") or state.get("positions"):
        return state
    # NON-AUTHORITATIVE fallback: agent_state.json projection cache
    try:
        from app_paths import resolve_app_dir
        path = os.path.join(resolve_app_dir(), "agent_state.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("[MarginCorrection] Could not load agent_state.json fallback: %s", e)
        return {}


def rank_positions_for_margin_correction(
    positions: list[dict],
    state: Optional[dict] = None,
) -> list[dict]:
    """
    Score and rank all positions.  Highest sell_score first.

    state: application state (SQLite ledger preferred). If None, loads from SQLite
           with a non-authoritative JSON fallback.
    """
    if state is None:
        state = _load_correction_state()

    signal_attrs = _as_attr_map(state.get("signal_attributions") or {})
    pos_breakdown = state.get("position_signal_breakdown") or {}
    if isinstance(pos_breakdown, list):
        pos_breakdown = _as_attr_map(pos_breakdown)
    stop_prices = state.get("stop_prices") or {}

    # Ensure stop_prices is a dict; sometimes stored as list
    if not isinstance(stop_prices, dict):
        stop_prices = {}

    scored = [
        score_position_for_sale(p, signal_attrs, pos_breakdown, stop_prices)
        for p in positions
    ]

    # Sort descending: highest sell_score (worst holds) first
    scored.sort(key=lambda x: x["sell_score"], reverse=True)

    log.info(
        "[MarginCorrection] Ranked %d positions — top sell candidates: %s",
        len(scored),
        ", ".join(
            f"{p.get('symbol','?')} ({p['sell_score']:.3f})"
            for p in scored[:5]
        ),
    )
    return scored
