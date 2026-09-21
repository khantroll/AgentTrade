"""Agent 4: Execution — Alpaca orders and hard rebalance sells."""

import logging
from datetime import datetime

import agent_config as cfg
from alpaca_client import alpaca_post
from buckets import Bucket
from buy_guard import check_buy_allowed
from market_data import is_crypto_bucket
from order_utils import format_qty_for_asset

log = logging.getLogger(__name__)


def execution_agent(approved: list, bucket: Bucket, buy_lock: dict = None, account_snapshot: dict = None) -> list:
    results = []

    if buy_lock and buy_lock.get("active"):
        log.warning("[Execution] BUY blocked: recent sell cooldown active")
        return []

    if not cfg.BUYING_ENABLED:
        log.warning("[Execution] BUY blocked: BUYING_ENABLED=false")
        return []

    snapshot = account_snapshot

    for d in approved:
        if cfg.daily_trades >= cfg.MAX_DAILY_TRADES:
            log.warning("[Execution] Daily trade limit reached.")
            break

        ticker = d["ticker"]
        is_crypto = is_crypto_bucket(bucket)
        shares = d.get("shares")
        notional = d.get("notional_usd")

        try:
            _hm = datetime.now().hour * 60 + datetime.now().minute
            _near = (570 <= _hm <= 585) or (945 <= _hm <= 960)
            _atr_pct = float(d.get("atr_pct") or 0.5)
            _p = float(d.get("current_price") or d.get("price") or 0)

            if is_crypto:
                payload = {
                    "symbol": ticker,
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "gtc",
                    "notional": str(round(float(notional), 2)),
                }
                _ot = "market(crypto)"
            elif _atr_pct > 3.0 or _near or not _p:
                payload = {
                    "symbol": ticker,
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                }
                _ot = "market"
            else:
                _lp = round(_p + max(_p * (_atr_pct / 100) * 0.5, 0.01), 2)
                payload = {
                    "symbol": ticker,
                    "side": "buy",
                    "type": "limit",
                    "limit_price": str(_lp),
                    "time_in_force": "day",
                }
                _ot = f"limit@{_lp}"

            if is_crypto:
                if not notional:
                    log.warning("[%s/Execution] %s: no notional for crypto order — skipping", bucket.name, ticker)
                    continue
            elif not shares:
                log.warning("[%s/Execution] %s: no shares for stock order — skipping", bucket.name, ticker)
                continue
            else:
                payload["qty"] = str(shares)

            order_notional = float(notional or 0) if is_crypto else float(shares or 0) * float(_p or d.get("current_price") or 0)
            allowed, block_reason, cash_ctx = check_buy_allowed(order_notional, snapshot=snapshot, buy_lock=buy_lock)
            if not allowed:
                log.warning(
                    "[Execution] BUY blocked: %s — cash=%s buying_power=%s order_notional=%s projected_cash=%s",
                    block_reason,
                    cash_ctx.get("cash"),
                    cash_ctx.get("buying_power"),
                    cash_ctx.get("order_notional"),
                    cash_ctx.get("projected_cash"),
                )
                results.append({
                    "ticker": ticker,
                    "shares": shares,
                    "bucket": bucket.name,
                    "status": "blocked",
                    "blocked_reason": block_reason,
                    "cash_context": cash_ctx,
                })
                continue

            # For stocks with stop/take-profit: use bracket order (single submission)
            # This replaces the two-step buy + OCO with one atomic bracket order
            _sl = d.get("stop_loss_price")
            _tp = d.get("take_profit_price")
            if not is_crypto and _sl and _tp and shares and _p:
                # Rebuild payload as bracket order — combines buy + OCO in one API call
                payload = {
                    "symbol":        ticker,
                    "qty":           str(shares),
                    "side":          "buy",
                    "type":          "market" if (_atr_pct > 3.0 or _near or not _p) else "limit",
                    "time_in_force": "day",
                    "order_class":   "bracket",
                    "stop_loss":     {"stop_price": str(_sl)},
                    "take_profit":   {"limit_price": str(_tp)},
                }
                if payload["type"] == "limit":
                    payload["limit_price"] = str(round(_p + max(_p * (_atr_pct / 100) * 0.5, 0.01), 2))
                _ot = f"bracket({'limit' if payload['type']=='limit' else 'market'} SL=${_sl} TP=${_tp})"

            order = alpaca_post("/v2/orders", payload)
            log.info(
                "[%s/Execution] ✅ %s %s %s | ID: %s",
                bucket.name,
                ticker,
                _ot,
                "$" + str(notional) if is_crypto else "×" + str(shares),
                order["id"],
            )
            cfg.increment_daily_trades()
            cfg.bucket_manager.tag_position(ticker, bucket.name)

            results.append({
                "ticker": ticker,
                "shares": shares,
                "notional_usd": notional,
                "asset_class": "crypto" if is_crypto else "us_equity",
                "bucket": bucket.name,
                "order_id": order["id"],
                "status": "placed",
                "rationale": d.get("rationale"),
                "stop_loss_price": d.get("stop_loss_price"),
                "take_profit_price": d.get("take_profit_price"),
                "dual_status": d.get("dual_status", "single"),
                "tiered_source": d.get("tiered_source"),
            })

        except Exception as e:
            log.error("[%s/Execution] ❌ %s: %s", bucket.name, ticker, e)
            results.append({
                "ticker": ticker,
                "shares": shares,
                "bucket": bucket.name,
                "status": "failed",
                "error": str(e),
            })

    return results


def hard_rebalance_agent(
    rebalance: dict,
    positions: list,
    portfolio_value: float,
    market_open: bool,
) -> tuple:
    """
    Execute sell orders to trim overweight buckets when HARD_REBALANCE is enabled.
    Returns (orders, plans).
    """
    if not cfg.SELLING_ENABLED:
        log.info("[HardRebalance] SELLING_ENABLED=false — skipping sells")
        return [], []

    if not cfg.env_bool("HARD_REBALANCE", "false"):
        return [], []

    drift_pct = cfg.hard_rebalance_drift_pct()
    plans = cfg.bucket_manager.plan_hard_rebalance(
        rebalance,
        positions,
        portfolio_value,
        drift_threshold_pct=drift_pct,
        max_sells_per_bucket=1,
        trim_fraction=0.5,
    )
    if not plans:
        return [], []

    pos_by_sym = {p.get("symbol"): p for p in positions if p.get("symbol")}
    orders = []

    log.info("[HardRebalance] %d sell plan(s) (drift threshold %.0f%%)", len(plans), drift_pct * 100)
    for plan in plans:
        if cfg.daily_trades >= cfg.MAX_DAILY_TRADES:
            log.warning("[HardRebalance] Daily trade limit reached — stopping.")
            break
        if not plan.get("is_crypto") and not market_open:
            log.info("[HardRebalance] Skip %s — market closed (equity)", plan["symbol"])
            continue

        sym = plan["symbol"]
        qty = plan["qty"]
        try:
            asset = {
                "symbol": sym,
                "asset_class": plan.get("asset_class") or ("crypto" if plan.get("is_crypto") else "us_equity"),
            }
            payload = {
                "symbol": sym,
                "side": "sell",
                "type": "market",
                "time_in_force": "gtc" if plan.get("is_crypto") else "day",
                "qty": format_qty_for_asset(qty, asset),
            }
            order = alpaca_post("/v2/orders", payload)
            cfg.increment_daily_trades()
            log.info("[HardRebalance] ✅ SELL %s ×%s | %s", sym, qty, plan.get("reason", "")[:80])

            held = pos_by_sym.get(sym) or {}
            held_qty = float(held.get("qty") or 0)
            if held_qty and float(qty) >= held_qty * 0.95:
                cfg.bucket_manager.untag_position(sym)

            orders.append({
                "ticker": sym,
                "shares": qty,
                "bucket": plan.get("bucket"),
                "order_id": order.get("id"),
                "status": "placed",
                "side": "sell",
                "rationale": plan.get("reason"),
                "hard_rebalance": True,
            })
        except Exception as e:
            log.error("[HardRebalance] ❌ %s: %s", sym, e)
            orders.append({
                "ticker": sym,
                "shares": qty,
                "bucket": plan.get("bucket"),
                "status": "failed",
                "side": "sell",
                "error": str(e),
                "hard_rebalance": True,
            })

    return orders, plans
