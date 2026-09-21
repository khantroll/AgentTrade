"""
Read-only SQLite signal backtest (Tier 4).

Never submits Alpaca orders.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from agenttrade import db as ledger
from agenttrade.indicators import calculate_atr_stop
from agenttrade.risk import calculate_position_size

log = logging.getLogger(__name__)

MAX_HOLD_DAYS = 30


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_date(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _fetch_prices(symbol: str, start: datetime, end: datetime) -> dict[str, float]:
    """date_str YYYY-MM-DD -> close price."""
    out: dict[str, float] = {}
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(
            start=start.date(),
            end=(end + timedelta(days=1)).date(),
        )
        if hist.empty:
            return out
        for idx, row in hist.iterrows():
            d = idx.strftime("%Y-%m-%d")
            out[d] = float(row["Close"])
    except Exception as e:
        log.warning("[Backtest] Missing price data for %s: %s", symbol, e)
    return out


def _simulate_trade(
    snap: dict,
    cash: float,
    equity: float,
    prices: dict[str, float],
    end_date: str,
    max_risk_pct: float,
    use_atr_stops: bool,
    allow_margin: bool,
) -> tuple[Optional[dict], float, list[str]]:
    """Returns (trade_dict, cash_after, skip_reasons)."""
    sym = str(snap.get("symbol") or "").upper()
    entry_ts = snap.get("created_at") or ""
    entry_day = entry_ts[:10]
    entry_price = _float(snap.get("price"))
    if entry_price <= 0:
        entry_price = prices.get(entry_day, 0)
    if entry_price <= 0:
        return None, cash, [f"{sym}@{entry_day}: no entry price"]

    components = snap.get("components") or {}
    if isinstance(components, str):
        try:
            components = json.loads(components)
        except json.JSONDecodeError:
            components = {}

    atr = 0.0
    ohlcv = {}
    try:
        raw = snap.get("raw_json")
        extra = json.loads(raw) if isinstance(raw, str) else (raw or {})
        atr = _float(extra.get("atr"))
        ohlcv = extra.get("ohlcv") or {}
    except (json.JSONDecodeError, TypeError):
        pass

    if use_atr_stops:
        from agenttrade.indicators import calculate_atr
        if not atr and ohlcv:
            atr = calculate_atr(ohlcv)
        stop_res = calculate_atr_stop(side="buy", entry_price=entry_price, atr=atr, fallback_stop_pct=0.07)
        stop_price = _float(stop_res.get("stop_price"))
    else:
        stop_price = round(entry_price * 0.93, 4)

    if stop_price <= 0 or stop_price >= entry_price:
        return None, cash, [f"{sym}: invalid stop"]

    available = cash if not allow_margin else equity
    size = calculate_position_size(
        symbol=sym,
        account_equity=equity,
        available_cash=available,
        entry_price=entry_price,
        stop_price=stop_price,
        max_risk_pct=max_risk_pct,
        allow_fractional=False,
    )
    if not size.get("approved"):
        return None, cash, [f"{sym}: {size.get('reason', 'sizing rejected')}"]

    qty = int(size["qty"])
    cost = qty * entry_price
    if not allow_margin and cost > cash + 0.01:
        return None, cash, [f"{sym}: insufficient cash (no margin)"]

    tp_price = round(entry_price * 1.15, 4)
    sorted_days = sorted(d for d in prices.keys() if d >= entry_day)
    exit_day = None
    exit_price = None
    exit_reason = "end_of_data"
    max_hold = _parse_date(end_date)

    for day in sorted_days:
        if day == entry_day:
            continue
        px = prices[day]
        if px <= stop_price:
            exit_day, exit_price, exit_reason = day, px, "stop_loss"
            break
        if px >= tp_price:
            exit_day, exit_price, exit_reason = day, px, "take_profit"
            break
        if _parse_date(day + "T12:00:00+00:00") - _parse_date(entry_ts) > timedelta(days=MAX_HOLD_DAYS):
            exit_day, exit_price, exit_reason = day, px, "max_hold"
            break
        if day > end_date[:10]:
            exit_day, exit_price, exit_reason = day, px, "backtest_end"
            break

    if exit_price is None and sorted_days:
        exit_day = sorted_days[-1]
        exit_price = prices[exit_day]
        exit_reason = "last_price"

    if exit_price is None:
        return None, cash, [f"{sym}: no exit price in range"]

    pnl = (exit_price - entry_price) * qty
    pnl_pct = (exit_price - entry_price) / entry_price * 100
    cash_after = cash - cost + exit_price * qty

    trade = {
        "symbol": sym,
        "entry_date": entry_day,
        "exit_date": exit_day,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "qty": qty,
        "stop_price": stop_price,
        "take_profit_price": tp_price,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 3),
        "exit_reason": exit_reason,
        "signal_score": _float(snap.get("total_score")),
        "reddit_score": _float(components.get("reddit_sentiment")),
        "confidence": _float(snap.get("calibrated_confidence") or snap.get("raw_confidence")),
    }
    return trade, cash_after, []


def run_backtest(
    *,
    strategy_name: str | None = None,
    start_date: str,
    end_date: str,
    initial_cash: float = 100000.0,
    max_risk_pct: float = 0.005,
    use_atr_stops: bool = True,
    allow_margin: bool = False,
) -> dict:
    """
    Replay BUY signal_snapshots from SQLite. Read-only — never calls Alpaca.
    """
    ledger.init_db()
    run_id = ledger.start_backtest_run(strategy_name, start_date, end_date, initial_cash)
    signals = ledger.get_signal_snapshots_in_range(start_date, end_date, decision="BUY")
    if not signals:
        metrics = {
            "ok": False,
            "run_id": run_id,
            "message": "insufficient data: no BUY signals in range",
            "signals_found": 0,
            "trades": 0,
            "skipped": [],
        }
        ledger.finish_backtest_run(run_id, metrics)
        return metrics

    cash = initial_cash
    equity = initial_cash
    trades: list[dict] = []
    skipped: list[str] = []
    price_cache: dict[str, dict[str, float]] = {}

    start_dt = _parse_date(start_date + "T00:00:00+00:00")
    end_dt = _parse_date(end_date + "T23:59:59+00:00")

    for snap in signals:
        sym = str(snap.get("symbol") or "").upper()
        if sym not in price_cache:
            price_cache[sym] = _fetch_prices(sym, start_dt, end_dt + timedelta(days=MAX_HOLD_DAYS + 5))
            if not price_cache[sym]:
                skipped.append(f"{sym}: missing price history")
                continue

        trade, cash, reasons = _simulate_trade(
            snap, cash, equity, price_cache[sym], end_date,
            max_risk_pct, use_atr_stops, allow_margin,
        )
        skipped.extend(reasons)
        if trade:
            ledger.insert_backtest_trade(run_id, trade)
            trades.append(trade)
            equity = cash + sum(
                t["qty"] * t["entry_price"] for t in trades if not t.get("exit_date")
            )  # simplified mark-to-market
            equity = cash  # flat after each round-trip

    if not trades:
        metrics = {
            "ok": False,
            "run_id": run_id,
            "message": "insufficient data: no simulated trades",
            "signals_found": len(signals),
            "trades": 0,
            "skipped": skipped,
        }
        ledger.finish_backtest_run(run_id, metrics)
        return metrics

    final_equity = initial_cash + sum(_float(t.get("pnl")) for t in trades)
    wins = [t for t in trades if _float(t.get("pnl")) > 0]
    losses = [t for t in trades if _float(t.get("pnl")) <= 0]
    gross_win = sum(_float(t.get("pnl")) for t in wins)
    gross_loss = abs(sum(_float(t.get("pnl")) for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else gross_win or 0

    equity_curve = [initial_cash]
    running = initial_cash
    for t in trades:
        running += _float(t.get("pnl"))
        equity_curve.append(running)
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    returns = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
    sharpe_like = 0.0
    if returns:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std = var ** 0.5
        sharpe_like = (mean_r / std * (252 ** 0.5)) if std > 0 else 0.0

    total_return_pct = (final_equity - initial_cash) / initial_cash * 100
    metrics = {
        "ok": True,
        "run_id": run_id,
        "strategy_name": strategy_name,
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": initial_cash,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 3),
        "max_drawdown_pct": round(max_dd * 100, 3),
        "win_rate": round(100 * len(wins) / len(trades), 2),
        "profit_factor": round(profit_factor, 3),
        "sharpe_like": round(sharpe_like, 3),
        "signals_found": len(signals),
        "trades": len(trades),
        "skipped": skipped,
        "notes": f"SQLite signal replay; {len(skipped)} skips logged",
    }
    ledger.finish_backtest_run(run_id, metrics)
    log.info(
        "[Backtest] Run %s complete: %d trades, return %.2f%%",
        run_id, len(trades), total_return_pct,
    )
    return metrics


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run SQLite signal backtest (no live orders)")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--cash", type=float, default=100000.0)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--no-atr", action="store_true")
    parser.add_argument("--margin", action="store_true")
    args = parser.parse_args(argv)
    result = run_backtest(
        strategy_name=args.strategy,
        start_date=args.start,
        end_date=args.end,
        initial_cash=args.cash,
        use_atr_stops=not args.no_atr,
        allow_margin=args.margin,
    )
    import json as _json
    print(_json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
