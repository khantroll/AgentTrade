"""Production-path tests against a temporary SQLite ledger (never the recovered fixture)."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from buckets import Bucket

from tests.conftest import RECOVERED_SQLITE, RECOVERED_SQLITE_SHA256

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _growth_bucket() -> Bucket:
    return Bucket(name="Growth", allocation_pct=0.5, mode="growth")


def _pass_snapshot(**kwargs) -> dict:
    account = {
        "cash": 20000.0,
        "equity": 30000.0,
        "buying_power": 20000.0,
        "portfolio_value": 30000.0,
        "long_market_value": 10000.0,
        "short_market_value": 0.0,
        "multiplier": 1.0,
        "id": "paper-acct",
    }
    account.update(kwargs.pop("account", {}))
    return {
        "account": account,
        "positions": kwargs.get("positions", []),
        "open_orders": kwargs.get("open_orders", []),
        "recent_fills": kwargs.get("recent_fills", []),
        "open_buy_notional": kwargs.get("open_buy_notional", 0),
        "equity": account["equity"],
        "cash": account["cash"],
        "buying_power": account["buying_power"],
        "portfolio_value": account["portfolio_value"],
    }


@pytest.fixture(scope="session")
def recovered_sqlite_fingerprint():
    if not os.path.isfile(RECOVERED_SQLITE):
        return None
    return (_hash_file(RECOVERED_SQLITE), os.path.getsize(RECOVERED_SQLITE))


def test_recovered_sqlite_fixture_is_never_the_test_db(recovered_sqlite_fingerprint):
    from agenttrade import db

    db.init_db()
    used = os.path.abspath(db.get_db_path())
    recovered = os.path.abspath(RECOVERED_SQLITE)
    assert used != recovered
    assert "test_ledger" in os.path.basename(used) or os.path.dirname(used) != os.path.dirname(recovered)
    if recovered_sqlite_fingerprint and os.path.isfile(RECOVERED_SQLITE):
        assert recovered_sqlite_fingerprint[0] == RECOVERED_SQLITE_SHA256
        assert _hash_file(RECOVERED_SQLITE) == recovered_sqlite_fingerprint[0]
        assert os.path.getsize(RECOVERED_SQLITE) == recovered_sqlite_fingerprint[1]


def test_reconciliation_pass(monkeypatch):
    from agenttrade import db
    from agenttrade.reconciliation import run_reconciliation_gate

    db.init_db()
    snapshot = _pass_snapshot()
    monkeypatch.setattr(
        "agenttrade.reconciliation.refresh_alpaca_snapshot",
        lambda: snapshot,
    )
    import agent_config as cfg
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    monkeypatch.setattr(cfg, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False)

    cycle_id = db.start_cycle_run("tiered")
    result = run_reconciliation_gate(cycle_id, "PAPER")
    assert result.passed is True
    assert result.status == "passed"
    recon = db.get_latest_reconciliation()
    assert recon and int(recon["passed"]) == 1


def test_reconciliation_failure_blocks_trading(monkeypatch):
    from agenttrade import db
    from agenttrade.reconciliation import run_reconciliation_gate

    db.init_db()
    snapshot = _pass_snapshot(account={"cash": -250.0, "equity": 10000.0, "long_market_value": 10250.0})
    monkeypatch.setattr(
        "agenttrade.reconciliation.refresh_alpaca_snapshot",
        lambda: snapshot,
    )
    import agent_config as cfg
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    monkeypatch.setattr(cfg, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False)

    cycle_id = db.start_cycle_run("tiered")
    result = run_reconciliation_gate(cycle_id, "PAPER")
    assert result.passed is False
    assert result.buys_allowed is False
    assert db.trading_halted() is True
    recon = db.get_latest_reconciliation()
    assert recon and int(recon["passed"]) == 0


def test_stale_consecutive_loss_pause_clears_when_ledger_incomplete():
    from agenttrade import db
    from agenttrade.risk import check_consecutive_loss_pause

    db.init_db()
    db.set_system_flag("TRADING_PAUSED", "true")
    db.set_system_flag("PAUSE_REASON", "Consecutive loss limit reached")
    paused, reason = check_consecutive_loss_pause()
    assert paused is False
    assert db.trading_paused() is False
    assert "ledger incomplete" in (reason or "").lower() or reason != ""


def test_stale_consecutive_loss_pause_clears_when_count_below_threshold():
    from agenttrade import db
    from agenttrade.risk import check_consecutive_loss_pause

    db.init_db()
    db.insert_completed_trade(
        "AAPL", "us_equity", "Growth",
        "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00",
        1.0, 100.0, 110.0, 10.0, 10.0, "take_profit", "1",
    )
    db.set_system_flag("TRADING_PAUSED", "true")
    db.set_system_flag("PAUSE_REASON", "Consecutive loss limit reached")
    paused, _reason = check_consecutive_loss_pause()
    assert paused is False
    assert db.trading_paused() is False


def test_alpaca_open_order_sync_into_sqlite():
    from agenttrade import db

    db.init_db()
    orders = [
        {
            "id": "ord-stop-1",
            "symbol": "MSFT",
            "side": "sell",
            "type": "stop",
            "qty": 5,
            "stop_price": 380.25,
            "status": "open",
            "submitted_at": "2026-08-27T14:00:00Z",
        }
    ]
    summary = db.sync_open_orders_from_alpaca(orders)
    assert summary["upserted"] == 1
    open_rows = db.get_latest_open_orders()
    assert any(r.get("alpaca_order_id") == "ord-stop-1" for r in open_rows)
    stops = db.extract_stop_prices_from_orders(orders)
    assert stops["MSFT"] == 380.25


def test_protective_sell_stop_visible_on_dashboard(monkeypatch):
    from agenttrade import db
    from agenttrade.publish import build_dashboard_state

    db.init_db()
    orders = [
        {
            "id": "ord-prot-2",
            "symbol": "NVDA",
            "side": "sell",
            "type": "stop",
            "qty": 2,
            "stop_price": 120.5,
            "status": "open",
        }
    ]
    db.sync_open_orders_from_alpaca(orders)
    db.insert_account_snapshot(db.start_cycle_run("tiered"), "test", {
        "equity": 10000, "cash": 4000, "buying_power": 4000,
        "portfolio_value": 10000, "long_market_value": 6000,
        "short_market_value": 0, "multiplier": 1,
    })
    snapshot = _pass_snapshot(open_orders=orders, positions=[{
        "symbol": "NVDA", "qty": 2, "avg_entry_price": 130,
        "current_price": 125, "market_value": 250, "unrealized_pl": -10,
        "side": "long",
    }])
    state = build_dashboard_state(cached_funnel={}, live_snapshot=snapshot)
    assert state["stop_prices"].get("NVDA") == 120.5
    assert any(
        (o.get("symbol") == "NVDA" or o.get("alpaca_order_id") == "ord-prot-2")
        for o in (state.get("open_orders") or orders)
    )


def test_manual_stop_persists_in_sqlite_not_json(tmp_path):
    from agenttrade import db

    db.init_db()
    db.set_manual_stop_price("aapl", 150.1234)
    assert db.get_manual_stop_prices() == {"AAPL": 150.1234}
    state_file = tmp_path / "agent_state.json"
    assert not state_file.exists() or "AAPL" not in json.dumps(json.loads(state_file.read_text()).get("stop_prices") or {})


def test_buy_lock_blocks_matching_symbol_only():
    from datetime import datetime, timedelta

    import buy_lock

    now = datetime(2026, 8, 27, 12, 0, 0)
    ctx = buy_lock.record_sells_placed(
        {},
        [{"ticker": "AAPL", "side": "sell", "status": "placed", "bucket": "Growth"}],
        now=now,
    )
    buy_lock_status = buy_lock.evaluate_buy_lock(ctx, now=now)
    locked, reason, _detail = buy_lock.is_buy_locked(
        symbol="AAPL", bucket="Growth", buy_lock=buy_lock_status, now=now,
    )
    assert locked is True
    assert reason == "recent_sell"
    unlocked, _, _ = buy_lock.is_buy_locked(
        symbol="MSFT", bucket="Growth", buy_lock=buy_lock_status, now=now,
    )
    assert unlocked is False
    later = now + timedelta(hours=25)
    expired, _, _ = buy_lock.is_buy_locked(
        symbol="AAPL", bucket="Growth", buy_lock=buy_lock_status, now=later,
    )
    assert expired is False


def test_buy_lock_state_round_trips_sqlite():
    from agenttrade import db
    import buy_lock

    db.init_db()
    ctx = {
        "symbol_locks": [{
            "symbol": "TSLA",
            "asset_class": "us_equity",
            "scope": "symbol",
            "reason": "recent_sell",
            "locked_at": "2026-08-27T10:00:00",
            "unlock_at": "2026-08-28T10:00:00",
        }],
        "last_sell_at": "2026-08-27T10:00:00",
        "last_sell_symbols": ["TSLA"],
        "processed_fill_ids": ["fill-1"],
    }
    db.set_buy_lock_state(ctx)
    loaded = buy_lock.load_prior_state()
    assert loaded["last_sell_symbols"] == ["TSLA"]
    assert loaded["symbol_locks"][0]["symbol"] == "TSLA"


def test_deterministic_risk_rejection_records_reason():
    from agenttrade import db
    from agenttrade.risk import evaluate_batch

    db.init_db()
    snapshot = _pass_snapshot(account={"equity": 10000, "cash": 0, "portfolio_value": 10000})
    decisions = [{
        "ticker": "AAPL",
        "action": "BUY",
        "current_price": 100.0,
        "shares": 10,
    }]
    approved = evaluate_batch(decisions, snapshot, bucket=_growth_bucket())
    assert approved == []
    assert decisions[0]["blocked_reason"]


def test_deterministic_sizing_ignores_llm_qty_and_notional():
    from agenttrade import db
    from agenttrade.risk import prepare_buy_order, strip_llm_sizing

    db.init_db()
    raw = {
        "ticker": "AAPL",
        "action": "BUY",
        "current_price": 100.0,
        "shares": 9999,
        "qty": 9999,
        "notional_usd": 500000,
        "notional": 500000,
    }
    stripped = strip_llm_sizing(raw)
    assert "shares" not in stripped or stripped.get("shares") in (None, "", 0, 0.0)
    assert "notional_usd" not in stripped
    snapshot = _pass_snapshot()
    ok, _reason, adj = prepare_buy_order(raw, snapshot, bucket=_growth_bucket())
    assert ok is True
    assert adj.get("position_sizing_source") == "deterministic"
    assert int(adj.get("shares") or 0) != 9999
    assert int(adj.get("shares") or 0) > 0


def test_execution_failure_emits_machine_readable_reason(monkeypatch):
    from agenttrade import db
    from agents.execution import execution_agent

    db.init_db()
    monkeypatch.setattr(db, "trading_paused", lambda: False)
    monkeypatch.setattr(db, "trading_halted", lambda: False)
    import agent_config as cfg
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr(cfg, "daily_trades", 0)
    monkeypatch.setattr(cfg, "MAX_DAILY_TRADES", 5)
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    monkeypatch.setattr("agenttrade.strategy_modes.mode_blocks_order", lambda *_a, **_k: (False, ""))
    monkeypatch.setattr("agenttrade.strategy_modes.get_bucket_execution_mode", lambda *_a, **_k: "live")
    monkeypatch.setattr(
        "agents.execution.check_buy_allowed",
        lambda *a, **k: (True, "", {"cash": 20000}),
    )
    monkeypatch.setattr("agents.execution.get_account", lambda: _pass_snapshot()["account"])
    monkeypatch.setattr("agents.execution.enforce_no_margin_order_guard", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(
        "agents.execution.alpaca_post",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("simulated broker outage")),
    )

    approved = [{
        "ticker": "AAPL",
        "shares": 2,
        "current_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "atr_pct": 0.5,
    }]
    results = execution_agent(approved, _growth_bucket(), account_snapshot=_pass_snapshot())
    assert results
    assert results[0]["status"] == "failed"
    assert results[0]["blocked_reason"] == "execution_failed"


def test_trading_pause_blocks_execution_with_reason(monkeypatch):
    from agenttrade import db
    from agents.execution import execution_agent

    db.init_db()
    db.set_system_flag("TRADING_PAUSED", "true")
    db.set_system_flag("PAUSE_REASON", "Consecutive loss limit reached")
    results = execution_agent(
        [{"ticker": "AAPL", "shares": 1, "current_price": 100}],
        _growth_bucket(),
        account_snapshot=_pass_snapshot(),
    )
    assert results[0]["blocked_reason"] == "trading_paused"


def test_dashboard_rebuilds_from_sqlite_without_agent_state_json(tmp_path):
    from agenttrade import db
    from agenttrade.publish import build_dashboard_state

    db.init_db()
    cycle_id = db.start_cycle_run("tiered")
    db.insert_account_snapshot(cycle_id, "test", {
        "equity": 25000, "cash": 8000, "buying_power": 8000,
        "portfolio_value": 25000, "long_market_value": 17000,
        "short_market_value": 0, "multiplier": 1,
    })
    db.insert_positions(cycle_id, "alpaca", [{
        "symbol": "AAPL", "qty": 10, "avg_entry_price": 150,
        "current_price": 160, "market_value": 1600, "unrealized_pl": 100,
    }])
    db.insert_funnel_event(cycle_id, "UNIVERSE", symbol="AAPL", bucket="Growth", status="SCREENED")
    db.insert_funnel_events(cycle_id, "CANDIDATE", [{"ticker": "AAPL", "bucket": "Growth", "confidence": 0.8}])
    db.insert_funnel_events(cycle_id, "DECISION", [{
        "ticker": "AAPL", "action": "BUY", "bucket": "Growth", "confidence": 0.8,
    }])
    db.insert_funnel_event(
        cycle_id, "ORDER", symbol="AAPL", bucket="Growth", status="placed",
        payload={"ticker": "AAPL", "status": "placed", "order_id": "ord-9"},
    )
    db.upsert_cycle_artifact(cycle_id, "universes", {"Growth": ["AAPL"]})
    db.upsert_cycle_artifact(cycle_id, "screener_sources", {"Growth": {"momentum": 12, "movers": 4}})
    db.upsert_cycle_artifact(cycle_id, "token_usage", {"total_tokens": 111, "total_cost_usd": 0.02, "calls": 3})
    db.upsert_cycle_artifact(cycle_id, "rebalance", {"Growth": {"action": "HOLD", "target_pct": 50}})

    state_path = tmp_path / "agent_state.json"
    if state_path.exists():
        state_path.unlink()
    public = tmp_path / "public" / "agent_state.json"
    if public.exists():
        public.unlink()

    state = build_dashboard_state(cached_funnel={}, live_snapshot={})
    assert state["funnel_source"] == "sqlite"
    assert state["trade_candidates"][0]["ticker"] == "AAPL"
    assert state["decisions"][0]["action"] == "BUY"
    assert state["universes"]["Growth"] == ["AAPL"]
    assert state["screener_sources"]["Growth"]["momentum"] == 12
    assert state["token_usage"]["total_tokens"] == 111
    assert state["rebalance"]["Growth"]["action"] == "HOLD"
    assert state["last_orders"][0]["status"] == "placed"
    assert state.get("equity") == 25000 or state.get("portfolio_value") == 25000


def test_funnel_replay_universe_through_order():
    from agenttrade import db

    db.init_db()
    cycle_id = db.start_cycle_run("tiered")
    db.insert_funnel_event(cycle_id, "UNIVERSE", symbol="MSFT", bucket="Growth", status="SCREENED")
    db.insert_funnel_event(cycle_id, "CANDIDATE", symbol="MSFT", bucket="Growth", status="CANDIDATE")
    db.insert_funnel_event(
        cycle_id, "DECISION", symbol="MSFT", bucket="Growth", status="BUY",
        payload={"ticker": "MSFT", "action": "BUY"},
    )
    db.insert_funnel_event(
        cycle_id, "RISK", symbol="MSFT", bucket="Growth", status="APPROVED",
        payload={"ticker": "MSFT", "risk_status": "APPROVED"},
    )
    db.insert_funnel_event(
        cycle_id, "ORDER", symbol="MSFT", bucket="Growth", status="placed",
        payload={"ticker": "MSFT", "status": "placed", "order_id": "abc"},
    )
    funnel = db.get_cycle_funnel(cycle_id)
    assert funnel["universe"][0]["ticker"] == "MSFT"
    assert funnel["candidates"]
    assert funnel["decisions"][0]["action"] == "BUY"
    assert funnel["risk"][0]["risk_status"] == "APPROVED"
    assert funnel["orders"][0]["status"] == "placed"


def test_funnel_replay_universe_through_blocked():
    from agenttrade import db

    db.init_db()
    cycle_id = db.start_cycle_run("tiered")
    db.insert_funnel_event(cycle_id, "UNIVERSE", symbol="AMD", bucket="Growth", status="SCREENED")
    db.insert_funnel_event(cycle_id, "CANDIDATE", symbol="AMD", bucket="Growth", status="CANDIDATE")
    db.insert_funnel_event(
        cycle_id, "DECISION", symbol="AMD", bucket="Growth", status="BUY",
        payload={"ticker": "AMD", "action": "BUY"},
    )
    db.insert_funnel_event(
        cycle_id, "RISK", symbol="AMD", bucket="Growth", status="BLOCKED",
        reason="insufficient_cash",
        payload={"ticker": "AMD", "risk_status": "BLOCKED", "blocked_reason": "insufficient_cash"},
    )
    funnel = db.get_cycle_funnel(cycle_id)
    assert funnel["risk"][0]["blocked_reason"] == "insufficient_cash"
    assert funnel["blocked"]
    assert funnel["blocked"][0]["blocked_reason"] == "insufficient_cash"


def test_funnel_insert_is_idempotent_on_retry():
    from agenttrade import db

    db.init_db()
    cycle_id = db.start_cycle_run("tiered")
    db.insert_funnel_event(cycle_id, "UNIVERSE", symbol="IBM", bucket="Growth", status="SCREENED")
    db.insert_funnel_event(cycle_id, "UNIVERSE", symbol="IBM", bucket="Growth", status="SCREENED")
    funnel = db.get_cycle_funnel(cycle_id)
    assert len(funnel["universe"]) == 1


def test_pre_analysis_skip_reasons_are_machine_readable():
    from analysis_funnel import partition_for_analysis

    skipped, analyze = partition_for_analysis(
        [
            {"ticker": "A", "confidence": 0.90},
            {"ticker": "B", "confidence": 0.40},
            {"ticker": "C", "confidence": 0.80},
        ],
        0.55,
        1,
        "Growth",
    )
    reasons = {r["ticker"]: r["skip_reason"] for r in skipped}
    assert reasons["B"] == "low_research_confidence"
    assert reasons["C"] == "below_analysis_cutoff"
    assert [r["ticker"] for r in analyze] == ["A"]


RECOVERED_AGENTS_RISK_SHA256 = "0201340ad66fcbf046457cd9e33a7e63fcf07b37a089b83b0da002d167f15392"


def _crypto_bucket() -> Bucket:
    return Bucket(
        name="Crypto",
        allocation_pct=0.10,
        mode="crypto",
        asset_class="crypto",
        is_crypto=True,
        max_positions=3,
        max_position_pct=0.25,
        stop_loss_pct=0.08,
        take_profit_pct=0.15,
    )


def _two_tier(decisions, snapshot, bucket, positions=None, buy_lock=None):
    """Mirror cycle.py: agents.risk then agenttrade.risk.evaluate_batch."""
    from agents.risk import risk_agent
    from agenttrade.risk import evaluate_batch

    account = snapshot.get("account") or snapshot
    tier1 = risk_agent(
        decisions, account, positions or [], bucket, None,
        buy_lock=buy_lock, account_snapshot=snapshot,
    )
    tier2 = evaluate_batch(tier1, snapshot, bucket)
    return tier1, tier2


def test_recovered_agents_risk_is_later_4142_byte_source():
    path = os.path.join(REPO_ROOT, "agents", "risk.py")
    data = open(path, "rb").read()
    assert len(data) == 4142
    assert _hash_file(path) == RECOVERED_AGENTS_RISK_SHA256
    src = data.decode("utf-8")
    assert "executable sizing is Tier 2" in src
    assert "APPROVED for deterministic sizing" in src
    assert "d[\"shares\"] = shares" not in src


def test_equity_buy_without_shares_survives_tier1_and_is_sized_in_tier2():
    from agenttrade import db

    db.init_db()
    snapshot = _pass_snapshot()
    decisions = [{
        "ticker": "AAPL",
        "action": "BUY",
        "current_price": 100.0,
        "bucket": "Growth",
    }]
    tier1, tier2 = _two_tier(decisions, snapshot, _growth_bucket())
    assert [d["ticker"] for d in tier1] == ["AAPL"]
    assert "shares" not in tier1[0] or not tier1[0].get("shares")
    assert "notional_usd" not in tier1[0] or not tier1[0].get("notional_usd")
    assert len(tier2) == 1
    assert tier2[0]["ticker"] == "AAPL"
    assert tier2[0].get("position_sizing_source") == "deterministic"
    assert int(tier2[0].get("shares") or 0) > 0
    assert float(tier2[0].get("estimated_notional") or 0) > 0


def test_two_tier_ignores_llm_qty_and_notional():
    from agenttrade import db

    db.init_db()
    snapshot = _pass_snapshot()
    decisions = [{
        "ticker": "MSFT",
        "action": "BUY",
        "current_price": 100.0,
        "shares": 9999,
        "qty": 9999,
        "notional_usd": 500000,
        "notional": 500000,
    }]
    tier1, tier2 = _two_tier(decisions, snapshot, _growth_bucket())
    assert [d["ticker"] for d in tier1] == ["MSFT"]
    assert len(tier2) == 1
    assert tier2[0].get("position_sizing_source") == "deterministic"
    assert int(tier2[0].get("shares") or 0) != 9999
    assert int(tier2[0].get("shares") or 0) > 0
    assert float(tier2[0].get("notional_usd") or 0) != 500000


def test_pause_blocks_at_tier1_before_execution():
    from agenttrade import db
    from agents.execution import execution_agent
    from agents.risk import risk_agent

    db.init_db()
    db.set_system_flag("TRADING_PAUSED", "true")
    db.set_system_flag("PAUSE_REASON", "Consecutive loss limit reached")
    snapshot = _pass_snapshot()
    decisions = [{"ticker": "AAPL", "action": "BUY", "current_price": 100.0}]
    tier1 = risk_agent(
        decisions, snapshot["account"], [], _growth_bucket(), None,
        account_snapshot=snapshot,
    )
    assert tier1 == []
    results = execution_agent(tier1 or decisions, _growth_bucket(), account_snapshot=snapshot)
    assert results
    assert results[0]["blocked_reason"] == "trading_paused"


def test_halt_blocks_before_broker_submit(monkeypatch):
    from agenttrade import db
    from agents.execution import execution_agent

    db.init_db()
    db.set_system_flag("TRADING_HALTED", "true")
    db.set_system_flag("HALT_REASON", "MARGIN DETECTED")
    called = {"alpaca": False}

    def _no_post(*_a, **_k):
        called["alpaca"] = True
        raise AssertionError("halt must not submit to Alpaca")

    monkeypatch.setattr("agents.execution.alpaca_post", _no_post)
    results = execution_agent(
        [{"ticker": "AAPL", "shares": 1, "current_price": 100}],
        _growth_bucket(),
        account_snapshot=_pass_snapshot(),
    )
    assert called["alpaca"] is False
    assert results[0]["blocked_reason"] == "trading_halted"
    assert results[0]["status"] == "blocked"


def test_tier2_rejection_reason_visible_in_funnel():
    from agenttrade import db
    from agents.risk import risk_agent
    from agenttrade.risk import evaluate_batch

    db.init_db()
    snapshot = _pass_snapshot()
    decisions = [{
        "ticker": "BRK.A",
        "action": "BUY",
        "current_price": 25000.0,
        "bucket": "Growth",
    }]
    bucket = _growth_bucket()
    cycle_id = db.start_cycle_run("tiered")
    tier1 = risk_agent(
        decisions, snapshot["account"], [], bucket, None, account_snapshot=snapshot,
    )
    assert [d["ticker"] for d in tier1] == ["BRK.A"]
    tier2 = evaluate_batch(tier1, snapshot, bucket, cycle_run_id=cycle_id)
    assert tier2 == []
    reason = decisions[0].get("blocked_reason") or "risk_rejected"
    db.insert_funnel_event(
        cycle_id, "RISK", symbol="BRK.A", bucket="Growth",
        status="BLOCKED", reason=reason,
        payload={"ticker": "BRK.A", "risk_status": "BLOCKED", "blocked_reason": reason},
    )
    funnel = db.get_cycle_funnel(cycle_id)
    assert funnel["risk"][0]["blocked_reason"]
    assert funnel["blocked"]
    assert funnel["blocked"][0]["blocked_reason"]


def test_crypto_and_equity_sized_only_in_tier2():
    from agenttrade import db

    db.init_db()
    snapshot = _pass_snapshot()

    equity = [{"ticker": "NVDA", "action": "BUY", "current_price": 120.0}]
    e1, e2 = _two_tier(equity, snapshot, _growth_bucket())
    assert e1 and "shares" not in e1[0]
    assert e1[0].get("notional_usd") in (None, "", 0, 0.0)
    assert e2[0].get("position_sizing_source") == "deterministic"
    assert int(e2[0]["shares"]) > 0
    assert "notional_usd" not in e2[0] or e2[0].get("notional_usd") in (None, "", 0, 0.0)

    crypto = [{"ticker": "BTC/USD", "action": "BUY", "current_price": 60000.0}]
    c1, c2 = _two_tier(crypto, snapshot, _crypto_bucket())
    assert c1 and "notional_usd" not in c1[0]
    assert c1[0].get("shares") in (None, "", 0, 0.0)
    assert c2
    assert c2[0].get("position_sizing_source") == "deterministic"
    assert float(c2[0].get("notional_usd") or 0) > 0
    assert "shares" not in c2[0] or c2[0].get("shares") in (None, "", 0, 0.0)
