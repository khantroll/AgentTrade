"""CSV export helpers for AgentTrade Reports.

Reconstructed from the preserved dashboard-side CSV fallback and config-server
routes so server exports match what the browser already produced.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone


def _csv(rows: list[list]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue()


def _pct(value, decimals=2) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{decimals}f}%"
    except (TypeError, ValueError):
        return ""


def _money(value) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def performance_summary_rows(period_summary: dict, max_days: int = 90) -> list[list]:
    ps = period_summary or {}
    bench = ps.get("benchmark") or {}
    max_dd = ""
    if ps.get("max_drawdown_pct") is not None:
        max_dd = _pct(ps.get("max_drawdown_pct"))
        if ps.get("max_drawdown") is not None:
            max_dd += f" (${float(ps['max_drawdown']):.2f})"

    pf = "" if ps.get("profit_factor") is None else str(ps.get("profit_factor"))
    if ps.get("profit_factor") is None and (ps.get("gross_profit") or 0) > 0 and not ps.get("losses"):
        pf = "inf"

    return [
        ["metric", "value"],
        ["Starting Value", _money(ps.get("starting_value"))],
        ["Ending Value", _money(ps.get("ending_value"))],
        ["S&P Return (same period)", _pct(bench.get("return_pct"))],
        ["Max Drawdown", max_dd],
        ["Number of Trades", str(ps.get("closed_trades") or 0)],
        ["Win Rate", _pct(ps.get("win_rate"), 1)],
        ["Average Winner", _money(ps.get("avg_winner"))],
        ["Average Loser", _money(ps.get("avg_loser"))],
        ["Profit Factor", pf],
        [],
        ["meta_start_date", ps.get("start_date") or ""],
        ["meta_end_date", ps.get("end_date") or ""],
        ["meta_range_days", str(max_days)],
        ["meta_fills", str(ps.get("fills") or 0)],
        ["meta_portfolio_return_pct", "" if ps.get("portfolio_return_pct") is None else f"{float(ps['portfolio_return_pct']):.2f}"],
        ["meta_alpha_vs_spy_pct", "" if ps.get("alpha_vs_benchmark_pct") is None else f"{float(ps['alpha_vs_benchmark_pct']):.2f}"],
        ["meta_benchmark_source", bench.get("source") or ""],
        ["meta_benchmark_fallback", "yes" if bench.get("fallback") else ""],
    ]


def performance_summary_csv(max_days: int = 90) -> str:
    from period_summary import compute_period_summary

    ps = compute_period_summary(max_days=max_days)
    return _csv(performance_summary_rows(ps, max_days=max_days))


def full_report_rows(max_days: int = 90) -> list[list]:
    from performance_history import get_history
    from trade_log import get_trades

    history = get_history(max_days=max_days)
    trade_data = get_trades(max_days=max_days)
    ps = history.get("period_summary") or {}
    attr = trade_data.get("attribution") or {}
    daily = history.get("daily") or []
    trades = trade_data.get("trades") or []

    rows = [
        ["AgentTrade Full Report"],
        ["exported_at", datetime.now(timezone.utc).isoformat()],
        ["range_days", str(max_days)],
        [],
        ["=== PERFORMANCE SUMMARY ==="],
        *performance_summary_rows(ps, max_days=max_days),
        [],
        ["=== DAILY PORTFOLIO ==="],
        ["date", "portfolio_value", "daily_pl", "change_from_prev", "cash", "positions_count", "daily_trades", "tokens", "token_cost_usd", "llm_mode"],
    ]
    for d in daily:
        rows.append([d.get(k) for k in ("date", "portfolio_value", "daily_pl", "change_from_prev", "cash", "positions_count", "daily_trades", "tokens", "token_cost_usd", "llm_mode")])

    rows += [[], ["=== TRADE LOG (FILLS) ==="], ["time", "date", "symbol", "side", "qty", "price", "notional", "bucket", "llm_mode", "tiered_source", "dual_status", "rationale"]]
    for t in trades:
        rows.append([t.get(k) for k in ("time", "date", "symbol", "side", "qty", "price", "notional", "bucket", "llm_mode", "tiered_source", "dual_status", "rationale")])

    rows += [[], ["=== P&L BY BUCKET ==="], ["bucket", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "wins", "losses", "win_rate", "open_positions"]]
    for item in attr.get("by_bucket") or []:
        rows.append([item.get(k) for k in ("bucket", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "wins", "losses", "win_rate", "open_positions")])

    rows += [[], ["=== P&L BY SYMBOL ==="], ["symbol", "bucket", "status", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "wins", "losses", "win_rate", "qty", "market_value"]]
    for item in attr.get("by_symbol") or []:
        rows.append([item.get(k) for k in ("symbol", "bucket", "status", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "wins", "losses", "win_rate", "qty", "market_value")])

    rows += [[], ["=== CLOSED TRADES (FIFO) ==="], ["symbol", "bucket", "qty", "buy_price", "sell_price", "realized_pl", "buy_time", "sell_time", "hold_days", "llm_mode", "llm_route", "primary_theme", "themes", "rationale"]]
    for c in attr.get("closed_trades") or []:
        themes = c.get("themes")
        if isinstance(themes, list):
            themes = "; ".join(str(x) for x in themes)
        rows.append([c.get("symbol"), c.get("bucket"), c.get("qty"), c.get("buy_price"), c.get("sell_price"), c.get("realized_pl"), c.get("buy_time"), c.get("sell_time"), c.get("hold_days"), c.get("llm_mode"), c.get("llm_route"), c.get("primary_theme"), themes or "", c.get("rationale")])

    blocks = [
        ("LLM BY MODE", "by_llm_mode", ["llm_mode", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "win_rate"]),
        ("LLM BY ROUTE", "by_route", ["llm_route", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "win_rate"]),
        ("LLM BY MODEL", "by_model", ["model", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "win_rate"]),
        ("P&L BY RATIONALE THEME", "by_theme", ["theme", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "win_rate", "symbols"]),
        ("P&L BY RATIONALE TAG (split)", "by_tag", ["theme", "realized_pl", "unrealized_pl", "total_pl", "closed_trades", "win_rate"]),
    ]
    for title, key, columns in blocks:
        data = attr.get(key) or []
        if not data:
            continue
        rows += [[], [f"=== {title} ==="], columns]
        for item in data:
            rows.append([item.get(c) for c in columns])

    return rows


def full_report_csv(max_days: int = 90) -> str:
    return _csv(full_report_rows(max_days=max_days))
