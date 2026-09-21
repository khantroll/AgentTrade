"""Performance metrics from SQLite + Alpaca data (Tier 4)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from agenttrade import db as ledger
from pnl_attribution import compute_attribution, fifo_realized, _load_agent_state

log = logging.getLogger(__name__)


def _float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _insufficient(msg: str = "insufficient data") -> dict:
    return {"status": msg}


def compute_performance_metrics(max_days: int = 90) -> dict:
    """Aggregate return, risk, bucket, and signal-source metrics."""
    from trade_log import load_trades
    ledger.init_db()
    trades = load_trades(max_days=max(max_days, 365), limit=3000)
    attr = compute_attribution(trades, max_days=max_days)
    totals = attr.get("totals") or {}

    if not totals and not attr.get("closed_trades"):
        return _insufficient()

    closed = attr.get("closed_trades") or []
    wins = [t for t in closed if _float(t.get("realized_pl"), 0) > 0]
    losses = [t for t in closed if _float(t.get("realized_pl"), 0) <= 0]

    avg_winner = (
        sum(_float(t.get("realized_pl"), 0) for t in wins) / len(wins) if wins else None
    )
    avg_loser = (
        sum(_float(t.get("realized_pl"), 0) for t in losses) / len(losses) if losses else None
    )
    gross_win = sum(_float(t.get("realized_pl"), 0) for t in wins)
    gross_loss = abs(sum(_float(t.get("realized_pl"), 0) for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else None

    acct = ledger.get_latest_account_snapshot()
    equity = _float(acct.get("equity")) if acct else None
    cash = _float(acct.get("cash")) if acct else None
    cash_util = (1 - cash / equity) if equity and cash is not None and equity > 0 else None

    by_bucket = attr.get("by_bucket") or []
    by_source = ledger.get_source_accuracy_stats()

    reddit_levels = {"high": [], "low": []}
    for row in ledger.get_signal_outcomes_for_scorecard(limit=300):
        try:
            comps = json.loads(row.get("components_json") or "{}")
        except json.JSONDecodeError:
            comps = {}
        rs = _float(comps.get("reddit_sentiment"), 0)
        pct = _float(row.get("price_change_pct"), 0)
        bucket = "high" if rs >= 15 else "low"
        reddit_levels[bucket].append(pct)

    reddit_return = {}
    for level, vals in reddit_levels.items():
        if vals:
            reddit_return[f"reddit_{level}"] = round(sum(vals) / len(vals), 3)
        else:
            reddit_return[f"reddit_{level}"] = "insufficient data"

    latest_bt = ledger.get_latest_backtest_run()
    max_dd = _float(latest_bt.get("max_drawdown_pct")) if latest_bt else None

    return {
        "status": "ok",
        "period_days": max_days,
        "total_return_realized": totals.get("realized_pl"),
        "unrealized_pl": totals.get("unrealized_pl"),
        "total_pl": totals.get("total_pl"),
        "win_rate_pct": totals.get("win_rate"),
        "average_winner": round(avg_winner, 2) if avg_winner is not None else "insufficient data",
        "average_loser": round(avg_loser, 2) if avg_loser is not None else "insufficient data",
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else "insufficient data",
        "max_drawdown_pct": max_dd if max_dd is not None else "insufficient data",
        "cash_utilization_pct": round(cash_util * 100, 2) if cash_util is not None else "insufficient data",
        "return_by_bucket": by_bucket,
        "return_by_signal_source": by_source or "insufficient data",
        "return_by_reddit_level": reddit_return,
        "exposure_time_note": "insufficient data",
    }
