"""
backtest_engine.py — Orchestrate historical replay of screener → decisions → risk → sim fills.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional

from backtest_data import PriceHistory, candidate_pool, universe_as_of
from backtest_decisions import rule_analysis_agent, rule_research_agent, scale_decision_shares
from backtest_simulator import BacktestPortfolio
from buckets import Bucket, BucketManager
from period_summary import fetch_benchmark_return, max_drawdown

log = logging.getLogger(__name__)


def _is_crypto_bucket(bucket: Bucket) -> bool:
    return getattr(bucket, "asset_class", "us_equity") == "crypto" or bucket.mode == "crypto"


def _cycle_days(trading_days: List[str], interval: int) -> set:
    """Every N trading days (default 5 ≈ weekly)."""
    if interval <= 1:
        return set(trading_days)
    return {trading_days[i] for i in range(0, len(trading_days), interval)}


def run_backtest(
    start_date: str,
    end_date: str,
    initial_capital: float = 10_000.0,
    cycle_interval: int = 5,
    universe_limit: int = 50,
    buckets: Optional[List[Bucket]] = None,
    prices: Optional[PriceHistory] = None,
    use_llm: bool = False,
    _cfg_override=None,
    _risk_fn=None,
) -> dict:
    """
    Replay screener + rule-based decisions against historical prices.

    use_llm=False (default): fast momentum + heuristic analysis + live risk_agent.
    use_llm=True: not yet implemented — reserved for future LLM replay mode.
    """
    if use_llm:
        return {"ok": False, "message": "LLM backtest mode not implemented — use rule-based (default)"}

    if _cfg_override is not None:
        cfg = _cfg_override
    else:
        import agent_config as cfg
        cfg.refresh_config()

    if _risk_fn is not None:
        risk_agent = _risk_fn
    else:
        from agents.risk import risk_agent
    bm = buckets if buckets is not None else BucketManager().active_buckets()
    bm = [b for b in bm if b.enabled and not _is_crypto_bucket(b)]
    if not bm:
        return {"ok": False, "message": "No enabled equity buckets for backtest"}

    if prices is None:
        symbols = set()
        for b in bm:
            symbols.update(candidate_pool(b.mode, limit=universe_limit))
        symbols = sorted(symbols)
        log.info("[Backtest] Downloading %d symbols %s → %s", len(symbols), start_date, end_date)
        prices = PriceHistory.from_yfinance(symbols, start_date, end_date)

    days = prices.trading_days(start_date, end_date)
    if len(days) < 10:
        return {"ok": False, "message": "Insufficient trading days in range"}

    cycle_set = _cycle_days(days, cycle_interval)
    portfolio = BacktestPortfolio(initial_capital)
    cycle_log = []

    for day in days:
        portfolio.check_exits(prices, day, max_daily_trades=cfg.MAX_DAILY_TRADES)
        portfolio.record_equity(prices, day)

        if day not in cycle_set:
            continue

        account = portfolio.to_account(prices, day)
        positions = portfolio.to_alpaca_positions(prices, day)
        pv = float(account["portfolio_value"])

        rebalance = cfg.bucket_manager.rebalance_report(positions, pv)
        ordered = cfg.bucket_manager.prioritize_buckets(rebalance)

        day_cycles = []

        for bucket in ordered:
            universe, sources = universe_as_of(
                prices, bucket.screener_config, day, max_universe=15,
            )
            if not universe:
                continue

            scored = prices.momentum_universe(
                universe, day,
                min_price=bucket.min_price,
                max_price=bucket.max_price,
                top_n=15,
            )
            metrics_fn = lambda t, d=day: prices.metrics_as_of(t, d)

            candidates = rule_research_agent(scored, bucket, metrics_fn, top_n=3)
            if not candidates:
                continue

            decisions = rule_analysis_agent(candidates, bucket, metrics_fn)
            scale_decision_shares(
                [d for d in decisions if d.get("action") == "BUY"],
                pv, bucket,
            )

            approved = risk_agent(decisions, account, positions, bucket, rebalance)
            buys = 0
            for dec in approved:
                trade = portfolio.buy(dec, prices, day, cfg.MAX_DAILY_TRADES)
                if trade:
                    buys += 1
                    account = portfolio.to_account(prices, day)
                    positions = portfolio.to_alpaca_positions(prices, day)

            day_cycles.append({
                "bucket": bucket.name,
                "universe_size": len(universe),
                "candidates": len(candidates),
                "decisions": len(decisions),
                "approved": len(approved),
                "buys": buys,
                "sources": sources,
            })

        if day_cycles:
            cycle_log.append({"date": day, "portfolio_value": pv, "buckets": day_cycles})

    start_val = portfolio.equity_curve[0]["portfolio_value"] if portfolio.equity_curve else initial_capital
    end_val = portfolio.equity_curve[-1]["portfolio_value"] if portfolio.equity_curve else initial_capital
    port_return = ((end_val - start_val) / start_val * 100) if start_val else None

    pvs = [e["portfolio_value"] for e in portfolio.equity_curve]
    dd = max_drawdown(pvs)
    bench = fetch_benchmark_return(start_date, end_date)
    alpha = None
    if port_return is not None and bench.get("ok"):
        alpha = round(port_return - bench["return_pct"], 2)

    stats = portfolio.summary_stats()

    return {
        "ok": True,
        "mode": "rule_based",
        "start_date": days[0],
        "end_date": days[-1],
        "trading_days": len(days),
        "cycle_interval": cycle_interval,
        "cycles_run": len(cycle_log),
        "initial_capital": round(initial_capital, 2),
        "starting_value": round(start_val, 2),
        "ending_value": round(end_val, 2),
        "portfolio_return_pct": round(port_return, 2) if port_return is not None else None,
        "benchmark": bench,
        "alpha_vs_benchmark_pct": alpha,
        "max_drawdown": dd["max_drawdown"],
        "max_drawdown_pct": dd["max_drawdown_pct"],
        **stats,
        "equity_curve": portfolio.equity_curve,
        "trades": [
            {
                "date": t.date,
                "symbol": t.symbol,
                "side": t.side,
                "qty": t.qty,
                "price": round(t.price, 4),
                "bucket": t.bucket,
                "rationale": t.rationale,
                "pnl": t.pnl,
            }
            for t in portfolio.trades
        ],
        "cycle_log": cycle_log[-20:],
    }


def default_date_range(days: int = 90) -> tuple:
    end = date.today()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()
