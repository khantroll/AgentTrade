"""Live cash guardrails before BUY order submission."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import agent_config as cfg
from account_sync import refresh_alpaca_snapshot

log = logging.getLogger(__name__)


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def check_buy_allowed(
    order_notional: float,
    snapshot: Optional[dict] = None,
    buy_lock: Optional[dict] = None,
    symbol: Optional[str] = None,
    asset_class: Optional[str] = None,
    bucket: Optional[str] = None,
) -> Tuple[bool, str, dict]:
    """
    Verify a buy is allowed against live Alpaca cash and scoped buy locks.

    Returns (allowed, reason, context_dict).
    """
    from buy_lock import is_buy_locked

    ctx: dict = {}

    if not cfg.BUYING_ENABLED:
        return False, "BUYING_ENABLED=false", ctx

    locked, lock_reason, lock_detail = is_buy_locked(
        symbol=symbol,
        asset_class=asset_class,
        bucket=bucket,
        buy_lock=buy_lock,
    )
    if locked:
        scope = (lock_detail or {}).get("scope", "symbol")
        sym = (lock_detail or {}).get("symbol") or symbol or ""
        if scope == "global":
            msg = "buying disabled"
        elif sym:
            msg = f"symbol lock active for {sym}"
        else:
            msg = f"{scope} lock active ({lock_reason})"
        return False, msg, {"buy_lock": lock_detail}

    snap = snapshot or refresh_alpaca_snapshot()
    acct = snap.get("account") or snap
    cash = _float(acct.get("cash", snap.get("cash")))
    buying_power = _float(acct.get("buying_power", snap.get("buying_power")))
    open_buy_notional = _float(snap.get("open_buy_notional"))
    notional = _float(order_notional)

    projected_cash = cash - open_buy_notional - notional
    ctx = {
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "order_notional": round(notional, 2),
        "open_buy_notional": round(open_buy_notional, 2),
        "projected_cash": round(projected_cash, 2),
        "min_cash_reserve": cfg.MIN_CASH_RESERVE,
    }

    if cash <= 0 and not cfg.ALLOW_NEGATIVE_CASH:
        log.warning(
            "BUY blocked: insufficient live cash (cash=%s buying_power=%s order_notional=%s projected_cash=%s)",
            ctx["cash"], ctx["buying_power"], ctx["order_notional"], ctx["projected_cash"],
        )
        return False, "insufficient live cash", ctx

    if not cfg.ALLOW_NEGATIVE_CASH and projected_cash < 0:
        log.warning(
            "BUY blocked: insufficient live cash (cash=%s buying_power=%s order_notional=%s projected_cash=%s)",
            ctx["cash"], ctx["buying_power"], ctx["order_notional"], ctx["projected_cash"],
        )
        return False, "insufficient live cash", ctx

    if projected_cash < cfg.MIN_CASH_RESERVE and not cfg.ALLOW_NEGATIVE_CASH:
        log.warning(
            "BUY blocked: insufficient live cash — below MIN_CASH_RESERVE "
            "(cash=%s buying_power=%s order_notional=%s projected_cash=%s reserve=%s)",
            ctx["cash"], ctx["buying_power"], ctx["order_notional"], ctx["projected_cash"], cfg.MIN_CASH_RESERVE,
        )
        return False, "insufficient live cash", ctx

    return True, "", ctx
