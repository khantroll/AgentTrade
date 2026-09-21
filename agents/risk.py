"""Agent 3: Risk — position sizing and bucket guardrails."""

import logging

import agent_config as cfg
from agenttrade import db as ledger
from buckets import Bucket

log = logging.getLogger(__name__)


def _is_crypto_bucket(bucket: Bucket) -> bool:
    return getattr(bucket, "asset_class", "us_equity") == "crypto" or bucket.mode == "crypto"


def risk_agent(
    decisions: list,
    account: dict,
    positions: list,
    bucket: Bucket,
    rebalance_report: dict = None,
    buy_lock: dict = None,
    account_snapshot: dict = None,
) -> list:
    """Five-brake risk gate (bucket guardrails; executable sizing is Tier 2)."""
    log.info("[%s/Risk] %d decisions (aggression=%s)", bucket.name, len(decisions), cfg.STRATEGY_AGGRESSION)

    if buy_lock and buy_lock.get("active"):
        log.warning("[%s/Risk] BUY blocked: recent sell cooldown active", bucket.name)
        return []

    if ledger.trading_paused():
        log.warning(
            "[%s/Risk] BUY blocked: TRADING_PAUSED — %s",
            bucket.name,
            ledger.get_pause_reason() or "consecutive loss pause",
        )
        return []

    if not cfg.BUYING_ENABLED:
        log.warning("[%s/Risk] BUY blocked: BUYING_ENABLED=false", bucket.name)
        return []

    live_acct = (account_snapshot or {}).get("account") or account_snapshot or account
    portfolio_value = float(live_acct.get("portfolio_value") or live_acct.get("equity") or account.get("portfolio_value", 10000))
    raw_cash = float(live_acct.get("cash", account.get("cash", 0)))
    open_buy = float((account_snapshot or {}).get("open_buy_notional", 0))
    if raw_cash <= 0 and not cfg.ALLOW_MARGIN and not cfg.ALLOW_NEGATIVE_CASH:
        log.warning("[%s/Risk] Live cash $%.2f — blocking buys.", bucket.name, raw_cash)
        return []

    cash = max(raw_cash - open_buy - portfolio_value * cfg.RESERVE_CASH_PCT, 0)
    if cash < cfg.MIN_CASH_RESERVE and not cfg.ALLOW_NEGATIVE_CASH:
        log.warning(
            "[%s/Risk] Usable cash $%.2f below MIN_CASH_RESERVE $%.2f — blocking buys.",
            bucket.name, cash, cfg.MIN_CASH_RESERVE,
        )
        return []
    if cash < 10 and not cfg.ALLOW_NEGATIVE_CASH:
        log.warning("[%s/Risk] Usable cash $%.2f — skipping.", bucket.name, cash)
        return []

    if rebalance_report and bucket.name in rebalance_report:
        r = rebalance_report[bucket.name]
        ow = (r.get("current_pct", 0) - r.get("target_pct", bucket.allocation_pct * 100)) / 100
        if ow > cfg.MAX_BUCKET_OVERWEIGHT:
            log.warning("[%s/Risk] Overweight %.1f%% — blocking.", bucket.name, ow * 100)
            return []

    bucket_positions = cfg.bucket_manager.bucket_positions(bucket, positions)
    held_tickers = {p["symbol"] for p in bucket_positions}
    max_pos_dollars = cfg.bucket_manager.max_position_dollars(bucket, portfolio_value)

    approved = []
    buys_this_cycle = 0

    for d in decisions:
        ticker = d.get("ticker", "")
        action = str(d.get("action") or d.get("decision", "SKIP")).upper()
        price = float(d.get("current_price") or d.get("price") or 0)
        if action != "BUY" or price <= 0:
            continue

        if buys_this_cycle >= cfg.MAX_BUYS_PER_BUCKET:
            d["blocked_reason"] = "max_buys_per_cycle"
            continue

        if ticker in held_tickers:
            ex = next((p for p in bucket_positions if p["symbol"] == ticker), None)
            if ex and float(ex.get("market_value", 0)) >= max_pos_dollars * 0.90:
                d["blocked_reason"] = "position_near_max"
                continue

        if ticker not in held_tickers and len(bucket_positions) >= bucket.max_positions:
            d["blocked_reason"] = "bucket_full"
            continue

        # Stops set by Tier 2 (ATR); bucket defaults applied there as fallback only
        log.info("[%s/Risk] %s: APPROVED for deterministic sizing @ ~$%.2f", bucket.name, ticker, price)
        approved.append(d)
        buys_this_cycle += 1

    return approved
