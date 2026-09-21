"""Tier 1 no-margin order guard — runs immediately before every submission."""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import agent_config as cfg

log = logging.getLogger(__name__)

MARGIN_TOLERANCE = 0.01


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _order_side(proposed_order: dict) -> str:
    return str(proposed_order.get("side", "buy")).lower()


def _estimated_notional(proposed_order: dict, account: Optional[dict] = None) -> float:
    if proposed_order.get("notional") is not None:
        return _float(proposed_order.get("notional"))
    if proposed_order.get("notional_usd") is not None:
        return _float(proposed_order.get("notional_usd"))
    qty = _float(proposed_order.get("qty") or proposed_order.get("shares"))
    if qty <= 0:
        return 0.0
    for key in ("limit_price", "stop_price", "current_price", "price", "estimated_price"):
        px = _float(proposed_order.get(key))
        if px > 0:
            return qty * px
    return 0.0


def enforce_no_margin_order_guard(
    account: dict,
    proposed_order: dict,
) -> Tuple[bool, str]:
    """
    Final pre-submit guard when ALLOW_MARGIN is False.
    Uses cash (not buying_power) for buy affordability.
    """
    allow_margin = cfg.ALLOW_MARGIN and cfg.env_bool("ALLOW_MARGIN", "false")
    allow_shorts = cfg.env_bool("ALLOW_SHORTS", "false")

    cash = _float(account.get("cash"))
    short_mv = _float(account.get("short_market_value"))
    side = _order_side(proposed_order)

    if not allow_margin and cash < -MARGIN_TOLERANCE:
        return False, f"ORDER_REJECTED_NO_MARGIN: cash ${cash:,.2f} is negative"

    if not allow_shorts and short_mv > MARGIN_TOLERANCE:
        return False, f"ORDER_REJECTED_NO_MARGIN: short exposure ${short_mv:,.2f}"

    if not allow_margin and side == "buy":
        notional = _estimated_notional(proposed_order)
        if notional <= 0:
            return False, "ORDER_REJECTED_NO_MARGIN: cannot estimate buy notional"
        if notional > cash + MARGIN_TOLERANCE:
            return False, (
                f"ORDER_REJECTED_NO_MARGIN: buy notional ${notional:,.2f} "
                f"exceeds cash ${cash:,.2f}"
            )
        if cash - notional < -MARGIN_TOLERANCE:
            return False, (
                f"ORDER_REJECTED_NO_MARGIN: order would make cash negative "
                f"(cash=${cash:,.2f}, notional=${notional:,.2f})"
            )

    return True, ""


def record_order_rejection(
    cycle_run_id: Optional[int],
    symbol: str,
    reason: str,
    proposed_order: Optional[dict] = None,
) -> None:
    if not cycle_run_id:
        return
    try:
        from agenttrade import db as ledger
        ledger.insert_risk_event(
            cycle_run_id,
            "critical",
            "ORDER_REJECTED_NO_MARGIN",
            reason,
            symbol=symbol or "",
            raw=proposed_order or {},
        )
    except Exception as e:
        log.warning("[BuyGuard] Could not record risk event: %s", e)
