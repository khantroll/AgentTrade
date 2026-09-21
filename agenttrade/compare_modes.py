"""Paper vs live behavior comparison (Tier 4)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from agenttrade import db as ledger

log = logging.getLogger(__name__)


def compare_modes(days: int = 30) -> dict:
    """Compare paper vs live cycles, signals, fills, and safety blocks."""
    ledger.init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    paper_cycles = ledger.get_cycle_runs_by_mode("PAPER", limit=200)
    live_cycles = ledger.get_cycle_runs_by_mode("LIVE", limit=200)

    def _filter_recent(rows):
        return [r for r in rows if (r.get("started_at") or "") >= cutoff]

    paper_cycles = _filter_recent(paper_cycles)
    live_cycles = _filter_recent(live_cycles)

    paper_signals = 0
    live_signals = 0
    for c in paper_cycles:
        snaps = ledger.get_cycle_signals(c["id"]).get("signal_snapshots") or []
        paper_signals += len([s for s in snaps if str(s.get("decision", "")).upper() == "BUY"])
    for c in live_cycles:
        snaps = ledger.get_cycle_signals(c["id"]).get("signal_snapshots") or []
        live_signals += len([s for s in snaps if str(s.get("decision", "")).upper() == "BUY"])

    fills = ledger.get_realized_closed_trades(limit=500)
    paper_fills = [f for f in fills if (f.get("sell_time") or "") >= cutoff]  # mode not on fill; approximate

    safety = ledger.get_safety_block_counts(days=days)
    hypo = ledger.get_hypothetical_trades(limit=100)

    paper_equity = []
    live_equity = []
    with ledger.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT captured_at, equity, source FROM account_snapshots
            WHERE captured_at >= ? ORDER BY captured_at
            """,
            (cutoff,),
        ).fetchall()
    for r in rows:
        eq = float(r["equity"] or 0)
        if "paper" in str(r["source"]).lower():
            paper_equity.append(eq)
        else:
            live_equity.append(eq)

    def _return_series(series):
        if len(series) < 2:
            return None
        return round((series[-1] - series[0]) / series[0] * 100, 3) if series[0] else None

    paper_return = _return_series(paper_equity)
    live_return = _return_series(live_equity)

    return {
        "ok": True,
        "days": days,
        "paper_cycles": len(paper_cycles),
        "live_cycles": len(live_cycles),
        "paper_buy_signals": paper_signals,
        "live_buy_signals": live_signals,
        "signal_mismatch": abs(paper_signals - live_signals),
        "closed_trades_in_window": len(paper_fills),
        "missed_trades_note": "insufficient data" if not paper_fills else None,
        "blocked_trades": {
            "reconciliation": safety.get("reconciliation_failed", 0),
            "no_margin_guard": safety.get("order_rejected_no_margin", 0),
            "consecutive_loss_pause": safety.get("consecutive_loss_pause", 0),
            "position_size_rejected": safety.get("position_size_rejected", 0),
        },
        "shadow_hypothetical_trades": len(hypo),
        "paper_return_pct": paper_return if paper_return is not None else "insufficient data",
        "live_return_pct": live_return if live_return is not None else "insufficient data",
        "return_delta_pct": (
            round(live_return - paper_return, 3)
            if paper_return is not None and live_return is not None
            else "insufficient data"
        ),
        "divergence_warning": (
            abs(paper_signals - live_signals) > 5
            or (paper_return is not None and live_return is not None and abs(paper_return - live_return) > 5)
        ),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Compare paper vs live behavior")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    result = compare_modes(days=args.days)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
