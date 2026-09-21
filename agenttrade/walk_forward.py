"""Walk-forward validation — report only, no live weight changes (Tier 4)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from agenttrade.backtest import run_backtest

log = logging.getLogger(__name__)


def run_walk_forward(train_days: int = 60, test_days: int = 30) -> dict:
    """
    Train period: evaluate signal scorecards via backtest.
    Test period: run backtest on next window.
    Reports recommendations only — does not modify live weights.
    """
    end = datetime.now(timezone.utc).date()
    test_start = end - timedelta(days=test_days)
    train_end = test_start - timedelta(days=1)
    train_start = train_end - timedelta(days=train_days)

    train_result = run_backtest(
        start_date=train_start.isoformat(),
        end_date=train_end.isoformat(),
        initial_cash=100000.0,
    )
    test_result = run_backtest(
        start_date=test_start.isoformat(),
        end_date=end.isoformat(),
        initial_cash=100000.0,
    )

    recommendations = []
    if train_result.get("ok") and test_result.get("ok"):
        train_ret = train_result.get("total_return_pct", 0)
        test_ret = test_result.get("total_return_pct", 0)
        if test_ret < train_ret * 0.5:
            recommendations.append(
                "Test period underperformed train — consider reviewing signal weights (manual only)"
            )
        if test_result.get("max_drawdown_pct", 0) > train_result.get("max_drawdown_pct", 0) * 1.5:
            recommendations.append("Drawdown increased in test window — risk controls validated")
    else:
        recommendations.append("insufficient data for full walk-forward comparison")

    return {
        "ok": train_result.get("ok") or test_result.get("ok"),
        "train_period": {"start": train_start.isoformat(), "end": train_end.isoformat(), "days": train_days},
        "test_period": {"start": test_start.isoformat(), "end": end.isoformat(), "days": test_days},
        "train_results": train_result,
        "test_results": test_result,
        "recommendations": recommendations,
        "live_weights_modified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward signal validation")
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_walk_forward(train_days=args.train_days, test_days=args.test_days)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("Walk-forward report (no live weight changes)")
        tr = result.get("train_results") or {}
        te = result.get("test_results") or {}
        print(f"  Train return: {tr.get('total_return_pct', 'insufficient data')}%")
        print(f"  Test return:  {te.get('total_return_pct', 'insufficient data')}%")
        for rec in result.get("recommendations") or []:
            print(f"  → {rec}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
