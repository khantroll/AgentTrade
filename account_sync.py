"""Live Alpaca account snapshot — single source of truth for dashboard and buy guards."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

import agent_config as cfg
from alpaca_client import alpaca_get, get_account, get_positions, get_recent_fills

log = logging.getLogger(__name__)

EQUITY_MISMATCH_THRESHOLD = 5.0

# Dashboard projection cap. The public agent_state.json once grew to ~1.4GB
# from nested escape bloat. Refuse that write instead of replacing the file.
# Default is 8 MiB — a few megabytes, not gigabytes. Override with
# AGENT_STATE_MAX_BYTES. This does not delete or rewrite the SQLite ledger.
# A multi-gigabyte sqlite file that OOMs the host is a separate ops follow-up.
DEFAULT_AGENT_STATE_MAX_BYTES = 8 * 1024 * 1024


def _fetch_open_orders(limit: int = 200) -> list:
    """Open orders via alpaca_get — avoids requiring get_open_orders on stale deploys."""
    data = alpaca_get(f"/v2/orders?status=open&limit={limit}")
    return data if isinstance(data, list) else []


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _order_notional(order: dict) -> float:
    """Estimate notional reserved by an open order."""
    if order.get("notional"):
        return _float(order["notional"])
    qty = _float(order.get("qty") or order.get("filled_qty"))
    if qty <= 0:
        return 0.0
    for key in ("limit_price", "stop_price", "filled_avg_price"):
        px = _float(order.get(key))
        if px > 0:
            return qty * px
    return 0.0


def _count_trades_today(fills: list, open_orders: list) -> int:
    """Count today's fill events + open orders submitted today (deduped by order_id)."""
    today = datetime.now().strftime("%Y-%m-%d")
    seen = set()
    count = 0
    for fill in fills or []:
        ts = str(fill.get("submitted_at") or fill.get("transaction_time") or "")
        if ts[:10] != today:
            continue
        oid = fill.get("order_id") or fill.get("id")
        key = oid or f"fill|{ts}|{fill.get('ticker')}"
        if key in seen:
            continue
        seen.add(key)
        count += 1
    for order in open_orders or []:
        ts = str(order.get("submitted_at") or order.get("created_at") or "")
        if ts[:10] != today:
            continue
        oid = order.get("id") or order.get("order_id")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        count += 1
    return count


def refresh_alpaca_snapshot() -> dict:
    """
    Fetch live Alpaca account truth.

    Returns snapshot with account fields, positions, open orders, recent fills,
    derived metrics, and sanity-check flags.
    """
    account = get_account()
    positions = get_positions()
    open_orders = _fetch_open_orders()
    recent_fills = get_recent_fills(50)

    cash = _float(account.get("cash"))
    equity = _float(account.get("equity") or account.get("portfolio_value"))
    portfolio_value = _float(account.get("portfolio_value") or equity)
    buying_power = _float(account.get("buying_power"))

    open_positions_value = sum(_float(p.get("market_value")) for p in positions)
    computed_equity = cash + open_positions_value
    mismatch_delta = round(equity - computed_equity, 2)
    equity_mismatch = abs(mismatch_delta) > EQUITY_MISMATCH_THRESHOLD

    open_buy_notional = sum(
        _order_notional(o) for o in open_orders if str(o.get("side", "")).lower() == "buy"
    )
    open_sell_qty = sum(
        1 for o in open_orders if str(o.get("side", "")).lower() == "sell"
    )

    now = datetime.now().isoformat()
    daily_trades_live = _count_trades_today(recent_fills, open_orders)

    snapshot = {
        "alpaca_sync_at": now,
        "account": {
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "buying_power": round(buying_power, 2),
            "portfolio_value": round(portfolio_value, 2),
            "long_market_value": round(_float(account.get("long_market_value")), 2),
            "short_market_value": round(_float(account.get("short_market_value")), 2),
            "multiplier": _float(account.get("multiplier"), 1.0),
        },
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "portfolio_value": round(portfolio_value, 2),
        "open_positions_value": round(open_positions_value, 2),
        "computed_equity": round(computed_equity, 2),
        "equity_mismatch": equity_mismatch,
        "equity_mismatch_delta": mismatch_delta,
        "equity_mismatch_message": (
            "Equity mismatch: dashboard state does not match Alpaca."
            if equity_mismatch else None
        ),
        "open_buy_notional": round(open_buy_notional, 2),
        "open_sell_orders": open_sell_qty,
        "positions": positions,
        "open_orders": open_orders,
        "recent_fills": recent_fills,
        "daily_trades_live": daily_trades_live,
        "positions_count": len(positions),
    }
    return snapshot


def merge_snapshot_into_state(state: dict, snapshot: dict, *, source: str = "cycle") -> dict:
    """Overlay live Alpaca fields onto agent state dict."""
    state = dict(state or {})
    acct = snapshot.get("account") or {}

    state["portfolio_value"] = acct.get("portfolio_value", snapshot.get("portfolio_value"))
    state["equity"] = acct.get("equity", snapshot.get("equity"))
    state["cash"] = acct.get("cash", snapshot.get("cash"))
    state["buying_power"] = acct.get("buying_power", snapshot.get("buying_power"))
    state["open_positions_value"] = snapshot.get("open_positions_value")
    state["computed_equity"] = snapshot.get("computed_equity")
    state["equity_mismatch"] = snapshot.get("equity_mismatch", False)
    state["equity_mismatch_delta"] = snapshot.get("equity_mismatch_delta")
    state["equity_mismatch_message"] = snapshot.get("equity_mismatch_message")
    state["open_buy_notional"] = snapshot.get("open_buy_notional")
    state["positions"] = snapshot.get("positions") or []
    state["open_orders"] = snapshot.get("open_orders") or []
    state["recent_fills"] = snapshot.get("recent_fills") or []
    state["daily_trades_live"] = snapshot.get("daily_trades_live", 0)
    state["last_alpaca_sync_at"] = snapshot.get("alpaca_sync_at")
    state["alpaca_account"] = acct

    if snapshot.get("daily_trades_live") is not None:
        state["daily_trades"] = snapshot["daily_trades_live"]

    now = datetime.now().isoformat()
    state["last_state_refresh_at"] = now
    if source == "cycle":
        state["last_trading_cycle_at"] = now
        state["last_run"] = now
    elif source == "monitor":
        state["last_monitor_at"] = now

    return state


def agent_state_max_bytes() -> int:
    """Configured max UTF-8 size for agent_state.json. Default 8 MiB."""
    raw = os.getenv("AGENT_STATE_MAX_BYTES")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_AGENT_STATE_MAX_BYTES
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning(
            "[State] Invalid AGENT_STATE_MAX_BYTES=%r; using default %d",
            raw,
            DEFAULT_AGENT_STATE_MAX_BYTES,
        )
        return DEFAULT_AGENT_STATE_MAX_BYTES
    if value < 1:
        return DEFAULT_AGENT_STATE_MAX_BYTES
    return value


def dumps_dashboard_projection(state: dict) -> Optional[str]:
    """Serialize the dashboard projection, or None if it exceeds the write cap.

    Callers must skip both the private agent_state.json replace and the public
    copy when this returns None. SQLite is not touched here.
    """
    payload = json.dumps(state, indent=2)
    size = len(payload.encode("utf-8"))
    limit = agent_state_max_bytes()
    if size > limit:
        log.error(
            "[State] Refusing agent_state.json write: %d bytes exceeds "
            "AGENT_STATE_MAX_BYTES=%d. Existing projection left in place. "
            "SQLite ledger was not modified.",
            size,
            limit,
        )
        return None
    return payload


def atomic_write_text(path: str, payload: str, chmod: Optional[int] = None) -> None:
    """Atomically replace path with payload. Temp file is created beside path."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="agent_state_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
        if chmod is not None:
            os.chmod(path, chmod)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def load_state_file() -> dict:
    """NON-AUTHORITATIVE: read dashboard projection cache if present."""
    try:
        with open(cfg.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state_file(state: dict) -> None:
    """Persist state to backend + public dashboard copy."""
    payload = dumps_dashboard_projection(state)
    if payload is None:
        return
    atomic_write_text(cfg.STATE_FILE, payload)
    try:
        atomic_write_text(cfg.PUBLIC_STATE_FILE, payload, chmod=0o644)
    except OSError as e:
        log.warning("[AccountSync] Could not publish state: %s", e)


def refresh_and_persist_state(existing: Optional[dict] = None, *, source: str = "cycle") -> dict:
    """Refresh Alpaca snapshot and write a disposable dashboard projection cache."""
    snapshot = refresh_alpaca_snapshot()
    state = merge_snapshot_into_state(existing or load_state_file(), snapshot, source=source)
    save_state_file(state)
    log.info(
        "[AccountSync] Live account: equity=$%s cash=$%s buying_power=$%s positions=$%s",
        state.get("equity"),
        state.get("cash"),
        state.get("buying_power"),
        state.get("open_positions_value"),
    )
    if state.get("equity_mismatch"):
        log.warning(
            "[AccountSync] %s (delta=$%s)",
            state.get("equity_mismatch_message"),
            state.get("equity_mismatch_delta"),
        )
    return state


def get_live_state() -> dict:
    """Live state for dashboard API — SQLite ledger + Alpaca + legacy funnel cache."""
    from agenttrade.publish import build_dashboard_state
    return build_dashboard_state()
