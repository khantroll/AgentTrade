"""Agent 3: Risk — position sizing guardrails and cycle-level entry blocks.

Executable share/notional sizing stays in Tier 2 (``agenttrade.risk``). This
gate decides whether a BUY may be sized at all.

Add rule (paper default): leftover room under the position cap is not
permission to buy more of a name that is already held.

- Held symbols are global (the whole portfolio plus names already approved
  earlier in this cycle). A different bucket is ``cross_bucket_duplicate``.
  An untagged holding is ``already_held``.
- Same-bucket adds are off unless ``ALLOW_POSITION_ADDS`` is true.
  Unrealized P&L that is not positive is ``average_down_blocked``.
  A winner with adds disabled is ``add_not_allowed``.
- ``ALLOW_AVERAGE_DOWN`` (default false) is the only switch that permits
  adding to a loser, and only when adds are also enabled.
- A position at or above 90% of its bucket dollar cap is still
  ``position_near_max``.

Cash: each approval reserves its quoted notional on the cycle ledger. The
next approval sees the reduced balance. Drawdown (``MAX_ACCOUNT_DRAWDOWN_PCT``,
default 0.10) and invested capital (``MAX_INVESTED_PCT``, default 0.90) block
new buys while the portfolio is already stressed. A value <= 0 disables that
guard. Protective sells are not decided here.
"""

import logging

import agent_config as cfg
from account_sync import reserved_open_buy_notional
from agenttrade import db as ledger
from agenttrade.risk import CycleRiskState, quote_buy_notional, _is_cash_block
from buckets import Bucket

log = logging.getLogger(__name__)


def _is_crypto_bucket(bucket: Bucket) -> bool:
    return getattr(bucket, "asset_class", "us_equity") == "crypto" or bucket.mode == "crypto"


def _stamp_buys(decisions: list, reason: str) -> None:
    for d in decisions or []:
        action = str(d.get("action") or d.get("decision") or "SKIP").upper()
        if action == "BUY":
            d["blocked_reason"] = reason


def _unrealized_pl(position: dict) -> float:
    if not position:
        return 0.0
    raw = position.get("unrealized_pl")
    if raw is None or raw == "":
        raw = position.get("unrealized_plpc")
    if raw is None or raw == "":
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _held_block_reason(bucket: Bucket, position: dict, max_pos_dollars: float, owner) -> str:
    """Block reason for a symbol that is already held, or empty if an add is allowed."""
    if owner is None:
        return ""
    if owner == "":
        return "already_held"
    if owner != bucket.name:
        return "cross_bucket_duplicate"
    if position is None:
        return "already_held"
    pl = _unrealized_pl(position)
    if not cfg.ALLOW_POSITION_ADDS:
        if pl <= 0:
            return "average_down_blocked"
        return "add_not_allowed"
    if pl <= 0 and not cfg.ALLOW_AVERAGE_DOWN:
        return "average_down_blocked"
    try:
        market_value = float(position.get("market_value") or 0)
    except (TypeError, ValueError):
        market_value = 0.0
    if max_pos_dollars > 0 and market_value >= max_pos_dollars * 0.90:
        return "position_near_max"
    return ""


def _base_snapshot(account: dict, account_snapshot: dict) -> dict:
    if isinstance(account_snapshot, dict):
        return account_snapshot
    return {"account": account if isinstance(account, dict) else {}}


def risk_agent(
    decisions: list,
    account: dict,
    positions: list,
    bucket: Bucket,
    rebalance_report: dict = None,
    buy_lock: dict = None,
    account_snapshot: dict = None,
    cycle_state: CycleRiskState = None,
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
    open_buy = reserved_open_buy_notional(account_snapshot)
    foreign_open_buy = float((account_snapshot or {}).get("foreign_open_buy_notional") or 0)
    if raw_cash <= 0 and not cfg.ALLOW_MARGIN and not cfg.ALLOW_NEGATIVE_CASH:
        log.warning("[%s/Risk] Live cash $%.2f — blocking buys.", bucket.name, raw_cash)
        return []

    reserve_dollars = portfolio_value * cfg.RESERVE_CASH_PCT
    cash = max(raw_cash - open_buy - reserve_dollars, 0)
    if cash < cfg.MIN_CASH_RESERVE and not cfg.ALLOW_NEGATIVE_CASH:
        log.warning(
            "[%s/Risk] Usable cash $%.2f below MIN_CASH_RESERVE $%.2f "
            "(cash $%.2f, AgentTrade open buys $%.2f, %.0f%% reserve $%.2f, "
            "foreign open buys $%.2f left on the shared account).",
            bucket.name, cash, cfg.MIN_CASH_RESERVE,
            raw_cash, open_buy, cfg.RESERVE_CASH_PCT * 100, reserve_dollars, foreign_open_buy,
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

    base_snapshot = _base_snapshot(account, account_snapshot)
    if cycle_state is None:
        cycle_state = CycleRiskState.from_snapshot(base_snapshot, positions)

    drawdown = cycle_state.drawdown_reason()
    if drawdown:
        log.warning("[%s/Risk] %s — blocking new buys.", bucket.name, drawdown)
        _stamp_buys(decisions, drawdown)
        return []

    already_invested = cycle_state.long_market_value + cycle_state.confirmed_reserved()
    invested_block = cycle_state.invested_reason(already_invested)
    if invested_block:
        log.warning(
            "[%s/Risk] max invested (long $%.2f + reserved $%.2f, equity $%.2f) — blocking new buys.",
            bucket.name,
            cycle_state.long_market_value,
            cycle_state.confirmed_reserved(),
            cycle_state.equity,
        )
        _stamp_buys(decisions, invested_block)
        return []

    bucket_positions = cfg.bucket_manager.bucket_positions(bucket, positions)
    bucket_held = {str(p.get("symbol") or "").upper() for p in bucket_positions}
    max_pos_dollars = cfg.bucket_manager.max_position_dollars(bucket, portfolio_value)

    approved = []
    buys_this_cycle = 0

    for d in decisions:
        ticker = str(d.get("ticker") or "")
        ticker_key = ticker.upper()
        action = str(d.get("action") or d.get("decision", "SKIP")).upper()
        price = float(d.get("current_price") or d.get("price") or 0)
        if action != "BUY" or price <= 0:
            continue

        if buys_this_cycle >= cfg.MAX_BUYS_PER_BUCKET:
            d["blocked_reason"] = "max_buys_per_cycle"
            continue

        owner = cycle_state.holding_bucket(ticker_key)
        if owner is not None:
            position = cycle_state.position_for(ticker_key)
            if position is None:
                position = next(
                    (p for p in positions or [] if str(p.get("symbol") or "").upper() == ticker_key),
                    None,
                )
            held_reason = _held_block_reason(bucket, position, max_pos_dollars, owner)
            if held_reason:
                d["blocked_reason"] = held_reason
                continue

        if ticker_key not in bucket_held and len(bucket_positions) >= bucket.max_positions:
            d["blocked_reason"] = "bucket_full"
            continue

        quote_ok, quote_reason, notional = quote_buy_notional(
            d, cycle_state.snapshot_for(ticker_key, base_snapshot), bucket,
        )
        if quote_ok and notional > 0:
            projected = (
                cycle_state.long_market_value
                + cycle_state.reserved_excluding(ticker_key)
                + notional
            )
            invest_reason = cycle_state.invested_reason(projected)
            if invest_reason:
                d["blocked_reason"] = invest_reason
                continue
            if not cycle_state.commit(ticker_key, notional):
                d["blocked_reason"] = "insufficient_cash"
                continue
        elif cycle_state.reserved_total() > 0.01 and _is_cash_block(quote_reason):
            d["blocked_reason"] = "insufficient_cash"
            continue

        # Stops set by Tier 2 (ATR); bucket defaults applied there as fallback only
        log.info("[%s/Risk] %s: APPROVED for deterministic sizing @ ~$%.2f", bucket.name, ticker, price)
        approved.append(d)
        cycle_state.note_pending(ticker_key, bucket.name)
        buys_this_cycle += 1

    return approved
