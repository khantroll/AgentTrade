"""Deterministic risk manager — no strategy or LLM may override executable sizing (Tier 2)."""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

import agent_config as cfg
from agenttrade import db as ledger
from agenttrade.indicators import (
    calculate_atr,
    calculate_atr_stop,
    calculate_atr_take_profit_recommendation,
)

log = logging.getLogger(__name__)

MAX_RISK_PER_TRADE_PCT = 0.005
MAX_ACCOUNT_DRAWDOWN_PCT = 0.10
MAX_CONSECUTIVE_LOSSES = 5

LLM_SIZING_KEYS = ("qty", "shares", "notional", "notional_usd", "position_size")


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def refresh_risk_constants() -> None:
    global MAX_RISK_PER_TRADE_PCT, MAX_ACCOUNT_DRAWDOWN_PCT, MAX_CONSECUTIVE_LOSSES
    import os
    try:
        MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.005"))
    except ValueError:
        MAX_RISK_PER_TRADE_PCT = 0.005
    try:
        MAX_ACCOUNT_DRAWDOWN_PCT = float(os.getenv("MAX_ACCOUNT_DRAWDOWN_PCT", "0.10"))
    except ValueError:
        MAX_ACCOUNT_DRAWDOWN_PCT = 0.10
    try:
        MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
    except ValueError:
        MAX_CONSECUTIVE_LOSSES = 5


def strip_llm_sizing(decision: dict, cycle_run_id: Optional[int] = None) -> dict:
    """Remove LLM-provided executable sizing; log warning if present."""
    out = dict(decision)
    ignored = {}
    for key in LLM_SIZING_KEYS:
        if key in out and out[key] not in (None, "", 0, 0.0):
            ignored[key] = out.pop(key)
    if ignored:
        log.warning(
            "Ignored LLM-provided sizing; deterministic risk module used instead: %s",
            ignored,
        )
        ledger.increment_ignored_llm_sizing()
        if cycle_run_id:
            try:
                ledger.insert_risk_event(
                    cycle_run_id,
                    "warning",
                    "LLM_SIZING_IGNORED",
                    f"Ignored LLM sizing fields: {list(ignored.keys())}",
                    symbol=str(out.get("ticker") or ""),
                    raw=ignored,
                )
            except Exception as e:
                log.debug("[RiskManager] Could not record LLM sizing ignore event: %s", e)
    return out


def calculate_position_size(
    *,
    symbol: str,
    account_equity: float,
    available_cash: float,
    entry_price: float,
    stop_price: float,
    max_risk_pct: float = 0.005,
    max_position_pct: float | None = None,
    allow_fractional: bool = True,
) -> dict:
    """Only approved source of executable qty/notional."""
    refresh_risk_constants()
    if max_risk_pct <= 0:
        max_risk_pct = MAX_RISK_PER_TRADE_PCT

    if account_equity <= 0 or entry_price <= 0:
        return {
            "approved": False,
            "symbol": symbol,
            "qty": 0,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_dollars": 0.0,
            "estimated_notional": 0.0,
            "reason": "invalid equity or entry price",
        }

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return {
            "approved": False,
            "symbol": symbol,
            "qty": 0,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_dollars": 0.0,
            "estimated_notional": 0.0,
            "reason": "zero stop distance",
        }

    risk_dollars = account_equity * max_risk_pct
    raw_qty = risk_dollars / stop_distance
    if allow_fractional:
        qty = round(raw_qty, 6)
    else:
        qty = float(int(raw_qty))
        if qty <= 0 and raw_qty > 0:
            qty = 1.0

    estimated_notional = qty * entry_price

    if max_position_pct is not None and max_position_pct > 0:
        cap = account_equity * max_position_pct
        if estimated_notional > cap + 0.01:
            qty = cap / entry_price
            if not allow_fractional:
                qty = float(max(1, int(qty)))
            else:
                qty = round(qty, 6)
            estimated_notional = qty * entry_price

    allow_margin = cfg.ALLOW_MARGIN
    if not allow_margin and estimated_notional > available_cash + 0.01:
        if available_cash <= 0:
            return {
                "approved": False,
                "symbol": symbol,
                "qty": 0,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "risk_dollars": risk_dollars,
                "estimated_notional": estimated_notional,
                "reason": "insufficient cash (margin disabled)",
            }
        qty = available_cash / entry_price
        if not allow_fractional:
            qty = float(max(0, int(qty)))
        else:
            qty = round(qty, 2)
        estimated_notional = qty * entry_price
        if qty <= 0 or estimated_notional <= 0:
            return {
                "approved": False,
                "symbol": symbol,
                "qty": 0,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "risk_dollars": risk_dollars,
                "estimated_notional": 0.0,
                "reason": "cash too low for minimum position",
            }

    if qty <= 0:
        return {
            "approved": False,
            "symbol": symbol,
            "qty": 0,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_dollars": risk_dollars,
            "estimated_notional": 0.0,
            "reason": "computed quantity is zero",
        }

    return {
        "approved": True,
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "risk_dollars": round(risk_dollars, 2),
        "estimated_notional": round(estimated_notional, 2),
        "reason": f"risk-based size ({max_risk_pct * 100:.2f}% equity)",
    }


def size_by_risk(equity: float, entry_price: float, stop_price: float) -> Optional[int]:
    """Backward-compatible whole-share sizing helper."""
    result = calculate_position_size(
        symbol="",
        account_equity=equity,
        available_cash=equity,
        entry_price=entry_price,
        stop_price=stop_price,
        allow_fractional=False,
    )
    if not result["approved"]:
        return None
    return int(result["qty"]) or None


def _resolve_atr(decision: dict) -> float:
    atr = _float(decision.get("atr"))
    if atr > 0:
        return atr
    ohlcv = decision.get("ohlcv")
    if ohlcv:
        atr = calculate_atr(ohlcv)
        if atr > 0:
            return atr
    price = _float(decision.get("current_price") or decision.get("price"))
    atr_pct = _float(decision.get("atr_pct"))
    if price > 0 and atr_pct > 0:
        return round(price * atr_pct / 100.0, 6)
    return 0.0


def _max_position_pct(bucket) -> Optional[float]:
    if not bucket:
        return None
    alloc = _float(getattr(bucket, "allocation_pct", 0))
    pos_pct = _float(getattr(bucket, "max_position_pct", 0))
    if alloc > 0 and pos_pct > 0:
        return alloc * pos_pct
    return pos_pct or None


def compute_stop_price(decision: dict, bucket=None, cycle_run_id: Optional[int] = None) -> Optional[float]:
    """ATR stop with bucket flat-% fallback; logs fallback to risk_events."""
    price = _float(decision.get("current_price") or decision.get("price"))
    if price <= 0:
        return None

    atr = _resolve_atr(decision)
    fb_pct = _float(getattr(bucket, "stop_loss_pct", None), 0.07) if bucket else 0.07
    stop_result = calculate_atr_stop(
        side="buy",
        entry_price=price,
        atr=atr,
        fallback_stop_pct=fb_pct,
        bucket=bucket,
    )
    decision["stop_source"] = "fallback_flat_pct" if stop_result.get("used_fallback") else "atr"
    decision["atr"] = atr
    decision["atr_multiplier"] = stop_result.get("atr_multiplier")

    if stop_result.get("used_fallback") and cycle_run_id:
        ledger.insert_risk_event(
            cycle_run_id,
            "warning",
            "ATR_STOP_FALLBACK",
            stop_result.get("reason", "ATR unavailable; used flat bucket stop"),
            symbol=str(decision.get("ticker") or ""),
            raw={"atr": atr, "stop_price": stop_result.get("stop_price")},
        )
        log.warning(
            "[RiskManager] ATR fallback for %s: %s",
            decision.get("ticker"),
            stop_result.get("reason"),
        )

    if not stop_result.get("approved"):
        return None
    return stop_result.get("stop_price")


def _apply_take_profit(decision: dict, bucket=None, entry_price: float = 0, atr: float = 0) -> None:
    """Preserve bucket take-profit; log ATR recommendation if different."""
    if bucket and entry_price > 0:
        if not decision.get("take_profit_price"):
            tp_pct = _float(getattr(bucket, "take_profit_pct", 0))
            if tp_pct > 0:
                decision["take_profit_price"] = round(entry_price * (1 + tp_pct), 4)
                decision["take_profit_source"] = "bucket_pct"

    rec = calculate_atr_take_profit_recommendation(
        entry_price=entry_price,
        atr=atr,
        atr_multiplier=_float(decision.get("atr_multiplier"), 2.0),
    )
    if rec.get("recommended") and rec.get("take_profit_price"):
        decision["atr_take_profit_recommendation"] = rec["take_profit_price"]
        existing = _float(decision.get("take_profit_price"))
        if existing and abs(existing - rec["take_profit_price"]) > 0.01:
            log.info(
                "[RiskManager] %s ATR TP recommendation $%s (keeping bucket TP $%s)",
                decision.get("ticker"),
                rec["take_profit_price"],
                existing,
            )


def prepare_buy_order(
    decision: dict,
    account_snapshot: dict,
    bucket=None,
    cycle_run_id: Optional[int] = None,
    *,
    is_crypto: bool = False,
) -> tuple[bool, str, dict]:
    """
    Tier 2 pipeline: strip LLM sizing → ATR stop → deterministic size.
    Returns (approved, reason, adjusted_decision).
    """
    refresh_risk_constants()
    decision = strip_llm_sizing(decision, cycle_run_id)

    action = str(decision.get("action") or decision.get("decision", "SKIP")).upper()
    if action != "BUY":
        return True, "", decision

    if ledger.trading_paused():
        reason = ledger.get_pause_reason() or "TRADING_PAUSED=true"
        return False, "trading_paused", {"blocked_reason": reason}

    acct = account_snapshot.get("account") or account_snapshot
    equity = _float(acct.get("equity") or acct.get("portfolio_value"))
    cash = _float(acct.get("cash"))
    open_buy = _float(account_snapshot.get("open_buy_notional"))
    available_cash = max(cash - open_buy, 0) if not cfg.ALLOW_MARGIN else cash

    price = _float(decision.get("current_price") or decision.get("price"))
    if price <= 0:
        return False, "invalid_price", decision

    stop = compute_stop_price(decision, bucket, cycle_run_id)
    if not stop:
        return False, "invalid_stop", decision

    atr = _resolve_atr(decision)
    _apply_take_profit(decision, bucket, price, atr)

    size_result = calculate_position_size(
        symbol=decision.get("ticker", ""),
        account_equity=equity,
        available_cash=available_cash,
        entry_price=price,
        stop_price=stop,
        max_risk_pct=MAX_RISK_PER_TRADE_PCT,
        max_position_pct=_max_position_pct(bucket),
        allow_fractional=is_crypto,
    )
    decision["position_sizing_source"] = "deterministic"
    decision["risk_per_trade_pct"] = MAX_RISK_PER_TRADE_PCT
    decision["stop_loss_price"] = stop

    if not size_result["approved"]:
        if cycle_run_id:
            ledger.insert_risk_event(
                cycle_run_id,
                "warning",
                "POSITION_SIZE_REJECTED",
                size_result.get("reason", "sizing rejected"),
                symbol=str(decision.get("ticker") or ""),
            )
        return False, size_result.get("reason", "sizing_rejected"), decision

    if is_crypto:
        decision["notional_usd"] = size_result["estimated_notional"]
        decision.pop("shares", None)
    else:
        decision["shares"] = int(size_result["qty"])
        decision.pop("notional_usd", None)

    decision["estimated_notional"] = size_result["estimated_notional"]
    decision["risk_dollars"] = size_result["risk_dollars"]
    return True, "", decision


def _is_crypto_bucket(bucket) -> bool:
    if not bucket:
        return False
    return (
        getattr(bucket, "asset_class", "us_equity") == "crypto"
        or getattr(bucket, "mode", "") == "crypto"
    )


def evaluate_proposed_order(
    decision: dict,
    account_snapshot: dict,
    bucket=None,
    cycle_run_id: Optional[int] = None,
) -> tuple[bool, str, dict]:
    """Final deterministic check before order submission."""
    is_crypto = _is_crypto_bucket(bucket) if bucket else bool(decision.get("asset_class") == "crypto")
    ok, reason, adj = prepare_buy_order(
        decision, account_snapshot, bucket, cycle_run_id, is_crypto=is_crypto,
    )
    if not ok:
        return False, reason, adj

    action = str(adj.get("action", "SKIP")).upper()
    if action != "BUY":
        return True, "", adj

    acct = account_snapshot.get("account") or account_snapshot
    equity = _float(acct.get("equity") or acct.get("portfolio_value"))
    cash = _float(acct.get("cash"))
    open_buy = _float(account_snapshot.get("open_buy_notional"))
    allow_margin = cfg.ALLOW_MARGIN

    price = _float(adj.get("current_price") or adj.get("price"))
    stop = _float(adj.get("stop_loss_price"))
    shares = adj.get("shares")
    notional = _float(adj.get("notional_usd"))
    order_notional = notional if notional else int(shares or 0) * price

    projected = cash - open_buy - order_notional
    if not allow_margin and not cfg.ALLOW_NEGATIVE_CASH:
        if cash <= 0:
            return False, "insufficient_live_cash", {"cash": cash, "order_notional": order_notional}
        if projected < cfg.MIN_CASH_RESERVE:
            return False, "below_min_cash_reserve", {"projected_cash": projected}
        if projected < 0:
            return False, "insufficient_live_cash", {"projected_cash": projected}

    max_risk = equity * MAX_RISK_PER_TRADE_PCT
    if shares and price and stop:
        risk_at_stop = abs(price - stop) * int(shares)
        if risk_at_stop > max_risk * 1.05:
            return False, "exceeds_max_risk_per_trade", {"risk": risk_at_stop, "max": max_risk}

    return True, "", adj


def evaluate_batch(
    decisions: list,
    account_snapshot: dict,
    bucket=None,
    cycle_run_id: Optional[int] = None,
) -> list:
    """Filter approved BUY decisions through Tier 2 deterministic risk."""
    approved = []
    for d in decisions:
        if str(d.get("action", "SKIP")).upper() != "BUY":
            continue
        ok, reason, adj = evaluate_proposed_order(d, account_snapshot, bucket, cycle_run_id)
        if ok:
            approved.append(adj)
        else:
            log.warning("[RiskManager] Rejected %s: %s", d.get("ticker"), reason)
            d["blocked_reason"] = reason
            if isinstance(adj, dict) and adj.get("blocked_reason"):
                d["blocked_reason"] = adj["blocked_reason"] or reason
    return approved


def _is_consecutive_loss_pause_reason(reason: Optional[str]) -> bool:
    text = (reason or "").lower()
    return "consecutive loss" in text or text == ""


def get_consecutive_loss_status(limit: int = 20) -> dict:
    """Single source of truth — completed_trades only."""
    return ledger.get_consecutive_loss_status(limit=limit)


def count_consecutive_losses_from_fills(limit: int = 100) -> int:
    """Deprecated alias — returns deterministic completed_trades count only."""
    return get_consecutive_loss_status(limit=limit).get("count", 0)


def check_consecutive_loss_pause(db=None, max_losses: int = None) -> tuple[bool, str]:
    """
    If consecutive losses >= threshold, set TRADING_PAUSED and block new buys.
    Returns (paused, reason). Protective sells are not blocked here.

    Pause enforcement uses completed_trades only. When the ledger is empty,
    no pause is set and any stale consecutive-loss pause is cleared.
    """
    refresh_risk_constants()
    threshold = max_losses if max_losses is not None else MAX_CONSECUTIVE_LOSSES
    status = get_consecutive_loss_status(limit=max(threshold * 4, 20))

    if not status.get("ledger_complete"):
        msg = status.get("message", "ledger incomplete")
        if ledger.trading_paused() and _is_consecutive_loss_pause_reason(ledger.get_pause_reason()):
            reset_trading_pause("Cleared stale pause — ledger incomplete (no completed_trades)")
        log.warning("[RiskManager] %s — consecutive-loss pause not enforced", msg)
        return False, msg

    consecutive = int(status.get("count", 0))

    if ledger.trading_paused():
        reason = ledger.get_pause_reason() or "Consecutive loss limit reached"
        if _is_consecutive_loss_pause_reason(reason) and consecutive < threshold:
            reset_trading_pause(
                f"Auto-cleared: deterministic count={consecutive} < threshold={threshold}"
            )
            return False, ""
        return True, reason

    if consecutive >= threshold:
        reason = "Consecutive loss limit reached"
        ledger.set_system_flag("TRADING_PAUSED", "true")
        ledger.set_system_flag("PAUSE_REASON", reason)
        ledger.insert_risk_event(
            None,
            "critical",
            "CONSECUTIVE_LOSS_PAUSE",
            f"{consecutive} consecutive losses (limit {threshold})",
            raw={"consecutive_losses": consecutive, "max_losses": threshold},
        )
        log.warning("[RiskManager] TRADING_PAUSED: %s (%d losses)", reason, consecutive)
        return True, reason

    return False, ""


def reset_trading_pause(reason: str = "Manual pause reset") -> None:
    """Clear TRADING_PAUSED flags and log risk event."""
    ledger.set_system_flag("TRADING_PAUSED", "false")
    ledger.set_system_flag("PAUSE_REASON", "")
    ledger.insert_risk_event(
        None,
        "info",
        "TRADING_PAUSE_RESET",
        reason,
    )
    log.info("[RiskManager] Trading pause cleared: %s", reason)
