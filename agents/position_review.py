"""
Position Review Agent — runs before the buy pipeline each cycle.

Checks every open position against:
  1. Hard stop-loss price (from bucket config or stored per-position)
  2. Hard take-profit price
  3. Bucket overweight drift (soft trim)

For positions where we have no stored stop/take-profit prices (the 14
existing positions that never got OCO orders), uses the bucket default
percentages applied to the average entry price.

Returns a list of sell order dicts ready for execution.
"""

import logging
from typing import Optional

import agent_config as cfg
from alpaca_client import alpaca_post, get_open_orders, has_pending_sell_order
from buckets import Bucket
from order_utils import format_qty_for_asset, stop_already_breached

log = logging.getLogger(__name__)


def _bucket_for_position(symbol: str, buckets: list) -> Optional[Bucket]:
    """Find which bucket a symbol belongs to via bucket_tags."""
    tags = cfg.bucket_manager._load_tags()
    bucket_name = tags.get(symbol)
    if not bucket_name:
        return None
    for b in buckets:
        if b.name == bucket_name:
            return b
    return None


def _get_alpaca_stops(symbol: str, open_orders: list) -> dict:
    """Read stop/take-profit from live Alpaca open orders (source of truth)."""
    out: dict = {}
    for o in open_orders or []:
        if o.get("symbol") != symbol:
            continue
        if o.get("stop_price"):
            out["stop_loss_price"] = float(o["stop_price"])
        if o.get("limit_price") and str(o.get("type", "")).lower() in ("limit", "stop_limit"):
            if str(o.get("side", "")).lower() == "sell":
                out.setdefault("take_profit_price", float(o["limit_price"]))
        for leg in o.get("legs") or []:
            leg_type = str(leg.get("type", "")).lower()
            if leg.get("stop_price"):
                out["stop_loss_price"] = float(leg["stop_price"])
            if leg.get("limit_price") and leg_type == "limit":
                out["take_profit_price"] = float(leg["limit_price"])
        if o.get("order_class") == "bracket":
            for key in ("stop_loss", "take_profit"):
                sub = o.get(key) or {}
                if key == "stop_loss" and sub.get("stop_price"):
                    out["stop_loss_price"] = float(sub["stop_price"])
                if key == "take_profit" and sub.get("limit_price"):
                    out["take_profit_price"] = float(sub["limit_price"])
    return out


def _get_stops_for_symbol(symbol: str, open_orders: list) -> dict:
    alpaca_stops = _get_alpaca_stops(symbol, open_orders)
    if alpaca_stops.get("stop_loss_price") or alpaca_stops.get("take_profit_price"):
        return alpaca_stops
    fallback = _get_stored_stops(symbol)
    if fallback:
        log.warning(
            "[PositionReview] %s: no Alpaca bracket/stop found — using SQLite/stored stop fallback",
            symbol,
        )
    else:
        log.warning(
            "[PositionReview] %s: no Alpaca or stored stop data — using bucket %% defaults",
            symbol,
        )
    return fallback


def _get_stored_stops(symbol: str) -> dict:
    """
    Stored stop/take-profit from SQLite funnel/orders/manual stops.
    JSON projection cache is a non-authoritative last resort.
    """
    stop = take = None
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            manual = ledger.get_manual_stop_prices()
            if manual.get(symbol):
                stop = float(manual[symbol])
            for o in reversed(ledger.get_latest_open_orders() or []):
                if (o.get("symbol") or o.get("ticker")) != symbol:
                    continue
                if o.get("stop_price"):
                    stop = stop or float(o["stop_price"])
            funnel = ledger.get_latest_funnel()
            for d in reversed((funnel.get("funnel") or {}).get("decisions") or []):
                if d.get("ticker") != symbol or str(d.get("action", "")).upper() != "BUY":
                    continue
                if d.get("stop_loss_price"):
                    stop = stop or float(d["stop_loss_price"])
                if d.get("take_profit_price"):
                    take = take or float(d["take_profit_price"])
                if stop or take:
                    break
            for o in reversed((funnel.get("funnel") or {}).get("orders") or []):
                if o.get("ticker") != symbol:
                    continue
                if o.get("stop_loss_price"):
                    stop = stop or float(o["stop_loss_price"])
                if o.get("take_profit_price"):
                    take = take or float(o["take_profit_price"])
                if stop or take:
                    break
    except Exception:
        pass

    if not stop and not take:
        # NON-AUTHORITATIVE fallback: agent_state.json projection
        import json
        import os
        from app_paths import resolve_app_dir
        state_path = os.path.join(resolve_app_dir(), "agent_state.json")
        try:
            if os.path.isfile(state_path):
                with open(state_path, encoding="utf-8") as f:
                    state = json.load(f)
                for d in reversed(state.get("decisions") or []):
                    if d.get("ticker") != symbol or str(d.get("action", "")).upper() != "BUY":
                        continue
                    if d.get("stop_loss_price"):
                        stop = float(d["stop_loss_price"])
                    if d.get("take_profit_price"):
                        take = float(d["take_profit_price"])
                    if stop or take:
                        break
                for o in reversed(state.get("last_orders") or []):
                    if o.get("ticker") != symbol:
                        continue
                    if o.get("stop_loss_price"):
                        stop = stop or float(o["stop_loss_price"])
                    if o.get("take_profit_price"):
                        take = take or float(o["take_profit_price"])
                    if stop or take:
                        break
        except Exception:
            pass

    out = {}
    if stop:
        out["stop_loss_price"] = stop
    if take:
        out["take_profit_price"] = take
    return out


def review_positions(
    positions: list,
    account: dict,
    rebalance: dict,
    market_open: bool,
) -> list:
    """
    Evaluate all open positions for exit conditions.

    Called at the START of each cycle, before the buy pipeline, so we
    exit losers before deploying fresh capital.

    Returns list of sell orders placed.
    """
    if not cfg.SELLING_ENABLED:
        log.info("[PositionReview] SELLING_ENABLED=false — skipping position review sells")
        return []

    if not positions:
        return []

    portfolio_value = float(account.get("portfolio_value", 10000))
    all_buckets = cfg.bucket_manager.buckets
    sells_placed = []
    sells_attempted = 0

    log.info("[PositionReview] Checking %d positions for exit conditions...", len(positions))

    try:
        alpaca_open_orders = get_open_orders()
    except Exception as e:
        log.warning("[PositionReview] Could not fetch Alpaca open orders: %s", e)
        alpaca_open_orders = []

    for pos in positions:
        symbol  = pos.get("symbol", "")
        qty     = float(pos.get("qty", 0))
        cost    = float(pos.get("avg_entry_price", 0))
        current = float(pos.get("current_price", 0))
        mkt_val = float(pos.get("market_value", 0))

        if not symbol or qty <= 0 or current <= 0:
            continue

        if has_pending_sell_order(symbol, alpaca_open_orders):
            log.info("[PositionReview] %s: pending sell order already open — skipping", symbol)
            continue

        bucket = _bucket_for_position(symbol, all_buckets)
        if bucket is None:
            # Untagged position — use conservative defaults
            sl_pct = 0.07
            tp_pct = 0.15
        else:
            sl_pct = bucket.stop_loss_pct
            tp_pct = bucket.take_profit_pct

        # Alpaca open orders first, then legacy JSON fallback
        stored = _get_stops_for_symbol(symbol, alpaca_open_orders)
        stop_price = stored.get("stop_loss_price")
        take_price = stored.get("take_profit_price")

        # Fall back to % from average cost
        if not stop_price and cost > 0:
            stop_price = round(cost * (1 - sl_pct), 2)
        if not take_price and cost > 0:
            take_price = round(cost * (1 + tp_pct), 2)

        exit_reason = None
        if stop_price and stop_already_breached(current, stop_price):
            exit_reason = f"stop-loss triggered: ${current:.2f} ≤ ${stop_price:.2f} (entry ${cost:.2f}, -{sl_pct*100:.0f}%)"
        elif take_price and current >= take_price:
            exit_reason = f"take-profit triggered: ${current:.2f} ≥ ${take_price:.2f} (entry ${cost:.2f}, +{tp_pct*100:.0f}%)"

        if exit_reason:
            log.info("[PositionReview] %s: %s — SELL %s shares", symbol, exit_reason, qty)
            order = _place_sell(symbol, qty, exit_reason, bucket, market_open)
            if order and order.get("status") == "placed":
                cfg.bucket_manager.untag_position(symbol)
                sells_placed.append(order)
                sells_attempted += 1
            continue

        # Soft trim: bucket overweight — sell partial position
        if bucket and rebalance and bucket.name in rebalance:
            r = rebalance[bucket.name]
            overweight = (r.get("current_pct", 0) - r.get("target_pct", bucket.allocation_pct * 100)) / 100
            if overweight > cfg.MAX_BUCKET_OVERWEIGHT:
                # Sell enough to bring bucket back to target — trim 50% of this position
                trim_qty = max(1, int(qty * 0.5))
                reason = (
                    f"bucket {bucket.name} overweight {overweight*100:.1f}%"
                    f" (limit {cfg.MAX_BUCKET_OVERWEIGHT*100:.0f}%)"
                    f" — trimming {trim_qty} of {qty:.0f} shares"
                )
                log.info("[PositionReview] %s: %s", symbol, reason)
                order = _place_sell(symbol, trim_qty, reason, bucket, market_open)
                if order:
                    sells_placed.append(order)
                    sells_attempted += 1

    if sells_attempted == 0:
        log.info("[PositionReview] No exit conditions triggered.")
    else:
        log.info("[PositionReview] %d sell order(s) placed.", sells_attempted)

    return sells_placed


def _place_sell(symbol: str, qty, reason: str, bucket: Optional[Bucket], market_open: bool) -> Optional[dict]:
    """Submit a market sell order to Alpaca."""
    is_crypto = bucket and (
        getattr(bucket, "asset_class", "") == "crypto" or bucket.mode == "crypto"
    )

    if not is_crypto and not market_open:
        log.info("[PositionReview] %s: market closed — deferring sell until next open", symbol)
        return None

    if cfg.daily_trades >= cfg.MAX_DAILY_TRADES:
        log.warning("[PositionReview] Daily trade limit reached — cannot sell %s", symbol)
        return None

    try:
        asset = {"symbol": symbol, "asset_class": "crypto" if is_crypto else "us_equity"}
        qty_str = format_qty_for_asset(qty, asset)
        payload = {
            "symbol":        symbol,
            "qty":           qty_str,
            "side":          "sell",
            "type":          "market",
            "time_in_force": "gtc" if is_crypto else "day",
        }
        order = alpaca_post("/v2/orders", payload)
        cfg.increment_daily_trades()
        log.info("[PositionReview] ✅ SELL %s ×%s | %s | ID: %s",
                 symbol, qty, reason[:80], order.get("id", "?"))
        return {
            "ticker":    symbol,
            "shares":    qty,
            "bucket":    bucket.name if bucket else "unknown",
            "order_id":  order.get("id"),
            "status":    "placed",
            "side":      "sell",
            "rationale": reason,
            "source":    "position_review",
        }
    except Exception as e:
        log.error("[PositionReview] ❌ SELL %s failed: %s", symbol, e)
        return {
            "ticker": symbol, "shares": qty, "side": "sell",
            "status": "failed", "error": str(e), "source": "position_review",
        }
