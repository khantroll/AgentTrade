"""Trading cycle orchestration — one full agent pipeline run."""

import json
import logging
import os
import tempfile
from datetime import datetime

import agent_config as cfg
from account_sync import merge_snapshot_into_state, refresh_alpaca_snapshot
from agents.analysis import analysis_agent
from agents.execution import execution_agent, hard_rebalance_agent
from agents.position_review import review_positions
from agents.research import research_agent
from agents.risk import risk_agent
from alpaca_client import get_recent_fills, is_market_open
from buy_lock import (
    apply_lock_fields_to_state,
    evaluate_buy_lock,
    is_buy_locked,
    load_prior_state,
    record_emergency_rebalance,
    record_sells_placed,
    sync_sell_fills,
)
from llm_router import active_mode, budget_exhausted, usage_summary
from market_data import is_crypto_bucket
from performance_history import append_cycle_snapshot, publish_history
from screener import get_universe
from signal_performance import (
    evaluate_pending_outcomes,
    record_candidate_snapshots,
    record_decision_snapshots,
)
from trade_log import append_order_meta, publish_trade_log, sync_trade_log

try:
    from confidence_engine import apply_calibration_to_decisions
except ImportError:
    apply_calibration_to_decisions = None  # type: ignore

log = logging.getLogger(__name__)


def _pipeline_counts(sources: dict) -> dict:
    """Numeric pipeline counts for logging (crypto uses separate key)."""
    if not isinstance(sources, dict):
        return {}
    keys = ("momentum", "movers", "reddit", "news", "congress", "tradingview", "crypto")
    return {k: int(sources.get(k) or 0) for k in keys if sources.get(k)}


def _summarize_screener_sources(all_sources: dict) -> dict:
    """Per-bucket screener summary for cycle/publish logs."""
    out = {}
    for bucket, sources in (all_sources or {}).items():
        if not isinstance(sources, dict):
            continue
        attr = sources.get("attribution") or {}
        out[bucket] = {
            "mode": sources.get("mode"),
            "cached": sources.get("cached"),
            "universe_hint": sources.get("crypto") or sources.get("momentum"),
            "pipelines": _pipeline_counts(sources),
            "attribution_symbols": len(attr) if isinstance(attr, dict) else 0,
        }
    return out


def _log_cycle_publish_payload(state: dict, all_sources: dict) -> None:
    """Log the signals-related slice of the dashboard payload."""
    ss = state.get("screener_sources") or all_sources or {}
    attrs = state.get("signal_attributions") or []
    trends = state.get("reddit_trends") or []
    pipelines = {}
    for _bucket, info in ss.items():
        if isinstance(info, dict):
            for k, v in _pipeline_counts(info).items():
                pipelines[k] = pipelines.get(k, 0) + v
    log.info(
        "[Cycle] Publish payload — signal_attributions=%d reddit_trends=%d "
        "screener_buckets=%s pipeline_totals=%s trade_candidates=%d decisions=%d",
        len(attrs),
        len(trends),
        list(ss.keys()),
        pipelines,
        len(state.get("trade_candidates") or []),
        len(state.get("decisions") or []),
    )


_CYCLE_RUN_ID: int | None = None


def get_cycle_run_id() -> int | None:
    return _CYCLE_RUN_ID


def _save_state(state: dict) -> None:
    """Write state atomically in backend folder and publish beside the dashboard."""
    fd, tmp_path = tempfile.mkstemp(prefix="agent_state_", suffix=".json", dir=cfg.APP_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, cfg.STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    try:
        os.makedirs(cfg.PUBLIC_DASHBOARD_DIR, exist_ok=True)
        fd, pub_tmp = tempfile.mkstemp(prefix="agent_state_", suffix=".json", dir=cfg.PUBLIC_DASHBOARD_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(pub_tmp, cfg.PUBLIC_STATE_FILE)
        os.chmod(cfg.PUBLIC_STATE_FILE, 0o644)
        log.info("[State] Published dashboard state to %s", cfg.PUBLIC_STATE_FILE)
    except Exception as pub_e:
        log.warning("[State] Could not publish dashboard state to %s: %s", cfg.PUBLIC_STATE_FILE, pub_e)


def run_trading_cycle() -> None:
    global _CYCLE_RUN_ID
    cfg.refresh_config()

    from agenttrade.db import init_db, finish_cycle_run, start_cycle_run
    from agenttrade.publish import build_dashboard_state, publish_dashboard_state
    from agenttrade.reconciliation import run_reconciliation_gate, sync_ledger_from_alpaca

    try:
        init_db()
    except Exception as db_e:
        log.error("[DB] SQLite init failed — trading blocked: %s", db_e)
        return

    import llm_router as _lr
    _lr.LLM_MODE = _lr._configured_llm_mode()

    try:
        from health_check import log_health_summary
        log_health_summary()
    except Exception:
        pass

    mode = active_mode()
    log.info("=" * 65)
    log.info(
        "🤖 Cycle — %s  (%s)  LLM=%s",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "PAPER" if cfg.ALPACA_PAPER else "⚠️ LIVE",
        mode,
    )
    log.info("=" * 65)

    if budget_exhausted():
        log.warning("Daily token budget exhausted — skipping entire cycle.")
        return

    market_open = is_market_open()
    if not market_open and not (cfg.ENABLE_CRYPTO and cfg.CRYPTO_TRADE_24_7):
        log.info("Market closed. Skipping.")
        return
    if not market_open:
        log.info("Stock market closed. Crypto 24/7 mode enabled; equity buckets will be skipped.")

    trading_mode = "PAPER" if cfg.ALPACA_PAPER else "LIVE"
    _CYCLE_RUN_ID = start_cycle_run(mode)

    recon = run_reconciliation_gate(_CYCLE_RUN_ID, trading_mode)
    if not recon.passed:
        log.error("[Cycle] BLOCKED — %s", recon.message)
        try:
            from agenttrade import db as _ledger
            _ledger.insert_funnel_event(
                _CYCLE_RUN_ID, "BLOCKED",
                status="BLOCKED",
                reason=str(recon.status or "reconciliation_failed"),
                payload={
                    "message": recon.message,
                    "status": recon.status,
                    "differences": recon.differences,
                    "blocked_reason": recon.status or "reconciliation_failed",
                },
            )
        except Exception:
            pass
        state = build_dashboard_state(live_snapshot=recon.snapshot)
        state["reconciliation"] = {
            "passed": False,
            "status": recon.status,
            "message": recon.message,
            "differences": recon.differences,
        }
        state["trading_status_message"] = f"Trading halted/paused: {recon.message}"
        publish_dashboard_state(state)
        finish_cycle_run(_CYCLE_RUN_ID, "blocked", recon.message)
        _CYCLE_RUN_ID = None
        return

    from agenttrade.risk import check_consecutive_loss_pause, get_consecutive_loss_status

    cl_status = get_consecutive_loss_status(limit=20)
    log.info(
        "[Cycle] Consecutive losses: count=%d source=%s ledger_complete=%s",
        cl_status.get("count", 0),
        cl_status.get("source"),
        cl_status.get("ledger_complete"),
    )
    if not cl_status.get("ledger_complete"):
        log.warning("[Cycle] %s", cl_status.get("message", "ledger incomplete"))

    paused, pause_reason = check_consecutive_loss_pause()
    if paused:
        log.warning("[Cycle] TRADING_PAUSED — %s (protective sells still allowed)", pause_reason)

    snapshot = recon.snapshot or refresh_alpaca_snapshot()

    hard_plans = []
    lock_ctx = load_prior_state()
    lock_ctx = sync_sell_fills(lock_ctx, get_recent_fills(50))
    buy_lock = evaluate_buy_lock(lock_ctx)
    if buy_lock.get("active"):
        log.warning(
            "[BuyLock] %s — %d symbol lock(s), unlock %s",
            buy_lock.get("message"),
            len(buy_lock.get("locked_symbols") or []),
            buy_lock.get("unlock_at"),
        )

    try:
        account = snapshot["account"]
        positions = snapshot["positions"]
        pv = float(account["portfolio_value"])
        cash = float(account["cash"])

        log.info(
            "💼 Live Alpaca: $%s equity | $%s cash | $%s buying_power | %d positions",
            f"{float(account.get('equity', pv)):,.2f}",
            f"{cash:,.2f}",
            f"{float(account.get('buying_power', 0)):,.2f}",
            len(positions),
        )
        if snapshot.get("equity_mismatch"):
            log.warning(
                "[AccountSync] %s (delta=$%s)",
                snapshot.get("equity_mismatch_message"),
                snapshot.get("equity_mismatch_delta"),
            )

        rebalance = cfg.bucket_manager.rebalance_report(positions, pv)
        ordered_buckets = cfg.bucket_manager.prioritize_buckets(rebalance)

        log.info("[Buckets] Rebalance:")
        for name, r in rebalance.items():
            log.info(
                "  %s: target=%s%% current=%s%% drift=$%+,.0f → %s",
                name, r["target_pct"], r["current_pct"], r["drift_$"], r["action"],
            )

        hard_orders, hard_plans = hard_rebalance_agent(rebalance, positions, pv, market_open)
        if hard_orders:
            lock_ctx = record_emergency_rebalance(lock_ctx, hard_orders)
            lock_ctx = record_sells_placed(lock_ctx, hard_orders)
            buy_lock = evaluate_buy_lock(lock_ctx)
            all_orders_early = list(hard_orders)
            try:
                append_order_meta(hard_orders, llm_mode=mode, decisions=[])
            except Exception as om_e:
                log.warning("[HardRebalance] order meta: %s", om_e)
            snapshot = sync_ledger_from_alpaca(_CYCLE_RUN_ID, "post_hard_rebalance")
            account = snapshot["account"]
            positions = snapshot["positions"]
            pv = float(account["portfolio_value"])
            cash = float(account["cash"])
            rebalance = cfg.bucket_manager.rebalance_report(positions, pv)
            ordered_buckets = cfg.bucket_manager.prioritize_buckets(rebalance)
            log.info(
                "💼 After hard rebalance: $%s | $%s cash | %d positions",
                f"{pv:,.2f}", f"{cash:,.2f}", len(positions),
            )
        else:
            all_orders_early = []

        # Refresh live account before buy pipeline
        snapshot = refresh_alpaca_snapshot()
        account = snapshot["account"]
        positions = snapshot["positions"]
        pv = float(account["portfolio_value"])
        cash = float(account["cash"])
        rebalance = cfg.bucket_manager.rebalance_report(positions, pv)
        ordered_buckets = cfg.bucket_manager.prioritize_buckets(rebalance)

        # ── Position Review — check exits before deploying new capital ────────
        # This runs before any buys so we exit losers first and free up cash.
        # Covers: stop-loss, take-profit, and bucket overweight trimming.
        review_sells = review_positions(positions, account, rebalance, market_open)
        if review_sells:
            lock_ctx = record_sells_placed(lock_ctx, review_sells)
            buy_lock = evaluate_buy_lock(lock_ctx)
            log.info("[Cycle] Position review placed %d sell order(s)", len(review_sells))
            try:
                append_order_meta(review_sells, llm_mode=mode, decisions=[])
            except Exception as rv_e:
                log.warning("[Cycle] order meta for review sells: %s", rv_e)
            snapshot = sync_ledger_from_alpaca(_CYCLE_RUN_ID, "post_position_review")
            account = snapshot["account"]
            positions = snapshot["positions"]
            pv = float(account["portfolio_value"])
            cash = float(account["cash"])
            rebalance = cfg.bucket_manager.rebalance_report(positions, pv)
            ordered_buckets = cfg.bucket_manager.prioritize_buckets(rebalance)
        else:
            review_sells = []

        all_orders = list(all_orders_early) + list(review_sells)
        all_universes = {}
        all_sources = {}
        all_candidates = []
        all_decisions = []
        all_blocked = []

        for bucket in ordered_buckets:
            if not market_open and not is_crypto_bucket(bucket):
                log.info("[%s] Stock market closed — skipping equity bucket.", bucket.name)
                continue
            if is_crypto_bucket(bucket) and not cfg.ENABLE_CRYPTO:
                log.info("[%s] ENABLE_CRYPTO is off — skipping crypto bucket.", bucket.name)
                continue
            if is_crypto_bucket(bucket) and not market_open:
                log.info("[%s] Crypto 24/7 — evaluating bucket while stock market closed.", bucket.name)

            log.info("\n── Bucket: %s (mode=%s) ──", bucket.name, bucket.mode)

            universe, sources = get_universe(
                bucket_config=bucket.screener_config,
                use_news_filter=True,
                use_congress_filter=True,
                use_reddit_filter=True,
            )

            all_universes[bucket.name] = universe
            all_sources[bucket.name] = sources
            log.info(
                "[%s] Screener: universe=%d pipelines=%s cached=%s mode=%s",
                bucket.name,
                len(universe),
                _pipeline_counts(sources),
                sources.get("cached"),
                sources.get("mode"),
            )

            from agenttrade import recording as rec
            from agenttrade import risk as risk_mgr

            attribution_map = sources.get("attribution") or {}
            attr_rows = 0
            if _CYCLE_RUN_ID and attribution_map:
                attr_rows = rec.record_signal_attributions(_CYCLE_RUN_ID, attribution_map)
                rec.record_reddit_sentiment(_CYCLE_RUN_ID, universe, sources)
            log.info(
                "[%s] Signal attribution: %d symbols written to SQLite (reddit pipeline hits=%s)",
                bucket.name,
                attr_rows,
                sources.get("reddit", 0),
            )
            if sources.get("mode") == "crypto" and not attribution_map:
                log.info(
                    "[%s] Crypto screener — no equity-style attribution map "
                    "(pipeline grid uses 'crypto' count=%s)",
                    bucket.name,
                    sources.get("crypto", len(universe)),
                )

            if _CYCLE_RUN_ID:
                for _sym in universe or []:
                    _attr = attribution_map.get(str(_sym).upper()) or {}
                    from agenttrade import db as _ledger
                    _ledger.insert_funnel_event(
                        _CYCLE_RUN_ID, "UNIVERSE", symbol=str(_sym), bucket=bucket.name,
                        status="SCREENED", payload={"ticker": str(_sym), "bucket": bucket.name, "attribution": _attr},
                    )

            candidates = research_agent(universe, bucket)
            log.info(
                "[%s] Research: %d candidates from universe of %d",
                bucket.name,
                len(candidates or []),
                len(universe),
            )
            if not candidates:
                log.warning(
                    "[%s] No research candidates — analysis skipped; screener data retained in state",
                    bucket.name,
                )
                continue

            candidates = rec.attach_attribution_to_candidates(candidates, attribution_map)
            for c in candidates:
                c["bucket"] = bucket.name
            all_candidates.extend(candidates)
            rec.record_candidates(_CYCLE_RUN_ID, candidates)
            if _CYCLE_RUN_ID:
                from agenttrade import db as _ledger
                _ledger.insert_funnel_events(_CYCLE_RUN_ID, "CANDIDATE", candidates, bucket=bucket.name)
                record_candidate_snapshots(_CYCLE_RUN_ID, candidates, attribution_map)

            decisions = analysis_agent(candidates, account, positions, bucket, attribution_map=attribution_map)
            if apply_calibration_to_decisions is not None:
                decisions = apply_calibration_to_decisions(decisions, attribution_map)
            else:
                log.warning("[%s] confidence_engine unavailable — raw confidence unchanged", bucket.name)
            for d in decisions:
                attr = attribution_map.get(str(d.get("ticker", "")).upper()) or {}
                d["total_score"] = attr.get("total_score")
                d["signal_components"] = attr.get("components")
            all_decisions.extend(decisions)
            rec.record_decisions(_CYCLE_RUN_ID, decisions)
            if _CYCLE_RUN_ID:
                from agenttrade import db as _ledger
                _ledger.insert_funnel_events(_CYCLE_RUN_ID, "DECISION", decisions, bucket=bucket.name)
                record_decision_snapshots(_CYCLE_RUN_ID, decisions, attribution_map)

            approved = risk_agent(
                decisions, account, positions, bucket, rebalance,
                buy_lock=buy_lock, account_snapshot=snapshot,
            )
            approved = risk_mgr.evaluate_batch(approved, snapshot, bucket, cycle_run_id=_CYCLE_RUN_ID)
            approved_tickers = {d["ticker"] for d in approved}
            bucket_ac = getattr(bucket, "asset_class", "us_equity")
            if is_crypto_bucket(bucket):
                bucket_ac = "crypto"
            for d in decisions:
                if d.get("action", "SKIP").upper() != "BUY":
                    all_blocked.append(d)
                else:
                    ticker = d.get("ticker", "")
                    locked, lock_reason, _ = is_buy_locked(
                        symbol=ticker,
                        asset_class=bucket_ac,
                        bucket=bucket.name,
                        buy_lock=buy_lock,
                    )
                    if locked:
                        d["blocked_reason"] = f"symbol_lock:{lock_reason}"
                        all_blocked.append(d)
                    elif d["ticker"] not in approved_tickers:
                        if not d.get("blocked_reason"):
                            d["blocked_reason"] = "risk_rejected"
                        all_blocked.append(d)

            if _CYCLE_RUN_ID:
                from agenttrade import db as _ledger
                _approved_by_ticker = {str(x.get("ticker", "")).upper(): x for x in approved}
                for _d in decisions:
                    if str(_d.get("action") or "SKIP").upper() != "BUY":
                        continue
                    _ticker = str(_d.get("ticker", "")).upper()
                    _is_approved = _ticker in _approved_by_ticker
                    _payload = dict(_d)
                    _payload["risk_status"] = "APPROVED" if _is_approved else "BLOCKED"
                    _ledger.insert_funnel_event(
                        _CYCLE_RUN_ID, "RISK", symbol=_ticker, bucket=bucket.name,
                        status=_payload["risk_status"],
                        reason="" if _is_approved else str(_d.get("blocked_reason") or "risk_rejected"),
                        payload=_payload,
                    )

            orders = execution_agent(
                approved, bucket, buy_lock=buy_lock, account_snapshot=snapshot,
                cycle_run_id=_CYCLE_RUN_ID,
            )
            all_orders.extend(orders)
            if _CYCLE_RUN_ID and orders:
                from agenttrade import db as _ledger
                _ledger.insert_funnel_events(_CYCLE_RUN_ID, "ORDER", orders, bucket=bucket.name)

        log.info(
            "[Cycle] Funnel summary: candidates=%d decisions=%d blocked=%d buckets_screener=%s",
            len(all_candidates),
            len(all_decisions),
            len(all_blocked),
            _summarize_screener_sources(all_sources),
        )

        try:
            append_order_meta(all_orders, llm_mode=mode, decisions=all_decisions)
        except Exception as om_e:
            log.warning("[TradeLog] Could not save order meta: %s", om_e)

        if all_orders and _CYCLE_RUN_ID:
            snapshot = sync_ledger_from_alpaca(_CYCLE_RUN_ID, "post_trade")

        lock_ctx = sync_sell_fills(lock_ctx, get_recent_fills(50))
        buy_lock = evaluate_buy_lock(lock_ctx)

        if _CYCLE_RUN_ID:
            snapshot = sync_ledger_from_alpaca(_CYCLE_RUN_ID, "post_cycle")
            try:
                evaluate_pending_outcomes(limit=40)
            except Exception as perf_e:
                log.warning("[SignalPerformance] Outcome evaluation skipped: %s", perf_e)
        else:
            snapshot = refresh_alpaca_snapshot()
        account = snapshot["account"]
        positions = snapshot["positions"]

        tok = usage_summary()
        cycle_finished = datetime.now().isoformat()
        state = {
            "last_run": cycle_finished,
            "last_trading_cycle_at": cycle_finished,
            "portfolio_value": account.get("portfolio_value"),
            "equity": account.get("equity"),
            "cash": account.get("cash"),
            "buying_power": account.get("buying_power"),
            "mode": "PAPER" if cfg.ALPACA_PAPER else "LIVE",
            "llm_mode": mode,
            "aggression": cfg.STRATEGY_AGGRESSION,
            "positions": positions,
            "bucket_tags": cfg.bucket_manager._load_tags(),
            "trade_candidates": all_candidates,
            "decisions": all_decisions,
            "blocked_ideas": all_blocked,
            "last_orders": all_orders,  # includes position_review sells + hard_rebalance + buys
            "recent_fills": get_recent_fills(30),
            "daily_trades": cfg.daily_trades,
            "rebalance": rebalance,
            "hard_rebalance": {
                "enabled": cfg.env_bool("HARD_REBALANCE", "false"),
                "drift_pct": cfg.hard_rebalance_drift_pct(),
                "plans": hard_plans,
                "orders": [o for o in all_orders if o.get("hard_rebalance")],
            },
            "buckets": cfg.bucket_manager.to_dict(),
            "universes": all_universes,
            "screener_sources": all_sources,
            "token_usage": {
                "total_tokens": tok.get("total_tokens", 0),
                "total_cost_usd": round(tok.get("total_cost_usd", 0), 4),
                "calls": tok.get("calls", 0),
                "budget": int(os.getenv("DAILY_TOKEN_BUDGET", "200000")),
                "by_model": tok.get("by_model", {}),
                "calls_log": tok.get("calls_log", [])[-20:],
            },
        }
        state = apply_lock_fields_to_state(state, lock_ctx, buy_lock)
        state = merge_snapshot_into_state(state, snapshot, source="cycle")
        if _CYCLE_RUN_ID:
            from agenttrade import db as _ledger
            for _key in ("universes", "screener_sources", "token_usage", "rebalance", "hard_rebalance", "buy_lock", "llm_mode", "last_run"):
                _ledger.upsert_cycle_artifact(_CYCLE_RUN_ID, _key, state.get(_key))
        from agenttrade.publish import build_dashboard_state, publish_dashboard_state
        state = build_dashboard_state(cached_funnel=state, live_snapshot=snapshot)
        _log_cycle_publish_payload(state, all_sources)
        publish_dashboard_state(state)
        finish_cycle_run(_CYCLE_RUN_ID, "completed", f"{len(all_orders)} orders")
        _CYCLE_RUN_ID = None

        try:
            append_cycle_snapshot(account, positions, state)
            publish_history(cfg.PUBLIC_DASHBOARD_DIR)
            log.info("[History] Cycle snapshot appended to performance_history.jsonl")
        except Exception as hist_e:
            log.warning("[History] Could not record cycle snapshot: %s", hist_e)

        try:
            sync_trade_log(state=state)
            publish_trade_log(cfg.PUBLIC_DASHBOARD_DIR)
            log.info("[TradeLog] Fills synced to trade_log.jsonl")
        except Exception as tl_e:
            log.warning("[TradeLog] Could not sync trade log: %s", tl_e)

        log.info(
            "\n✅ Cycle complete — %d orders | %s tokens used today ($%.4f)",
            len(all_orders),
            f"{tok['total_tokens']:,}",
            tok["total_cost_usd"],
        )

    except Exception as e:
        log.error("Cycle error: %s", e, exc_info=True)
        try:
            from agenttrade.db import finish_cycle_run as _finish
            if _CYCLE_RUN_ID:
                _finish(_CYCLE_RUN_ID, "error", str(e))
        except Exception:
            pass
        _CYCLE_RUN_ID = None


def monitor_positions() -> None:
    try:
        from agenttrade.db import init_db
        from agenttrade.publish import build_dashboard_state, publish_dashboard_state

        init_db()
        state = build_dashboard_state()
        state["last_monitor_at"] = state.get("last_alpaca_sync_at")
        publish_dashboard_state(state)
        for pos in state.get("positions") or []:
            pnl = float(pos.get("unrealized_plpc", 0)) * 100
            log.info("[Monitor] %s: %+.1f%%", pos["symbol"], pnl)
        log.info("[Monitor] Live account synced at %s", state.get("last_monitor_at"))
    except Exception as e:
        log.error("[Monitor] %s", e)


def reset_daily_counters() -> None:
    cfg.reset_daily_counters()
    log.info("Daily counters reset.")
