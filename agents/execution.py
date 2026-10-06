"""Agent 4: Execution — Alpaca orders and hard rebalance sells."""

import logging
from datetime import datetime

import agent_config as cfg
from alpaca_client import (
    AlpacaAPIError,
    alpaca_delete,
    alpaca_post,
    get_account,
    get_open_orders,
    has_pending_sell_order,
)
from buckets import Bucket
from buy_lock import is_buy_locked
from buy_guard import check_buy_allowed
from agenttrade.buy_guard import enforce_no_margin_order_guard, record_order_rejection
from market_data import is_crypto_bucket
from order_utils import (
    build_equity_buy_payload,
    format_qty_for_asset,
    plan_equity_buy_vs_open_orders,
)

log = logging.getLogger(__name__)


def _resolve_equity_open_order_plan(ticker: str, snapshot: dict) -> dict:
    """Cancel resting same-symbol buys. Do not cancel protective sells."""
    orders = []
    if isinstance(snapshot, dict) and isinstance(snapshot.get("open_orders"), list):
        orders = snapshot["open_orders"]
    plan = plan_equity_buy_vs_open_orders(ticker, orders)
    if plan["action"] != "cancel_then_submit":
        return plan
    cancelled = []
    for oid in plan["cancel_ids"]:
        try:
            alpaca_delete(f"/v2/orders/{oid}")
            log.info("[Execution] Canceled resting buy %s for %s before replace", oid, ticker)
            cancelled.append(oid)
        except AlpacaAPIError as exc:
            if getattr(exc, "status_code", None) == 404:
                cancelled.append(oid)
                continue
            log.error("[Execution] Could not cancel open buy %s for %s: %s", oid, ticker, exc)
            return {
                "action": "skip",
                "reason": "open_buy_cancel_failed",
                "cancel_ids": plan["cancel_ids"],
                "include_bracket": False,
            }
        except Exception as exc:
            log.error("[Execution] Could not cancel open buy %s for %s: %s", oid, ticker, exc)
            return {
                "action": "skip",
                "reason": "open_buy_cancel_failed",
                "cancel_ids": plan["cancel_ids"],
                "include_bracket": False,
            }
    if isinstance(snapshot, dict) and cancelled:
        snapshot["open_orders"] = [
            order for order in orders
            if str((order or {}).get("id") or (order or {}).get("order_id") or "") not in set(cancelled)
        ]
    return plan


def _broker_payload(payload: dict) -> dict:
    """Drop local bookkeeping keys before the Alpaca POST.

    Stamp ``client_order_id`` so a later count can tell this order from
    another app using the same Alpaca account.
    """
    from trading_day import new_client_order_id

    body = {key: value for key, value in payload.items() if not str(key).startswith("_")}
    if not body.get("client_order_id"):
        body["client_order_id"] = new_client_order_id()
    return body


def execution_agent(approved: list, bucket: Bucket, buy_lock: dict = None,
                    account_snapshot: dict = None, cycle_run_id: int = None) -> list:
    results = []

    from agenttrade import db as ledger
    if ledger.trading_halted():
        halt_reason = ledger.get_halt_reason() or "TRADING_HALTED=true"
        log.warning("[Execution] BUY blocked: TRADING_HALTED — %s", halt_reason)
        return [
            {
                "ticker": d.get("ticker"),
                "shares": d.get("shares"),
                "bucket": bucket.name,
                "status": "blocked",
                "blocked_reason": "trading_halted",
                "reason": "trading_halted",
                "halt_reason": halt_reason,
            }
            for d in (approved or []) if d.get("ticker")
        ]

    if ledger.trading_paused():
        pause_reason = ledger.get_pause_reason() or "consecutive loss pause"
        log.warning(
            "[Execution] BUY blocked: TRADING_PAUSED — %s",
            pause_reason,
        )
        return [
            {
                "ticker": d.get("ticker"),
                "shares": d.get("shares"),
                "bucket": bucket.name,
                "status": "blocked",
                "blocked_reason": "trading_paused",
                "reason": "trading_paused",
                "pause_reason": pause_reason,
            }
            for d in (approved or []) if d.get("ticker")
        ]

    if not cfg.BUYING_ENABLED:
        log.warning("[Execution] BUY blocked: BUYING_ENABLED=false")
        return [
            {
                "ticker": d.get("ticker"),
                "shares": d.get("shares"),
                "bucket": bucket.name,
                "status": "blocked",
                "blocked_reason": "buying_disabled",
                "reason": "buying_disabled",
            }
            for d in (approved or []) if d.get("ticker")
        ]

    from agenttrade.strategy_modes import get_bucket_execution_mode, mode_blocks_order

    snapshot = account_snapshot

    for d in approved:
        ticker = d.get("ticker") or d.get("symbol") or ""
        if cfg.daily_trade_cap_reached():
            log.warning(
                "[Execution] Daily trade limit reached (%s/%s).",
                cfg.daily_trades,
                cfg.MAX_DAILY_TRADES,
            )
            results.append({
                "ticker": ticker,
                "shares": d.get("shares"),
                "bucket": bucket.name,
                "status": "blocked",
                "blocked_reason": "daily_trade_limit",
                "reason": "daily_trade_limit",
            })
            for rest in approved[approved.index(d) + 1:]:
                if rest.get("ticker"):
                    results.append({
                        "ticker": rest.get("ticker"),
                        "shares": rest.get("shares"),
                        "bucket": bucket.name,
                        "status": "blocked",
                        "blocked_reason": "daily_trade_limit",
                        "reason": "daily_trade_limit",
                    })
            break

        ticker = d["ticker"]
        is_crypto = is_crypto_bucket(bucket)
        exec_mode = get_bucket_execution_mode(bucket.name)
        blocked, block_reason = mode_blocks_order(bucket.name, cfg.ALPACA_PAPER)
        if blocked:
            log.warning("[Execution] %s blocked for %s: %s", exec_mode, ticker, block_reason)
            hypo = {
                "ticker": ticker,
                "shares": d.get("shares"),
                "notional_usd": d.get("notional_usd"),
                "bucket": bucket.name,
                "status": exec_mode,
                "blocked_reason": block_reason,
                "stop_loss_price": d.get("stop_loss_price"),
                "take_profit_price": d.get("take_profit_price"),
            }
            if cycle_run_id:
                try:
                    ledger.insert_hypothetical_trade(cycle_run_id, {
                        "strategy_name": bucket.name,
                        "symbol": ticker,
                        "side": "buy",
                        "qty": d.get("shares"),
                        "notional": d.get("notional_usd"),
                        "price": d.get("current_price"),
                        "stop_price": d.get("stop_loss_price"),
                        "take_profit_price": d.get("take_profit_price"),
                        "mode": exec_mode,
                        **hypo,
                    })
                except Exception:
                    pass
            results.append(hypo)
            continue

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
                    results.append({
                        "ticker": ticker,
                        "bucket": bucket.name,
                        "status": "blocked",
                        "blocked_reason": "missing_notional",
                        "reason": "missing_notional",
                    })
                    continue
            elif not shares:
                log.warning("[%s/Execution] %s: no shares for stock order — skipping", bucket.name, ticker)
                results.append({
                    "ticker": ticker,
                    "bucket": bucket.name,
                    "status": "blocked",
                    "blocked_reason": "missing_shares",
                    "reason": "missing_shares",
                })
                continue
            else:
                try:
                    payload["qty"] = format_qty_for_asset(shares, ticker)
                except ValueError as ve:
                    log.warning("[%s/Execution] %s: %s", bucket.name, ticker, ve)
                    results.append({
                        "ticker": ticker,
                        "shares": shares,
                        "bucket": bucket.name,
                        "status": "blocked",
                        "blocked_reason": "invalid_qty",
                        "reason": "invalid_qty",
                        "error": str(ve),
                    })
                    continue

            order_notional = float(notional or 0) if is_crypto else float(shares or 0) * float(_p or d.get("current_price") or 0)
            ac = "crypto" if is_crypto else "us_equity"
            allowed, block_reason, cash_ctx = check_buy_allowed(
                order_notional,
                snapshot=snapshot,
                buy_lock=buy_lock,
                symbol=ticker,
                asset_class=ac,
                bucket=bucket.name,
            )
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

            live_account = get_account()
            proposed = {
                "side": "buy",
                "symbol": ticker,
                "qty": shares,
                "shares": shares,
                "notional": notional,
                "notional_usd": round(float(order_notional), 2),
                "estimated_notional": round(float(order_notional), 2),
                "current_price": _p or d.get("current_price"),
                "current_price": _p or d.get("current_price"),
            }
            ok, guard_reason = enforce_no_margin_order_guard(live_account, proposed)
            if not ok:
                log.warning("[Execution] %s", guard_reason)
                record_order_rejection(cycle_run_id, ticker, guard_reason, proposed)
                results.append({
                    "ticker": ticker,
                    "shares": shares,
                    "bucket": bucket.name,
                    "status": "blocked",
                    "blocked_reason": guard_reason,
                })
                continue

            # Equity buys: whole-share qty, stop below the market, take-profit above it.
            # A resting unfilled buy is canceled first so this submit is a replace.
            if not is_crypto and shares:
                plan = _resolve_equity_open_order_plan(ticker, snapshot if isinstance(snapshot, dict) else {})
                if plan.get("action") == "skip":
                    reason = plan.get("reason") or "open_buy_conflict"
                    log.warning("[%s/Execution] %s: %s — not submitting", bucket.name, ticker, reason)
                    results.append({
                        "ticker": ticker,
                        "shares": shares,
                        "bucket": bucket.name,
                        "status": "blocked",
                        "blocked_reason": reason,
                        "reason": reason,
                    })
                    continue
                use_market = _atr_pct > 3.0 or _near or not _p
                limit_price = None
                if not use_market:
                    limit_price = round(_p + max(_p * (_atr_pct / 100) * 0.5, 0.01), 2)
                try:
                    payload = build_equity_buy_payload(
                        ticker,
                        shares,
                        order_type="market" if use_market else "limit",
                        limit_price=limit_price,
                        stop_price=d.get("stop_loss_price"),
                        take_profit_price=d.get("take_profit_price"),
                        current_price=_p or None,
                        include_bracket=bool(plan.get("include_bracket", True)),
                    )
                except ValueError as ve:
                    log.warning("[%s/Execution] %s: %s", bucket.name, ticker, ve)
                    results.append({
                        "ticker": ticker,
                        "shares": shares,
                        "bucket": bucket.name,
                        "status": "blocked",
                        "blocked_reason": "invalid_qty",
                        "reason": "invalid_qty",
                        "error": str(ve),
                    })
                    continue
                if payload.get("order_class") == "bracket":
                    _sl = (payload.get("stop_loss") or {}).get("stop_price")
                    _tp = (payload.get("take_profit") or {}).get("limit_price")
                    _ot = f"bracket({payload['type']} SL=${_sl} TP=${_tp})"
                    if payload.get("_stop_adjusted") or payload.get("_take_profit_adjusted"):
                        log.info(
                            "[Execution] %s bracket prices moved onto the correct side of the entry "
                            "(stop=%s take_profit=%s)",
                            ticker,
                            _sl,
                            _tp,
                        )
                elif not plan.get("include_bracket", True):
                    log.info(
                        "[Execution] %s entry without bracket — open sell already rests on the symbol",
                        ticker,
                    )

            broker_body = _broker_payload(payload)
            order = alpaca_post("/v2/orders", broker_body)
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

            order_row = {
                "ticker": ticker,
                "shares": shares,
                "notional_usd": notional,
                "asset_class": "crypto" if is_crypto else "us_equity",
                "bucket": bucket.name,
                "order_id": order["id"],
                "client_order_id": broker_body.get("client_order_id"),
                "status": "placed",
                "side": "buy",
                "rationale": d.get("rationale"),
                "stop_loss_price": d.get("stop_loss_price"),
                "take_profit_price": d.get("take_profit_price"),
                "dual_status": d.get("dual_status", "single"),
                "tiered_source": d.get("tiered_source"),
                "source": d.get("source") or d.get("candidate_source") or "",
                "candidate_source": d.get("candidate_source") or d.get("source") or "",
                "entry_source": d.get("entry_source") or "",
                "analysis_path": d.get("analysis_path") or "",
            }
            if cycle_run_id:
                try:
                    from agenttrade import db as ledger
                    ledger.record_submitted_order(cycle_run_id, order_row, strategy_name=bucket.name)
                except Exception:
                    pass

            results.append(order_row)

        except AlpacaAPIError as e:
            log.error("[%s/Execution] ❌ %s broker rejected: %s", bucket.name, ticker, e)
            results.append({
                "ticker": ticker,
                "shares": shares,
                "bucket": bucket.name,
                "status": "failed",
                "blocked_reason": "broker_rejected",
                "reason": "broker_rejected",
                "error": str(e),
            })
        except Exception as e:
            log.error("[%s/Execution] ❌ %s: %s", bucket.name, ticker, e)
            results.append({
                "ticker": ticker,
                "shares": shares,
                "bucket": bucket.name,
                "status": "failed",
                "blocked_reason": "execution_failed",
                "reason": "execution_failed",
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

    try:
        open_orders = get_open_orders()
    except Exception as e:
        log.warning("[HardRebalance] Could not fetch open orders: %s", e)
        open_orders = []

    log.info("[HardRebalance] %d sell plan(s) (drift threshold %.0f%%)", len(plans), drift_pct * 100)
    for plan in plans:
        if cfg.daily_trade_cap_reached():
            log.warning(
                "[HardRebalance] Daily trade limit reached (%s/%s) — stopping.",
                cfg.daily_trades,
                cfg.MAX_DAILY_TRADES,
            )
            break
        if not plan.get("is_crypto") and not market_open:
            log.info("[HardRebalance] Skip %s — market closed (equity)", plan["symbol"])
            continue

        sym = plan["symbol"]
        if has_pending_sell_order(sym, open_orders):
            log.info("[HardRebalance] %s: pending sell order already open — skipping", sym)
            continue
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
            from trading_day import new_client_order_id
            payload["client_order_id"] = new_client_order_id()
            order = alpaca_post("/v2/orders", payload)
            # Exits do not consume a daily entry slot.
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
