"""Shared order helpers — stop breach checks and Alpaca quantity formatting."""

from __future__ import annotations

import math
from typing import Any, Optional, Union

AssetRef = Union[str, dict, None]

_CRYPTO_USD_BASES = frozenset({
    "BTC", "ETH", "SOL", "LINK", "DOGE", "LTC", "BCH", "AVAX", "UNI", "AAVE",
    "SHIB", "DOT", "MATIC", "XRP", "ADA", "USDC", "USDT",
})


def stop_already_breached(current_price: float, stop_price: float) -> bool:
    """True when a long position's stop has already been hit (price at or below stop)."""
    return current_price <= stop_price


def is_crypto_asset(asset: AssetRef) -> bool:
    """Detect crypto from Alpaca position dict, symbol string, or asset_class."""
    if asset is None:
        return False
    if isinstance(asset, dict):
        if str(asset.get("asset_class", "")).lower() == "crypto":
            return True
        sym = asset.get("symbol") or asset.get("ticker") or ""
    else:
        sym = str(asset)
    return is_crypto_symbol(sym)


def is_crypto_symbol(symbol: str) -> bool:
    sym = (symbol or "").upper()
    if not sym:
        return False
    if "/" in sym:
        return True
    if sym.endswith("USD") and len(sym) > 3:
        base = sym[:-3]
        if base in _CRYPTO_USD_BASES or len(base) <= 5:
            return True
    return False


def format_qty_for_asset(qty: Union[int, float, str], asset: AssetRef) -> str:
    """Format quantity for Alpaca — fractional for crypto, whole shares for equity.

    Equity and bracket orders reject fractional qty (HTTP 422). A value such as
    ``2.0`` or ``"3.0"`` must be sent as ``"2"`` / ``"3"``, not ``"2.0"``.
    """
    q = float(qty)
    if q <= 0:
        raise ValueError(f"qty must be positive, got {qty!r}")
    if is_crypto_asset(asset):
        s = f"{q:.9f}".rstrip("0").rstrip(".")
        return s or "0"
    whole = int(q)
    if whole < 1:
        raise ValueError(f"equity qty must be at least 1 share, got {qty!r}")
    return str(whole)


def _price_decimals(price: float) -> int:
    """Alpaca equity ticks: $0.01 at or above $1, $0.0001 below $1."""
    return 2 if float(price) >= 1 else 4


def _to_ticks(price: float, decimals: int, mode: str) -> int:
    factor = 10 ** decimals
    scaled = float(price) * factor
    if mode == "down":
        return int(math.floor(scaled + 1e-8))
    if mode == "up":
        return int(math.ceil(scaled - 1e-8))
    return int(math.floor(scaled + 0.5 + 1e-8))


def format_alpaca_price(price: float, decimals: Optional[int] = None) -> str:
    """Format a price with the exact decimal places Alpaca accepts."""
    places = _price_decimals(price) if decimals is None else decimals
    return f"{float(price):.{places}f}"


def fit_buy_protection(
    stop_price: float,
    take_profit_price: float,
    *,
    market_price: Optional[float] = None,
    limit_price: Optional[float] = None,
) -> dict:
    """Place a long bracket's stop below, and take-profit above, the entry.

    Alpaca rejects a buy bracket when the stop is at or above the market or
    the limit (HTTP 422, surfaced here as stop-above-market) and when prices
    have more than two decimals on names at or above $1. Rounding the stop
    down and the take-profit up never loosens the approved stop: a stop that
    is already through the market is pulled one tick below the lower anchor.
    """
    anchors = [float(p) for p in (market_price, limit_price) if p and float(p) > 0]
    if not anchors:
        raise ValueError("buy bracket needs a market or limit price")
    if stop_price is None or take_profit_price is None:
        raise ValueError("stop and take-profit are required")
    decimals = _price_decimals(max(anchors))
    stop_units = _to_ticks(float(stop_price), decimals, "down")
    tp_units = _to_ticks(float(take_profit_price), decimals, "up")
    lower_units = min(_to_ticks(p, decimals, "nearest") for p in anchors)
    upper_units = max(_to_ticks(p, decimals, "nearest") for p in anchors)
    stop_ceiling = lower_units - 1
    tp_floor = upper_units + 1
    stop_adjusted = stop_units > stop_ceiling
    tp_adjusted = tp_units < tp_floor
    if stop_adjusted:
        stop_units = stop_ceiling
    if tp_adjusted:
        tp_units = tp_floor
    if stop_units <= 0 or not (stop_units < lower_units and tp_units > upper_units):
        raise ValueError("bracket prices cannot be placed around the entry")
    factor = 10 ** decimals
    return {
        "stop_price": stop_units / factor,
        "take_profit_price": tp_units / factor,
        "decimals": decimals,
        "stop_adjusted": stop_adjusted,
        "take_profit_adjusted": tp_adjusted,
    }


def build_equity_buy_payload(
    symbol: str,
    shares: Union[int, float, str],
    *,
    order_type: str = "market",
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    current_price: Optional[float] = None,
    include_bracket: bool = True,
) -> dict:
    """Alpaca equity buy body: whole-share qty and a valid long bracket.

    ``include_bracket`` is false when an open sell already rests on the symbol.
    A second OCO would 422 as a conflicting order; the approved entry is still
    submitted without new child orders.
    """
    payload = {
        "symbol": symbol,
        "qty": format_qty_for_asset(shares, symbol),
        "side": "buy",
        "type": "limit" if order_type == "limit" else "market",
        "time_in_force": "day",
    }
    rounded_limit = None
    if payload["type"] == "limit":
        if not limit_price or float(limit_price) <= 0:
            raise ValueError("limit price required")
        decimals = _price_decimals(float(limit_price))
        units = _to_ticks(float(limit_price), decimals, "nearest")
        rounded_limit = units / (10 ** decimals)
        payload["limit_price"] = format_alpaca_price(rounded_limit, decimals)
    if (
        include_bracket
        and stop_price not in (None, "", 0, 0.0)
        and take_profit_price not in (None, "", 0, 0.0)
        and ((current_price and float(current_price) > 0) or rounded_limit)
    ):
        fitted = fit_buy_protection(
            float(stop_price),
            float(take_profit_price),
            market_price=float(current_price) if current_price else None,
            limit_price=rounded_limit,
        )
        decimals = fitted["decimals"]
        payload["order_class"] = "bracket"
        payload["stop_loss"] = {
            "stop_price": format_alpaca_price(fitted["stop_price"], decimals),
        }
        payload["take_profit"] = {
            "limit_price": format_alpaca_price(fitted["take_profit_price"], decimals),
        }
        payload["_stop_adjusted"] = fitted["stop_adjusted"]
        payload["_take_profit_adjusted"] = fitted["take_profit_adjusted"]
    return payload


_TERMINAL_ORDER_STATUSES = frozenset({
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "replaced",
    "done_for_day",
})


def plan_equity_buy_vs_open_orders(symbol: str, open_orders: Optional[list] = None) -> dict:
    """Decide cancel/replace versus skip so a new buy does not double-submit.

    Resting unfilled buys are canceled and replaced. A partial fill is left
    alone (a second full qty would 422 or double the position). An open sell
    is not canceled; the entry is sent without a new bracket so it does not
    conflict with the existing exit.
    """
    sym = str(symbol or "").upper()
    buys = []
    sells = []
    for order in open_orders or []:
        if not isinstance(order, dict):
            continue
        if str(order.get("symbol") or "").upper() != sym:
            continue
        status = str(order.get("status") or "open").lower()
        if status in _TERMINAL_ORDER_STATUSES:
            continue
        side = str(order.get("side") or "").lower()
        if side == "buy":
            buys.append(order)
        elif side == "sell":
            sells.append(order)

    partial = []
    resting = []
    for order in buys:
        try:
            filled = float(order.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        status = str(order.get("status") or "").lower()
        if filled > 0 or status == "partially_filled":
            partial.append(order)
        else:
            resting.append(order)

    if partial:
        return {
            "action": "skip",
            "reason": "open_buy_partial_fill",
            "cancel_ids": [],
            "include_bracket": False,
        }

    cancel_ids = []
    for order in resting:
        oid = order.get("id") or order.get("order_id")
        if oid:
            cancel_ids.append(str(oid))
    return {
        "action": "cancel_then_submit" if cancel_ids else "submit",
        "reason": "",
        "cancel_ids": cancel_ids,
        "include_bracket": not bool(sells),
    }


def round_price_for_asset(price: float, asset: AssetRef) -> float:
    """Round stop/limit prices to Alpaca-friendly precision."""
    decimals = 4 if is_crypto_asset(asset) else 2
    return round(float(price), decimals)
