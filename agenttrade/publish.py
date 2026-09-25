"""Publish dashboard-compatible state from SQLite ledger."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Optional

import agent_config as cfg
from account_sync import load_state_file, merge_snapshot_into_state, refresh_alpaca_snapshot
from agenttrade import db as ledger

log = logging.getLogger(__name__)


def _pipeline_totals(screener_sources: dict) -> dict:
    totals = {}
    for _bucket, info in (screener_sources or {}).items():
        if not isinstance(info, dict):
            continue
        for key in ("momentum", "movers", "reddit", "news", "congress", "tradingview", "crypto"):
            val = info.get(key)
            if val:
                totals[key] = totals.get(key, 0) + int(val)
    return totals


def _funnel_signal_attributions(cached: dict) -> list:
    """Extract attribution rows embedded in screener_sources (equity buckets)."""
    rows = []
    seen: set[str] = set()
    for bucket, sources in (cached.get("screener_sources") or {}).items():
        if not isinstance(sources, dict):
            continue
        attr_map = sources.get("attribution")
        if not isinstance(attr_map, dict):
            continue
        for sym, row in attr_map.items():
            sym_u = str(sym).upper()
            if sym_u in seen or not isinstance(row, dict):
                continue
            seen.add(sym_u)
            rows.append({
                "symbol": sym_u,
                "total_score": row.get("total_score"),
                "components": row.get("components"),
                "pipelines": row.get("pipelines"),
                "bucket": bucket,
                "source": "screener_funnel",
            })
    return rows


def _merge_signal_attributions(ledger_rows: list, funnel_rows: list) -> list:
    """Prefer SQLite rows; supplement symbols present only in funnel cache."""
    if not funnel_rows:
        return ledger_rows or []
    ledger_syms = {str(r.get("symbol", "")).upper() for r in (ledger_rows or [])}
    merged = list(ledger_rows or [])
    for row in funnel_rows:
        if row.get("symbol") not in ledger_syms:
            merged.append(row)
    return merged


def _log_signals_publish_summary(state: dict, cached: dict) -> None:
    ss = state.get("screener_sources") or cached.get("screener_sources") or {}
    log.info(
        "[Publish] Signals dashboard — attributions=%d reddit_trends=%d "
        "screener_buckets=%s pipeline_totals=%s ledger_complete=%s",
        len(state.get("signal_attributions") or []),
        len(state.get("reddit_trends") or []),
        list(ss.keys()) if isinstance(ss, dict) else [],
        _pipeline_totals(ss if isinstance(ss, dict) else {}),
        state.get("ledger_complete"),
    )


def build_dashboard_state(cached_funnel: Optional[dict] = None, live_snapshot: Optional[dict] = None) -> dict:
    """
    Merge SQLite ledger + live Alpaca + projection-cache compatibility data.
    Trading/account numbers come from SQLite/Alpaca, not stale JSON.
    """
    cached = cached_funnel or load_state_file()
    _alpaca_ok = True
    _alpaca_error: Optional[str] = None
    if live_snapshot is not None:
        snapshot = live_snapshot
        _alpaca_ok = bool(snapshot.get("account") or snapshot.get("positions") or snapshot.get("open_orders"))
    else:
        try:
            snapshot = refresh_alpaca_snapshot()
        except Exception as _snap_err:
            _alpaca_error = str(_snap_err)
            log.warning("[Publish] Alpaca unavailable — serving SQLite-only state: %s", _alpaca_error)
            snapshot = {}
            _alpaca_ok = False
    ledger_data = ledger.get_dashboard_ledger()
    sqlite_funnel = ledger.get_latest_funnel()
    persisted_funnel = sqlite_funnel.get("funnel") or {}
    persisted_artifacts = sqlite_funnel.get("artifacts") or {}

    snapshot = dict(snapshot or {})
    if not snapshot.get("account"):
        acct_from_ledger = ledger.account_row_as_snapshot(ledger_data.get("account_snapshot"))
        if acct_from_ledger:
            snapshot["account"] = acct_from_ledger
            snapshot.setdefault("equity", acct_from_ledger.get("equity"))
            snapshot.setdefault("cash", acct_from_ledger.get("cash"))
            snapshot.setdefault("buying_power", acct_from_ledger.get("buying_power"))
            snapshot.setdefault("portfolio_value", acct_from_ledger.get("portfolio_value"))
    if not snapshot.get("positions"):
        snapshot["positions"] = ledger.positions_as_dashboard(ledger_data.get("positions") or [])
    if not snapshot.get("open_orders"):
        snapshot["open_orders"] = ledger_data.get("open_orders") or []

    state = merge_snapshot_into_state(cached, snapshot, source="dashboard")
    state["ledger_source"] = "sqlite"
    state["broker_source"] = "alpaca"
    state["state_cache_role"] = "projection_only"
    state["funnel_source"] = "sqlite" if sqlite_funnel.get("cycle_run_id") else "legacy_cache"
    state["alpaca_reachable"] = _alpaca_ok
    if _alpaca_error:
        state["alpaca_error"] = _alpaca_error

    acct = snapshot.get("account") or {}
    acct_row = ledger_data.get("account_snapshot") or {}
    if acct_row:
        state["portfolio_value"] = acct_row.get("portfolio_value")
        state["equity"] = acct_row.get("equity")
        state["cash"] = acct_row.get("cash")
        state["buying_power"] = acct_row.get("buying_power")

    state["account_cash"] = acct.get("cash", state.get("cash"))
    state["account_equity"] = acct.get("equity", state.get("equity"))
    state["account_buying_power"] = acct.get("buying_power", state.get("buying_power"))
    # long_market_value: use account field when present; otherwise derive from
    # position market values (Alpaca sometimes omits it in cash accounts).
    _positions_for_lmv = snapshot.get("positions") or []
    _acct_long_mv = acct.get("long_market_value")
    if _acct_long_mv is None and _positions_for_lmv:
        try:
            _pos_mv_total = 0.0
            for _p in _positions_for_lmv:
                if str(_p.get("side", "long")).lower() == "short":
                    continue
                _mv = float(_p.get("market_value") or 0)
                if not _mv:
                    _mv = float(_p.get("current_price") or 0) * float(_p.get("qty") or 0)
                _pos_mv_total += _mv
            _acct_long_mv = round(_pos_mv_total, 2) if _pos_mv_total > 0 else None
        except Exception:
            pass
    state["long_market_value"] = _acct_long_mv
    state["short_market_value"] = acct.get("short_market_value")
    state["open_positions"] = ledger_data.get("positions") or snapshot.get("positions") or []

    # ── Sync Alpaca open orders into SQLite ──────────────────────────────────
    # Pull broker-side open orders from the snapshot (already fetched by
    # refresh_alpaca_snapshot) and upsert them into the local orders table so
    # protective stop orders are always visible in the ledger.
    _alpaca_open_orders = snapshot.get("open_orders") or []
    if _alpaca_open_orders and _alpaca_ok:
        try:
            ledger.sync_open_orders_from_alpaca(_alpaca_open_orders)
        except Exception as _oo_err:
            log.debug("[Publish] sync_open_orders_from_alpaca: %s", _oo_err)

    # Merge broker-side stops with durable SQLite manual overrides.
    # Manual operator-set stops win over broker-derived values for display.
    _broker_stops = ledger.extract_stop_prices_from_orders(_alpaca_open_orders)
    _manual_stops = ledger.get_manual_stop_prices()
    state["stop_prices"] = {**_broker_stops, **_manual_stops}

    state["open_orders"] = ledger_data.get("open_orders") or snapshot.get("open_orders") or []

    recon = ledger_data.get("reconciliation") or {}
    diffs = recon.get("differences") if isinstance(recon.get("differences"), dict) else {}
    if not diffs and recon.get("differences_json"):
        try:
            diffs = json.loads(recon.get("differences_json") or "{}")
        except json.JSONDecodeError:
            diffs = {}

    margin_detected = bool(diffs.get("margin_detected"))
    if not margin_detected and acct:
        try:
            cash = float(acct.get("cash") or 0)
            equity = float(acct.get("equity") or 0)
            long_mv = float(acct.get("long_market_value") or 0)
            short_mv = float(acct.get("short_market_value") or 0)
            mult = float(acct.get("multiplier") or 1)
            margin_detected = (
                cash < -0.01 or long_mv > equity + 0.01
                or short_mv > 0.01 or mult > 1.01
            )
        except (TypeError, ValueError):
            pass

    state["margin_detected"] = margin_detected
    state["trading_halted"] = ledger_data.get("trading_halted", False)
    state["trading_paused"] = ledger_data.get("trading_paused", False)
    state["pause_reason"] = ledger_data.get("pause_reason") or ledger.get_pause_reason()

    # Consecutive losses — completed_trades only (no legacy fills fallback)
    try:
        cl_status = ledger.get_consecutive_loss_status(limit=20)
        state["consecutive_losses"] = cl_status.get("count", 0)
        state["consecutive_loss_detail"] = cl_status
        state["consecutive_loss_source"] = cl_status.get("source", "completed_trades")
        state["ledger_complete"] = bool(cl_status.get("ledger_complete"))
        if not cl_status.get("ledger_complete"):
            state["ledger_status_message"] = cl_status.get(
                "message", "ledger incomplete — run python -m agenttrade.rebuild_ledger"
            )
    except Exception:
        state["consecutive_losses"] = 0
        state["consecutive_loss_detail"] = None
        state["consecutive_loss_source"] = "error"
        state["ledger_complete"] = False
        state["ledger_status_message"] = "ledger status unavailable"

    # ── Auto-clear stale consecutive-loss pause ──────────────────────────────
    # Clear when deterministic count is below threshold OR ledger is incomplete.
    if state.get("trading_paused") and not state.get("trading_halted"):
        _cl_count = state.get("consecutive_losses", 0)
        _threshold = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
        _pause_reason = (state.get("pause_reason") or "").lower()
        _is_cl_pause = "consecutive loss" in _pause_reason or _pause_reason == ""
        _ledger_ok = state.get("ledger_complete", False)
        if _is_cl_pause and (not _ledger_ok or _cl_count < _threshold):
            try:
                ledger.set_system_flag("TRADING_PAUSED", "false")
                ledger.set_system_flag("PAUSE_REASON", "")
                ledger.insert_risk_event(
                    None, "info", "CONSECUTIVE_LOSS_PAUSE_CLEARED",
                    f"Auto-cleared: deterministic count={_cl_count} < threshold={_threshold}"
                    if _ledger_ok else "Auto-cleared: ledger incomplete",
                )
                state["trading_paused"] = False
                state["pause_reason"] = ""
                log.info(
                    "[Publish] Auto-cleared consecutive-loss pause (count=%d < threshold=%d)",
                    _cl_count, _threshold,
                )
            except Exception as _clr_err:
                log.warning("[Publish] Could not auto-clear pause flag: %s", _clr_err)

    # Recent completed trades for Trading Desk
    try:
        state["completed_trades"] = ledger.get_completed_trades(limit=50)
    except Exception:
        state["completed_trades"] = []

    # daily_trades: prefer live Alpaca count, fall back to SQLite fills table
    if state.get("daily_trades_live") is None:
        try:
            state["daily_trades"] = ledger.count_fills_today()
        except Exception:
            pass
    state["position_sizing_source"] = ledger_data.get("position_sizing_source", "deterministic")
    state["risk_per_trade_pct"] = ledger_data.get("risk_per_trade_pct", 0.005)
    state["ignored_llm_sizing_count"] = ledger_data.get("ignored_llm_sizing_count", 0)
    state["halt_reason"] = ledger_data.get("halt_reason") or ledger.get_halt_reason()

    if state.get("trading_halted") and margin_detected and not cfg.ALLOW_MARGIN:
        state["trading_status_message"] = "TRADING HALTED: MARGIN DETECTED"
        state["trading_mode_status"] = "HALTED"
    elif state.get("trading_halted"):
        state["trading_status_message"] = state.get("halt_reason") or "Trading halted"
        state["trading_mode_status"] = "HALTED"
    elif state.get("trading_paused"):
        state["trading_status_message"] = (
            f"TRADING PAUSED: {state.get('pause_reason') or 'No new buys until review'}"
        )
        state["trading_mode_status"] = "PAUSED"
    else:
        # TRADING_HALTED flag is the authoritative halt signal.
        # A stale failed reconciliation row does NOT re-halt the system once
        # the operator has explicitly cleared the halt.  Recon status is still
        # surfaced via state["reconciliation"]["passed"] / last_reconciliation_status.
        state["trading_mode_status"] = "ACTIVE"

    state["reconciliation"] = {
        "passed": bool(recon.get("passed")),
        "status": recon.get("status"),
        "message": recon.get("message"),
        "finished_at": recon.get("finished_at"),
        "differences": diffs,
    }
    state["last_reconciliation_status"] = recon.get("status")
    state["last_reconciliation_message"] = recon.get("message")
    state["recent_risk_events"] = ledger_data.get("risk_events") or []
    state["risk_events"] = state["recent_risk_events"]
    state["strategy_signals"] = ledger_data.get("strategy_signals") or []
    state["sentiment_scores"] = ledger_data.get("sentiment_scores") or []

    try:
        from signal_performance import compute_signal_scorecards
        from agenttrade.performance import compute_performance_metrics
        state["signal_scorecards"] = compute_signal_scorecards(max_signals=100)
        state["performance_metrics"] = compute_performance_metrics(max_days=90)
    except Exception as e:
        log.warning("[Publish] Signal scorecards unavailable: %s", e)
        state["signal_scorecards"] = {}
        state["performance_metrics"] = {"status": "insufficient data"}

    bt = ledger_data.get("latest_backtest") or ledger.get_latest_backtest_run()
    state["validation"] = {
        "last_backtest_run": bt.get("finished_at") or bt.get("started_at") if bt else None,
        "backtest_return_pct": bt.get("total_return_pct") if bt else None,
        "backtest_max_drawdown_pct": bt.get("max_drawdown_pct") if bt else None,
        "backtest_win_rate": bt.get("win_rate") if bt else None,
        "backtest_profit_factor": bt.get("profit_factor") if bt else None,
        "safety_blocks": ledger_data.get("safety_blocks") or ledger.get_safety_block_counts(30),
    }

    state["signal_attributions"] = _merge_signal_attributions(
        ledger_data.get("signal_attributions") or [],
        _funnel_signal_attributions(cached),
    )
    state["reddit_trends"] = ledger_data.get("reddit_trends") or []
    state["screener_sources"] = (
        persisted_artifacts.get("screener_sources")
        or cached.get("screener_sources")
        or state.get("screener_sources")
        or {}
    )
    state["universes"] = (
        persisted_artifacts.get("universes")
        or cached.get("universes")
        or state.get("universes")
        or {}
    )
    state["pipeline_totals"] = _pipeline_totals(state["screener_sources"])

    if sqlite_funnel.get("cycle_run_id"):
        state["funnel_cycle_run_id"] = sqlite_funnel["cycle_run_id"]
        state["trade_candidates"] = persisted_funnel.get("candidates") or []
        state["decisions"] = persisted_funnel.get("decisions") or []
        state["blocked_ideas"] = list(persisted_funnel.get("blocked") or [])
        if not state["blocked_ideas"]:
            state["blocked_ideas"] = [
                row for row in (persisted_funnel.get("risk") or [])
                if str(row.get("risk_status") or row.get("status") or "").upper() == "BLOCKED"
            ]
        state["last_orders"] = persisted_funnel.get("orders") or state.get("last_orders") or []
        for _key in ("token_usage", "rebalance", "hard_rebalance", "buy_lock", "llm_mode", "last_run"):
            if persisted_artifacts.get(_key) is not None:
                state[_key] = persisted_artifacts[_key]
    state["signal_snapshots"] = ledger_data.get("signal_snapshots") or []
    state["source_accuracy_stats"] = ledger_data.get("source_accuracy_stats") or {}

    # Per-position signal breakdown from latest attributions
    attr_by_sym = {}
    for row in state["signal_attributions"]:
        sym = row.get("symbol")
        if sym and sym not in attr_by_sym:
            try:
                import json as _json
                comps = row.get("components") or _json.loads(row.get("components_json") or "{}")
            except Exception:
                comps = {}
            attr_by_sym[sym] = {
                "total_score": row.get("total_score"),
                "components": comps,
            }
    state["position_signal_breakdown"] = attr_by_sym

    cycle = ledger_data.get("cycle_run") or {}
    state["last_cycle_run"] = cycle.get("finished_at") or cycle.get("started_at")
    if cycle.get("started_at"):
        state["last_trading_cycle_at"] = cycle.get("finished_at") or cycle.get("started_at")

    if recon.get("finished_at"):
        state["last_reconciliation_at"] = recon.get("finished_at")

    # Always refresh bucket_tags from the live JSON file so the dashboard
    # shows correct bucket assignments even when no cycle has run recently.
    try:
        state["bucket_tags"] = cfg.bucket_manager._load_tags()
    except Exception as _bt_err:
        log.debug("[Publish] bucket_tags unavailable: %s", _bt_err)

    try:
        lock_ctx = ledger.get_buy_lock_state()
        if lock_ctx and not state.get("buy_lock"):
            from buy_lock import apply_lock_fields_to_state, evaluate_buy_lock
            buy_lock = evaluate_buy_lock(lock_ctx)
            state = apply_lock_fields_to_state(state, lock_ctx, buy_lock)
        elif lock_ctx:
            from buy_lock import apply_lock_fields_to_state, evaluate_buy_lock
            if not isinstance(state.get("buy_lock"), dict) or not state.get("buy_lock"):
                state = apply_lock_fields_to_state(state, lock_ctx, evaluate_buy_lock(lock_ctx))
    except Exception as _bl_err:
        log.debug("[Publish] buy_lock from SQLite unavailable: %s", _bl_err)

    # Enrich positions with bucket info from bucket_tags so the trading
    # desk shows the correct bucket for each open position.
    bucket_tags = state.get("bucket_tags") or {}
    if bucket_tags:
        for pos in state.get("positions") or []:
            if not pos.get("bucket"):
                sym = pos.get("symbol") or pos.get("ticker") or ""
                if sym and bucket_tags.get(sym):
                    pos["bucket"] = bucket_tags[sym]

    _log_signals_publish_summary(state, cached)
    return state


def _atomic_write_json(directory: str, dest: str, payload: str) -> None:
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="agent_state_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, dest)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def publish_dashboard_state(state: dict) -> None:
    """Write cache JSON for dashboard (non-authoritative).

    Refuses both the backend file and the public copy when the serialized
    payload exceeds AGENT_STATE_MAX_BYTES (default 8 MiB). The previous
    projection is left in place. SQLite is not modified.
    """
    payload = cfg.dumps_agent_state(state)
    if payload is None:
        return
    ss = state.get("screener_sources") or {}
    log.info(
        "[Publish] Writing agent_state.json — signal_attributions=%d reddit_trends=%d "
        "screener_buckets=%s pipeline_totals=%s",
        len(state.get("signal_attributions") or []),
        len(state.get("reddit_trends") or []),
        list(ss.keys()) if isinstance(ss, dict) else [],
        state.get("pipeline_totals") or {},
    )
    _atomic_write_json(cfg.APP_DIR, cfg.STATE_FILE, payload)
    try:
        _atomic_write_json(cfg.PUBLIC_DASHBOARD_DIR, cfg.PUBLIC_STATE_FILE, payload)
        os.chmod(cfg.PUBLIC_STATE_FILE, 0o644)
    except OSError as e:
        log.warning("[Ledger] Could not publish dashboard cache: %s", e)
