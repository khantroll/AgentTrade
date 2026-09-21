"""Historical signal tracking, outcomes, and scorecards (Tier 3)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agenttrade import db as ledger

log = logging.getLogger(__name__)

EVAL_HORIZONS = (1, 3, 7, 30)


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def record_signal_snapshot(
    cycle_run_id: int,
    *,
    symbol: str,
    signal_type: str,
    decision: str = "",
    price: float = 0.0,
    raw_confidence: float = 0.0,
    calibrated_confidence: float = 0.0,
    agreement_score: float = 0.0,
    total_score: float = 0.0,
    components: Optional[dict] = None,
    explainability: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> int:
    """Store signal for backtesting foundation."""
    row = {
        "symbol": symbol.upper(),
        "signal_type": signal_type,
        "decision": decision,
        "price": price,
        "raw_confidence": raw_confidence,
        "calibrated_confidence": calibrated_confidence,
        "agreement_score": agreement_score,
        "total_score": total_score,
        "components": components or {},
        "explainability": explainability or {},
        **(extra or {}),
    }
    return ledger.insert_signal_snapshot(cycle_run_id, row)


def record_candidate_snapshots(
    cycle_run_id: int,
    candidates: list,
    attribution_map: dict,
) -> None:
    for c in candidates or []:
        sym = str(c.get("ticker") or "").upper()
        attr = attribution_map.get(sym) or {}
        record_signal_snapshot(
            cycle_run_id,
            symbol=sym,
            signal_type="CANDIDATE",
            decision="CANDIDATE",
            price=_float(c.get("current_price")),
            raw_confidence=_float(c.get("confidence"), 0.5),
            calibrated_confidence=_float(c.get("calibrated_confidence") or c.get("confidence"), 0.5),
            agreement_score=_float(c.get("agreement_score"), 0.5),
            total_score=_float(attr.get("total_score")),
            components=attr.get("components"),
            explainability={
                "reason": c.get("reason") or c.get("rationale"),
                "tiered_votes": c.get("tiered_votes"),
                "dual_agree": c.get("dual_agree"),
            },
            extra=c,
        )


def record_decision_snapshots(
    cycle_run_id: int,
    decisions: list,
    attribution_map: dict,
) -> None:
    for d in decisions or []:
        sym = str(d.get("ticker") or "").upper()
        attr = attribution_map.get(sym) or {}
        action = str(d.get("action") or d.get("decision", "SKIP")).upper()
        record_signal_snapshot(
            cycle_run_id,
            symbol=sym,
            signal_type="ANALYSIS",
            decision=action,
            price=_float(d.get("current_price")),
            raw_confidence=_float(d.get("raw_confidence") or d.get("confidence"), 0.5),
            calibrated_confidence=_float(d.get("calibrated_confidence") or d.get("confidence"), 0.5),
            agreement_score=_float(d.get("agreement_score"), 0.5),
            total_score=_float(attr.get("total_score")),
            components=attr.get("components"),
            explainability={
                "bull_case": d.get("bull_case"),
                "bear_case": d.get("bear_case"),
                "key_risks": d.get("key_risks") or d.get("risks"),
                "signal_summary": d.get("signal_summary"),
                "reddit_summary": d.get("reddit_summary") or d.get("sentiment_summary"),
                "why_now": d.get("why_now"),
                "rationale": d.get("rationale"),
            },
            extra=d,
        )


def _fetch_price_history(symbol: str, start: datetime, end: datetime) -> list[float]:
    """Lightweight price series for outcome evaluation."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        hist = t.history(start=start.date(), end=(end + timedelta(days=1)).date())
        if hist.empty:
            return []
        return [float(x) for x in hist["Close"].tolist()]
    except Exception as e:
        log.debug("[SignalPerformance] Price fetch failed for %s: %s", symbol, e)
        return []


def evaluate_snapshot_outcomes(snapshot: dict, horizons: tuple = EVAL_HORIZONS) -> list[dict]:
    """Evaluate price change / max gain / drawdown for one snapshot."""
    sym = snapshot.get("symbol")
    price_at = _float(snapshot.get("price"))
    created = _parse_ts(snapshot.get("created_at") or "")
    if not sym or price_at <= 0:
        return []

    now = datetime.now(timezone.utc)
    results = []
    max_horizon = max(horizons)
    prices = _fetch_price_history(sym, created, created + timedelta(days=max_horizon + 2))
    if not prices:
        return results

    for days in horizons:
        target = created + timedelta(days=days)
        if target > now:
            continue
        idx = min(days, len(prices) - 1)
        window = prices[: idx + 1]
        if not window:
            continue
        eval_price = window[-1]
        pct = (eval_price - price_at) / price_at * 100
        peak = max(window)
        trough = min(window)
        max_gain = (peak - price_at) / price_at * 100
        max_dd = (trough - price_at) / price_at * 100
        results.append({
            "signal_snapshot_id": snapshot.get("id"),
            "horizon_days": days,
            "evaluated_at": utc_now_str(),
            "price_at_signal": price_at,
            "price_at_eval": eval_price,
            "price_change_pct": round(pct, 3),
            "max_gain_pct": round(max_gain, 3),
            "max_drawdown_pct": round(max_dd, 3),
        })
    return results


def utc_now_str() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def evaluate_pending_outcomes(limit: int = 50) -> int:
    """Evaluate matured signal snapshots; returns count of outcomes written."""
    pending = ledger.get_signal_snapshots(limit=limit, pending_outcomes=True)
    count = 0
    for snap in pending:
        sid = snap.get("id")
        if not sid:
            continue
        for outcome in evaluate_snapshot_outcomes(snap):
            ledger.insert_signal_outcome(int(sid), outcome)
            count += 1
    if count:
        log.info("[SignalPerformance] Recorded %d outcome rows", count)
    return count


def compute_signal_scorecards(max_signals: int = 100) -> dict:
    """
    Scorecards from actual stored outcome data — not guessed.

    Returns win rate and avg return per signal source component.
    """
    rows = ledger.get_signal_outcomes_for_scorecard(limit=max_signals)
    by_source: dict[str, dict] = {}
    combined = {"count": 0, "wins": 0, "return_sum": 0.0, "drawdown_sum": 0.0}

    import json

    for r in rows:
        try:
            components = json.loads(r.get("components_json") or "{}")
        except json.JSONDecodeError:
            components = {}
        pct = _float(r.get("price_change_pct"))
        dd = _float(r.get("max_drawdown_pct"))
        win = pct > 0

        combined["count"] += 1
        combined["wins"] += 1 if win else 0
        combined["return_sum"] += pct
        combined["drawdown_sum"] += dd

        active = [s for s, w in components.items() if _float(w) > 0]
        if len(active) >= 2:
            key = "combined_signals"
            bucket = by_source.setdefault(key, {"count": 0, "wins": 0, "return_sum": 0.0})
            bucket["count"] += 1
            bucket["wins"] += 1 if win else 0
            bucket["return_sum"] += pct

        for source, weight in components.items():
            if _float(weight) <= 0:
                continue
            bucket = by_source.setdefault(source, {"count": 0, "wins": 0, "return_sum": 0.0})
            bucket["count"] += 1
            bucket["wins"] += 1 if win else 0
            bucket["return_sum"] += pct

    def _fmt(bucket: dict) -> dict:
        n = bucket.get("count") or 0
        if n == 0:
            return {"count": 0, "win_rate_pct": None, "avg_gain_pct": None}
        return {
            "count": n,
            "win_rate_pct": round(100 * bucket["wins"] / n, 1),
            "avg_gain_pct": round(bucket["return_sum"] / n, 2),
        }

    scorecards = {src: _fmt(b) for src, b in by_source.items()}
    cn = combined["count"] or 0
    summary = {
        "last_n_signals": cn,
        "win_rate_pct": round(100 * combined["wins"] / cn, 1) if cn else None,
        "avg_return_pct": round(combined["return_sum"] / cn, 2) if cn else None,
        "avg_drawdown_pct": round(combined["drawdown_sum"] / cn, 2) if cn else None,
    }

    sources = list(scorecards.items())
    best = max(sources, key=lambda x: x[1].get("win_rate_pct") or 0, default=(None, {}))
    worst = min(sources, key=lambda x: x[1].get("win_rate_pct") or 100, default=(None, {}))

    return {
        "scorecards": scorecards,
        "summary": summary,
        "best_source": {"name": best[0], **best[1]} if best[0] else None,
        "worst_source": {"name": worst[0], **worst[1]} if worst[0] else None,
    }


def get_signal_performance_report(max_days: int = 90) -> dict:
    """Full report for API/dashboard."""
    evaluate_pending_outcomes(limit=30)
    scorecards = compute_signal_scorecards(max_signals=100)
    snapshots = ledger.get_signal_snapshots(limit=100)
    return {
        "scorecards": scorecards,
        "recent_snapshots": snapshots[:20],
        "source_accuracy": ledger.get_source_accuracy_stats(),
    }
