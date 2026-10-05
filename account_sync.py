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
# from nested escape bloat, and later cycles refused to write once screener
# mention dumps and duplicated funnel blobs passed 8 MiB — leaving the
# dashboard on the last successful SKIP snapshot. Writers prune those bodies
# first (see agenttrade.state_size). The default cap stays 8 MiB. Override
# with AGENT_STATE_MAX_BYTES; values above the hard ceiling are clamped so a
# workaround cannot recreate a multi-hundred-megabyte projection.
# Refusing a projection does not roll back SQLite rows already committed.
DEFAULT_AGENT_STATE_MAX_BYTES = 8 * 1024 * 1024
HARD_AGENT_STATE_MAX_BYTES = 32 * 1024 * 1024


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


def _count_trades_today(fills: list, open_orders: list, now=None) -> int:
    """Distinct orders in the America/Chicago trading day.

    Partial fills share an order id. Cancelled, failed, and skipped orders
    are ignored, as are protective bracket sell legs.
    """
    from trading_day import count_trades_for_day

    return count_trades_for_day(fills, open_orders, now=now)


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
    if value > HARD_AGENT_STATE_MAX_BYTES:
        log.warning(
            "[State] AGENT_STATE_MAX_BYTES=%d exceeds hard ceiling %d; clamping",
            value,
            HARD_AGENT_STATE_MAX_BYTES,
        )
        return HARD_AGENT_STATE_MAX_BYTES
    return value


def dumps_dashboard_projection(state: dict) -> Optional[str]:
    """Serialize a pruned dashboard projection, or None if it still exceeds the cap.

    Bulky screener/LLM bodies are removed first so a normal cycle stays under
    the cap and the dashboard receives this cycle's decisions, positions, and
    fills. Callers must skip both JSON replaces when this returns None.
    SQLite is not touched here, and a refusal does not roll back ledger rows
    the cycle already committed.
    """
    from agenttrade.state_size import fit_dashboard_projection

    limit = agent_state_max_bytes()
    payload, info = fit_dashboard_projection(state, limit)
    if payload is None:
        log.error(
            "[State] Refusing agent_state.json write: pruned projection is "
            "%d bytes, above AGENT_STATE_MAX_BYTES=%d. Existing projection "
            "left in place. SQLite rows already committed this cycle are not "
            "rolled back.",
            int(info.get("bytes") or 0),
            limit,
        )
        return None
    if info.get("shed"):
        log.warning(
            "[State] Dropped optional dashboard sections to fit %d-byte cap: %s",
            limit,
            ", ".join(info["shed"]),
        )
    elif not info.get("pretty"):
        log.info(
            "[State] Wrote compact agent_state.json (%d bytes, cap %d)",
            info.get("bytes"),
            limit,
        )
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
    """NON-AUTHORITATIVE: read dashboard projection cache if present.

    A file already over the cap is ignored. The next successful publish
    replaces it, so a bloated on-disk cache does not have to be truncated
    by hand and is not merged back into the following cycle.
    """
    try:
        size = os.path.getsize(cfg.STATE_FILE)
    except OSError:
        return {}
    limit = agent_state_max_bytes()
    if size > limit:
        log.error(
            "[State] Ignoring on-disk agent_state.json (%d bytes > %d). "
            "The next successful publish replaces it with a pruned projection.",
            size,
            limit,
        )
        return {}
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
