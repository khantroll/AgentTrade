"""Shared America/Chicago entry count for the banner, strip, and trader.

Read-only. ``python -m agenttrade.daily_trades --explain`` prints every
counted and excluded order and does not rewrite fills or orders.

The count is AgentTrade entries only: orders this process submitted
(strategy name on the ledger row) or stamped with an ``agenttrade-``
client order id. Partial fills collapse onto the broker order id, even
when an older fill row stored the Alpaca activity id. Sells, stops, and
take-profit legs do not count. Stored open orders outside today, and
orders whose live Alpaca status is terminal, do not count.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def _shape_and_tag(orders: list, origin: str) -> list:
    from trading_day import shape_order_for_count

    tagged = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        shaped = shape_order_for_count(order)
        if not shaped:
            continue
        shaped["_count_origin"] = origin
        tagged.append(shaped)
    return tagged


def _ledger_orders_today(orders: list, now) -> list:
    """Drop stored open orders that were not submitted on this Chicago day.

    A stale snapshot of hundreds of old rows must not enter the count.
    Today's take-profit legs are kept so the diagnostic can show why they
    were excluded. Live ``state.open_orders`` are filtered later, so ghosts
    from another day are still listed as excluded.
    """
    from trading_day import parse_timestamp, trading_day_bounds

    start, end = trading_day_bounds(now)
    kept = []
    for order in orders or []:
        moment = parse_timestamp(order.get("submitted_at") or order.get("created_at") or "")
        if moment is not None and start <= moment < end:
            kept.append(order)
    return kept


def current_daily_trade_report(
    now=None,
    *,
    extra_fills: Optional[list] = None,
    extra_orders: Optional[list] = None,
) -> dict:
    """The one count used by the health banner, dashboard strip, and cycle."""
    from trading_day import explain_trades_for_day

    fills: list = []
    orders: list = []
    owned: set[str] = set()
    try:
        from agenttrade import db as ledger

        ledger.init_db()
        fills.extend(ledger.fills_in_trading_day(now))
        stored = _shape_and_tag(ledger.get_latest_open_orders(), "ledger")
        orders.extend(_ledger_orders_today(stored, now))
        owned = ledger.agent_submitted_order_ids()
    except Exception:
        log.exception("[DailyTrades] Ledger count unavailable; using the rows passed in")
    for fill in extra_fills or []:
        if not isinstance(fill, dict):
            continue
        tagged = dict(fill)
        tagged.setdefault("_count_origin", "state")
        fills.append(tagged)
    orders.extend(_shape_and_tag(extra_orders or [], "live"))
    return explain_trades_for_day(fills, orders, now=now, owned_order_ids=owned)


def current_daily_trade_count(
    now=None,
    *,
    extra_fills: Optional[list] = None,
    extra_orders: Optional[list] = None,
) -> int:
    return int(current_daily_trade_report(
        now,
        extra_fills=extra_fills,
        extra_orders=extra_orders,
    )["count"])


def _state_orders_and_fills() -> tuple[list, list]:
    """Recent fills and open orders from the dashboard projection, if present."""
    try:
        import agent_config as cfg
        path = cfg.STATE_FILE
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError, AttributeError):
        return [], []
    if not isinstance(state, dict):
        return [], []
    fills = state.get("recent_fills") if isinstance(state.get("recent_fills"), list) else []
    orders = state.get("open_orders") if isinstance(state.get("open_orders"), list) else []
    return fills, orders


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print AgentTrade's America/Chicago daily entry count. Read-only.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print each counted and excluded order, including activity-id merges.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO timestamp used as 'now' (default: current time).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to agenttrade.sqlite3 (default: AGENTTRADE_DB_PATH or app dir).",
    )
    args = parser.parse_args(argv)
    if args.db:
        os.environ["AGENTTRADE_DB_PATH"] = args.db
        from agenttrade import db as ledger
        ledger._DB_INITIALIZED = False

    fills, orders = _state_orders_and_fills()
    report = current_daily_trade_report(args.now, extra_fills=fills, extra_orders=orders)
    limit = None
    try:
        import agent_config as cfg
        limit = int(cfg.current_max_daily_trades())
    except (TypeError, ValueError, ImportError):
        limit = None
    if args.explain:
        from trading_day import format_trade_report
        print(format_trade_report(report, limit=limit))
    else:
        cap = f"/{limit}" if limit is not None else ""
        print(f"{report['count']}{cap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
