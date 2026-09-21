"""Technical indicators for deterministic risk (Tier 2)."""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import agent_config as cfg

log = logging.getLogger(__name__)

BUCKET_ATR_MULTIPLIERS = {
    "growth": 2.5,
    "swing": 2.0,
    "dividend": 3.0,
    "crypto": 3.5,
}

DEFAULT_ATR_MULTIPLIER = 2.0


def _series(data: Any, key: str) -> list[float]:
    if isinstance(data, dict):
        if key in data and data[key] is not None:
            return [float(x) for x in data[key]]
        alt = {"high": "highs", "low": "lows", "close": "closes"}.get(key, key)
        if alt in data and data[alt] is not None:
            return [float(x) for x in data[alt]]
    return []


def calculate_atr(ohlcv_data: Any, period: int = 14) -> float:
    """
    Standard Average True Range over `period` bars.

    true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
    ATR = mean(true_range) over period
    """
    highs = _series(ohlcv_data, "high")
    lows = _series(ohlcv_data, "low")
    closes = _series(ohlcv_data, "close")
    if not closes and isinstance(ohlcv_data, dict):
        closes = _series(ohlcv_data, "closes")
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return 0.0

    trs: list[float] = []
    start = n - period
    for i in range(start, n):
        prev_close = closes[i - 1]
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        trs.append(tr)
    return round(sum(trs) / len(trs), 6) if trs else 0.0


def _bucket_mode(bucket) -> str:
    if bucket is None:
        return "growth"
    mode = getattr(bucket, "mode", None) or getattr(bucket, "name", "growth")
    return str(mode).lower()


def _atr_multiplier(bucket=None, atr_multiplier: Optional[float] = None) -> float:
    if atr_multiplier is not None:
        return float(atr_multiplier)
    mode = _bucket_mode(bucket)
    return float(BUCKET_ATR_MULTIPLIERS.get(mode, DEFAULT_ATR_MULTIPLIER))


def calculate_atr_stop(
    *,
    side: str,
    entry_price: float,
    atr: float,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    fallback_stop_pct: float = 0.07,
    bucket=None,
    allow_shorts: Optional[bool] = None,
) -> dict:
    """
    ATR-based stop with flat bucket-% fallback when ATR invalid.
    """
    side_l = str(side or "buy").lower()
    allow_shorts = (
        cfg.env_bool("ALLOW_SHORTS", "false") if allow_shorts is None else allow_shorts
    )
    mult = _atr_multiplier(bucket, atr_multiplier)
    fb_pct = fallback_stop_pct
    if bucket and getattr(bucket, "stop_loss_pct", None):
        fb_pct = float(bucket.stop_loss_pct)

    if side_l in ("sell", "short") and not allow_shorts:
        return {
            "approved": False,
            "stop_price": None,
            "atr": atr,
            "atr_multiplier": mult,
            "used_fallback": False,
            "reason": "short-side stops disabled (ALLOW_SHORTS=false)",
        }

    if entry_price <= 0:
        return {
            "approved": False,
            "stop_price": None,
            "atr": atr,
            "atr_multiplier": mult,
            "used_fallback": True,
            "reason": "invalid entry price",
        }

    used_fallback = False
    if atr not in (None, 0, 0.0) and float(atr) > 0:
        if side_l in ("sell", "short"):
            stop_price = round(entry_price + (atr * mult), 4)
        else:
            stop_price = round(entry_price - (atr * mult), 4)
        reason = f"ATR stop ({mult}× ATR)"
    else:
        used_fallback = True
        if side_l in ("sell", "short"):
            stop_price = round(entry_price * (1 + fb_pct), 4)
        else:
            stop_price = round(entry_price * (1 - fb_pct), 4)
        reason = f"flat bucket fallback ({fb_pct * 100:.1f}% stop)"

    if side_l not in ("sell", "short") and stop_price >= entry_price:
        return {
            "approved": False,
            "stop_price": stop_price,
            "atr": atr,
            "atr_multiplier": mult,
            "used_fallback": used_fallback,
            "reason": "stop not below entry for long",
        }

    return {
        "approved": True,
        "stop_price": stop_price,
        "atr": atr,
        "atr_multiplier": mult,
        "used_fallback": used_fallback,
        "reason": reason,
    }


def calculate_atr_take_profit_recommendation(
    *,
    entry_price: float,
    atr: float,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    reward_multiplier: Optional[float] = None,
    side: str = "buy",
) -> dict:
    """
    Optional 2:1 reward/risk take-profit recommendation (not authoritative).
    """
    mult = float(atr_multiplier)
    reward_mult = reward_multiplier if reward_multiplier is not None else mult * 2
    side_l = str(side or "buy").lower()
    if entry_price <= 0 or not atr or atr <= 0:
        return {
            "recommended": False,
            "take_profit_price": None,
            "reward_multiplier": reward_mult,
            "reason": "ATR unavailable for take-profit recommendation",
        }
    if side_l in ("sell", "short"):
        tp = round(entry_price - (atr * reward_mult), 4)
    else:
        tp = round(entry_price + (atr * reward_mult), 4)
    return {
        "recommended": True,
        "take_profit_price": tp,
        "reward_multiplier": reward_mult,
        "reason": f"ATR take-profit recommendation ({reward_mult}× ATR)",
    }
