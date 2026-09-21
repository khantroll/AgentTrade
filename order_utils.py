"""Shared order helpers — stop breach checks and Alpaca quantity formatting."""

from __future__ import annotations

from typing import Any, Union

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
    """Format quantity for Alpaca — fractional for crypto, whole shares for equity."""
    q = float(qty)
    if q <= 0:
        raise ValueError(f"qty must be positive, got {qty!r}")
    if is_crypto_asset(asset):
        s = f"{q:.9f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(int(q))


def round_price_for_asset(price: float, asset: AssetRef) -> float:
    """Round stop/limit prices to Alpaca-friendly precision."""
    decimals = 4 if is_crypto_asset(asset) else 2
    return round(float(price), decimals)
