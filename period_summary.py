"""Period-level portfolio performance statistics and benchmark comparison.

Reconstructed during the 2026-08 AgentTrade recovery from the preserved unit
contract, dashboard schema, and call sites.  This module intentionally keeps a
small, stable API used by performance_history.py and backtest_engine.py.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests

from performance_history import load_cycles, rollup_daily


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date_str(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value or "")[:10]


def max_drawdown(values: Iterable[float]) -> dict:
    """Return absolute and percentage peak-to-trough drawdown.

    The absolute drawdown is reported as a positive loss amount. Percentage is
    relative to the running peak. Empty/all-invalid input returns None values.
    """
    clean = []
    for value in values or []:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        clean.append(v)

    if not clean:
        return {"max_drawdown": None, "max_drawdown_pct": None}

    peak = clean[0]
    worst_amt = 0.0
    worst_pct = 0.0
    for value in clean:
        if value > peak:
            peak = value
        amount = max(0.0, peak - value)
        pct = (amount / peak * 100.0) if peak else 0.0
        if pct > worst_pct or (pct == worst_pct and amount > worst_amt):
            worst_amt = amount
            worst_pct = pct

    return {
        "max_drawdown": round(worst_amt, 2),
        "max_drawdown_pct": round(worst_pct, 2),
    }


def trade_stats(closed_trades: list) -> dict:
    """Aggregate FIFO closed-trade rows for the selected period."""
    rows = [r for r in (closed_trades or []) if r.get("in_period", True)]
    pnls = [_float(r.get("realized_pl")) for r in rows]
    winners = [v for v in pnls if v >= 0]
    losers = [v for v in pnls if v < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    n = len(pnls)

    return {
        "closed_trades": n,
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": round(len(winners) / n * 100.0, 1) if n else None,
        "avg_winner": round(gross_profit / len(winners), 2) if winners else None,
        "avg_loser": round(sum(losers) / len(losers), 2) if losers else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (None if not gross_profit else None),
    }


def _benchmark_return(start_price, end_price, symbol: str, source: str, **extra) -> dict:
    start = _float(start_price)
    end = _float(end_price)
    if start <= 0 or end <= 0:
        return {"ok": False, "symbol": symbol, "source": source, "error": "invalid benchmark prices", **extra}
    ret = (end - start) / start * 100.0
    return {
        "ok": True,
        "symbol": symbol,
        "source": source,
        "start_price": round(start, 4),
        "end_price": round(end, 4),
        "return_pct": round(ret, 2),
        **extra,
    }


def _fetch_benchmark_yfinance(start_date: str, end_date: str, symbol: str = "SPY") -> dict:
    try:
        import yfinance as yf

        # yfinance's end date is exclusive; include the requested final day.
        end_exclusive = (datetime.fromisoformat(_date_str(end_date)) + timedelta(days=1)).date().isoformat()
        data = yf.download(
            symbol,
            start=_date_str(start_date),
            end=end_exclusive,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if data is None or len(data) == 0:
            return {"ok": False, "symbol": symbol, "source": "yfinance", "error": "no benchmark data"}
        close = data["Close"]
        # Handle both Series and single-symbol MultiIndex DataFrames.
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 1:
            return {"ok": False, "symbol": symbol, "source": "yfinance", "error": "no closing prices"}
        return _benchmark_return(float(close.iloc[0]), float(close.iloc[-1]), symbol, "yfinance", bars=int(len(close)))
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "source": "yfinance", "error": str(exc)}


def _load_config_env() -> None:
    try:
        from config_server import apply_config_to_env
        apply_config_to_env()
    except Exception:
        try:
            from dotenv import load_dotenv
            load_dotenv(override=False)
        except Exception:
            pass


def _fetch_benchmark_alpaca(start_date: str, end_date: str, symbol: str = "SPY") -> dict:
    try:
        _load_config_env()
        key = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            return {"ok": False, "symbol": symbol, "source": "alpaca", "error": "Alpaca API keys unavailable"}

        start = f"{_date_str(start_date)}T00:00:00Z"
        end = f"{_date_str(end_date)}T23:59:59Z"
        response = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            params={"timeframe": "1Day", "start": start, "end": end, "limit": 1000, "adjustment": "all"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=30,
        )
        response.raise_for_status()
        bars = response.json().get("bars") or []
        if not bars:
            return {"ok": False, "symbol": symbol, "source": "alpaca", "error": "no benchmark bars"}
        return _benchmark_return(bars[0].get("c"), bars[-1].get("c"), symbol, "alpaca", bars=len(bars))
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "source": "alpaca", "error": str(exc)}


def fetch_benchmark_return(start_date: str, end_date: str, symbol: str = "SPY") -> dict:
    """Fetch benchmark return, preferring yfinance and falling back to Alpaca."""
    primary = _fetch_benchmark_yfinance(start_date, end_date, symbol)
    if primary.get("ok"):
        return primary

    fallback = _fetch_benchmark_alpaca(start_date, end_date, symbol)
    if fallback.get("ok"):
        out = dict(fallback)
        out["fallback"] = True
        out["primary_error"] = primary.get("error")
        return out

    return {
        "ok": False,
        "symbol": symbol,
        "source": fallback.get("source") or "alpaca",
        "primary_error": primary.get("error"),
        "fallback_error": fallback.get("error"),
        "error": fallback.get("error") or primary.get("error") or "benchmark unavailable",
    }


def compute_period_summary(max_days: int = 90) -> dict:
    """Compute dashboard period summary from portfolio history and FIFO fills."""
    max_days = max(1, int(max_days or 90))
    cycles = load_cycles(max_days=max_days)
    if not cycles:
        return {"ok": False, "max_days": max_days, "message": "No portfolio history available for this period."}

    daily = rollup_daily(cycles)
    daily = [d for d in daily if d.get("portfolio_value") is not None]
    if not daily:
        return {"ok": False, "max_days": max_days, "message": "No portfolio history values available for this period."}

    start = daily[0]
    end = daily[-1]
    start_value = _float(start.get("portfolio_value"))
    end_value = _float(end.get("portfolio_value"))
    portfolio_return = ((end_value - start_value) / start_value * 100.0) if start_value else None

    benchmark = fetch_benchmark_return(start.get("date"), end.get("date"), "SPY")
    alpha = None
    if portfolio_return is not None and benchmark.get("ok") and benchmark.get("return_pct") is not None:
        alpha = round(portfolio_return - _float(benchmark.get("return_pct")), 2)

    dd = max_drawdown([d.get("portfolio_value") for d in daily])

    # FIFO must receive enough history to reconstruct lots bought before this reporting window.
    try:
        from trade_log import load_trades, summarize_trades
        from pnl_attribution import fifo_realized

        trades = load_trades(max_days=max(max_days, 365), limit=3000)
        closed, _ = fifo_realized(trades, max_days=max_days)
        stats = trade_stats(closed)
        fills = int((summarize_trades(load_trades(max_days=max_days, limit=3000)) or {}).get("total") or 0)
    except Exception:
        stats = trade_stats([])
        fills = 0

    return {
        "ok": True,
        "max_days": max_days,
        "start_date": start.get("date"),
        "end_date": end.get("date"),
        "starting_value": round(start_value, 2),
        "ending_value": round(end_value, 2),
        "portfolio_return_pct": round(portfolio_return, 2) if portfolio_return is not None else None,
        "benchmark": benchmark,
        "alpha_vs_benchmark_pct": alpha,
        **dd,
        **stats,
        "fills": fills,
    }
