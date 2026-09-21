"""Mandatory Reconciliation Gate — Alpaca vs SQLite before any trading (Tier 1)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import agent_config as cfg
from account_sync import refresh_alpaca_snapshot
from agenttrade import db as ledger

log = logging.getLogger(__name__)

MARGIN_TOLERANCE = 0.01
DRAWDOWN_PCT = 0.10

REQUIRED_ACCOUNT_FIELDS = (
    "cash", "equity", "buying_power", "portfolio_value",
    "long_market_value", "short_market_value", "multiplier",
)


@dataclass
class ReconciliationResult:
    passed: bool
    status: str
    message: str
    differences: dict = field(default_factory=dict)
    snapshot: Optional[dict] = None
    reconciliation_id: Optional[int] = None
    # Backward-compatible helpers for dashboard/cycle
    buys_allowed: bool = True
    sells_allowed: bool = True
    buy_block_message: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _allow_margin() -> bool:
    return cfg.ALLOW_MARGIN and cfg.env_bool("ALLOW_MARGIN", "false")


def _allow_shorts() -> bool:
    return cfg.env_bool("ALLOW_SHORTS", "false")


def _float(value, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _persist_alpaca_snapshot(cycle_run_id: int, snapshot: dict) -> None:
    account = snapshot.get("account") or {}
    ledger.insert_account_snapshot(cycle_run_id, "alpaca_reconciliation", account)
    ledger.insert_positions(cycle_run_id, "alpaca", snapshot.get("positions") or [])
    ledger.insert_orders(cycle_run_id, snapshot.get("open_orders") or [])
    ledger.insert_fills_from_alpaca(cycle_run_id, snapshot.get("recent_fills") or [])


def sync_ledger_from_alpaca(cycle_run_id: int, source: str = "post_trade") -> dict:
    """Refresh Alpaca and write snapshots to SQLite (post-order sync)."""
    snapshot = refresh_alpaca_snapshot()
    account = snapshot.get("account") or {}
    ledger.insert_account_snapshot(cycle_run_id, source, account)
    ledger.insert_positions(cycle_run_id, "alpaca", snapshot.get("positions") or [])
    ledger.insert_orders(cycle_run_id, snapshot.get("open_orders") or [])
    ledger.insert_fills_from_alpaca(cycle_run_id, snapshot.get("recent_fills") or [])
    log.info(
        "[Reconciliation] Ledger sync (%s) equity=$%s cash=$%s",
        source, account.get("equity"), account.get("cash"),
    )
    return snapshot


def _record_position_drift(positions: list, cycle_run_id: int, differences: dict) -> None:
    prior = ledger.get_latest_positions()
    if not prior:
        return
    alpaca_map = {p.get("symbol"): _float(p.get("qty"), 0) for p in positions}
    prior_map = {
        p.get("symbol"): _float(p.get("qty"), 0)
        for p in prior
        if p.get("cycle_run_id") != cycle_run_id
    }
    drift = {}
    for sym, qty in alpaca_map.items():
        old = prior_map.get(sym)
        if old is not None and abs(old - qty) > 0.0001:
            drift[sym] = {"sqlite_qty": old, "alpaca_qty": qty}
    if drift:
        differences["position_drift"] = drift
        log.warning("[Reconciliation] Position drift (Alpaca wins): %s", drift)


def _init_equity_flags(equity: float) -> None:
    if not ledger.get_system_flag("STARTING_EQUITY"):
        ledger.set_system_flag("STARTING_EQUITY", str(equity))
    if not ledger.get_system_flag("HIGH_WATER_EQUITY"):
        ledger.set_system_flag("HIGH_WATER_EQUITY", str(equity))
    if ledger.get_system_flag("TRADING_HALTED") is None:
        ledger.set_system_flag("TRADING_HALTED", "false")


def _update_high_water(equity: float) -> float:
    hw = _float(ledger.get_system_flag("HIGH_WATER_EQUITY"), equity) or equity
    if equity > hw:
        ledger.set_system_flag("HIGH_WATER_EQUITY", str(equity))
        return equity
    return hw


def _halt(reason: str) -> None:
    ledger.set_system_flag("TRADING_HALTED", "true")
    ledger.set_system_flag("HALT_REASON", reason)


def _fail(
    cycle_run_id: int,
    started: str,
    status: str,
    message: str,
    differences: dict,
    snapshot: Optional[dict] = None,
    *,
    halt: bool = False,
    halt_reason: str = "",
) -> ReconciliationResult:
    if halt and halt_reason:
        _halt(halt_reason)
    finished = _utc_now()
    recon_id = ledger.insert_reconciliation_run(
        cycle_run_id, started, finished, status, False, differences, message,
    )
    ledger.insert_risk_event(
        cycle_run_id, "critical", "RECONCILIATION_FAILED", message,
    )
    log.error("[Reconciliation] FAILED (%s): %s", status, message)
    return ReconciliationResult(
        passed=False,
        status=status,
        message=message,
        differences=differences,
        snapshot=snapshot,
        reconciliation_id=recon_id,
        buys_allowed=False,
        sells_allowed=False,
        buy_block_message=message,
    )


def _pass(
    cycle_run_id: int,
    started: str,
    differences: dict,
    snapshot: dict,
) -> ReconciliationResult:
    finished = _utc_now()
    recon_id = ledger.insert_reconciliation_run(
        cycle_run_id, started, finished, "passed", True, differences, "Reconciliation gate passed",
    )
    log.info("[Reconciliation] PASSED")
    return ReconciliationResult(
        passed=True,
        status="passed",
        message="Reconciliation gate passed",
        differences=differences,
        snapshot=snapshot,
        reconciliation_id=recon_id,
        buys_allowed=True,
        sells_allowed=True,
    )


def run_reconciliation_gate(
    cycle_run_id: int,
    mode: str,
    alpaca_client=None,
) -> ReconciliationResult:
    """
    Fetch Alpaca truth, persist to SQLite, verify Tier 1 safety checks.
    Must pass before screener, research, analysis, sentiment, risk, or execution.
    """
    started = _utc_now()
    differences: dict[str, Any] = {}

    try:
        snapshot = refresh_alpaca_snapshot()
    except Exception as e:
        ledger.insert_risk_event(
            cycle_run_id, "critical", "RECONCILIATION_FAILED",
            f"Alpaca fetch failed: {e}",
        )
        return _fail(cycle_run_id, started, "alpaca_error", str(e), differences)

    account = dict(snapshot.get("account") or {})
    positions = snapshot.get("positions") or []
    open_orders = snapshot.get("open_orders") or []

    _persist_alpaca_snapshot(cycle_run_id, snapshot)

    # Mode consistency
    expected_paper = cfg.ALPACA_PAPER
    is_paper_endpoint = "paper-api" in cfg.ALPACA_BASE_URL
    if expected_paper != is_paper_endpoint:
        differences["mode_mismatch"] = {
            "expected_paper": expected_paper,
            "endpoint": cfg.ALPACA_BASE_URL,
            "mode": mode,
        }
        return _fail(
            cycle_run_id, started, "mode_mismatch",
            "Paper/live config does not match Alpaca endpoint",
            differences, snapshot,
        )

    # Required account fields
    missing = []
    parsed: dict[str, float] = {}
    for field_name in REQUIRED_ACCOUNT_FIELDS:
        raw = account.get(field_name)
        if field_name == "multiplier" and raw is None:
            raw = 1
        val = _float(raw)
        if val is None:
            missing.append(field_name)
        else:
            parsed[field_name] = val
            account[field_name] = val

    if missing:
        differences["missing_fields"] = missing
        return _fail(
            cycle_run_id, started, "missing_account_fields",
            f"Required Alpaca account fields missing: {', '.join(missing)}",
            differences, snapshot,
        )

    cash = parsed["cash"]
    equity = parsed["equity"]
    buying_power = parsed["buying_power"]
    long_mv = parsed["long_market_value"]
    short_mv = parsed["short_market_value"]
    multiplier = parsed["multiplier"]
    allow_margin = _allow_margin()
    allow_shorts = _allow_shorts()

    if long_mv <= 0:
        long_mv = sum(
            _float(p.get("market_value"), 0) or 0
            for p in positions
            if (_float(p.get("qty"), 0) or 0) > 0
        )
        parsed["long_market_value"] = long_mv

    differences["account"] = parsed
    margin_detected = (
        cash < -MARGIN_TOLERANCE
        or long_mv > equity + MARGIN_TOLERANCE
        or short_mv > MARGIN_TOLERANCE
        or multiplier > 1.0 + MARGIN_TOLERANCE
    )
    differences["margin_detected"] = margin_detected

    if multiplier > 1.0 + MARGIN_TOLERANCE:
        log.warning("[Reconciliation] Alpaca multiplier=%s (margin account signal)", multiplier)

    _record_position_drift(positions, cycle_run_id, differences)

    # Initialize / update equity flags
    _init_equity_flags(equity)
    high_water = _update_high_water(equity)

    # In normal cycle modes, a pre-existing halt is a hard stop.
    # In dashboard_action mode the operator is explicitly asking us to re-evaluate
    # whether the account is healthy enough to lift the halt — skip the early exit
    # so the full account check can run and potentially clear the flag.
    if ledger.trading_halted() and mode != "dashboard_action":
        reason = ledger.get_system_flag("HALT_REASON") or "TRADING_HALTED=true"
        return _fail(
            cycle_run_id, started, "trading_halted", reason, differences, snapshot,
        )

    # Drawdown circuit breaker (high-water mark)
    try:
        max_dd = float(os.getenv("MAX_ACCOUNT_DRAWDOWN_PCT", str(DRAWDOWN_PCT)))
    except ValueError:
        max_dd = DRAWDOWN_PCT
    if high_water > 0:
        dd = (high_water - equity) / high_water
        differences["drawdown_pct"] = round(dd * 100, 2)
        differences["high_water_equity"] = high_water
        if dd >= max_dd:
            reason = f"Portfolio drawdown exceeded {max_dd*100:.0f}%"
            return _fail(
                cycle_run_id, started, "drawdown", reason, differences, snapshot,
                halt=True, halt_reason=reason,
            )

    # No-margin hard blocks (fail closed)
    if not allow_margin:
        if cash < -MARGIN_TOLERANCE and not cfg.ALLOW_NEGATIVE_CASH:
            reason = f"MARGIN DETECTED: cash ${cash:,.2f} is negative"
            return _fail(
                cycle_run_id, started, "negative_cash", reason, differences, snapshot,
                halt=True, halt_reason=reason,
            )
        if long_mv > equity + MARGIN_TOLERANCE:
            reason = (
                f"MARGIN DETECTED: long market value ${long_mv:,.2f} "
                f"exceeds equity ${equity:,.2f}"
            )
            return _fail(
                cycle_run_id, started, "leveraged_long", reason, differences, snapshot,
                halt=True, halt_reason=reason,
            )

    if not allow_shorts and short_mv > MARGIN_TOLERANCE:
        reason = f"Short exposure ${short_mv:,.2f} while ALLOW_SHORTS=false"
        return _fail(
            cycle_run_id, started, "shorts_disabled", reason, differences, snapshot,
            halt=True, halt_reason=reason,
        )

    # Open-order sanity
    buy_by_symbol: dict[str, list] = {}
    for o in open_orders:
        if str(o.get("side", "")).lower() != "buy":
            continue
        sym = o.get("symbol")
        if sym:
            buy_by_symbol.setdefault(sym, []).append(o)
    dup_buys = {k: len(v) for k, v in buy_by_symbol.items() if len(v) > 1}
    if dup_buys:
        differences["duplicate_open_buys"] = dup_buys
        return _fail(
            cycle_run_id, started, "duplicate_open_buys",
            f"Duplicate open buy orders: {dup_buys}",
            differences, snapshot,
        )

    open_buy_notional = _float(snapshot.get("open_buy_notional"), 0) or 0
    if cash < -MARGIN_TOLERANCE and open_buy_notional > 0:
        differences["open_buy_notional"] = open_buy_notional
        return _fail(
            cycle_run_id, started, "open_buys_while_negative_cash",
            "Open buy orders exist while cash is negative",
            differences, snapshot,
            halt=True, halt_reason="MARGIN DETECTED: open buys with negative cash",
        )

    if not allow_margin and open_buy_notional > cash + MARGIN_TOLERANCE and cash >= -MARGIN_TOLERANCE:
        differences["open_buy_notional"] = open_buy_notional
        differences["cash"] = cash
        return _fail(
            cycle_run_id, started, "open_buys_exceed_cash",
            f"Open buy notional ${open_buy_notional:,.2f} exceeds cash ${cash:,.2f}",
            differences, snapshot,
        )

    differences["buying_power"] = buying_power
    return _pass(cycle_run_id, started, differences, snapshot)
