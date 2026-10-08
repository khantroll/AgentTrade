"""Opportunity-cost rebalance: decay, swap hysteresis, ownership, and funding."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from buy_guard import check_buy_allowed
from opportunity_rebalance import (
    DEFAULTS,
    execute_opportunity_sells,
    plan_rebalance,
    position_owned_by_agenttrade,
    run_opportunity_cycle,
    sell_orders_from_decisions,
    snapshot_with_sell_proceeds,
)

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
OLD = "2026-09-01T15:00:00+00:00"


@pytest.fixture(autouse=True)
def _clear_opportunity_env(monkeypatch):
    for key in (
        "OPPORTUNITY_REBALANCE",
        "OPPORTUNITY_DECAY_SCORE",
        "OPPORTUNITY_DECAY_DROP",
        "OPPORTUNITY_SWAP_MARGIN",
        "OPPORTUNITY_SWAP_COST_POINTS",
        "OPPORTUNITY_MIN_HOLD_HOURS",
        "OPPORTUNITY_MAX_SWAPS_PER_DAY",
        "OPPORTUNITY_FADE_MIN_GAIN",
        "OPPORTUNITY_FADE_DROP",
        "OPPORTUNITY_FADE_TRIM_PCT",
        "OPPORTUNITY_FADE_CLOSE_DROP",
    ):
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides):
    base = dict(DEFAULTS)
    base.update(overrides)
    return base


def _holding(**overrides):
    base = {
        "symbol": "AAPL",
        "owned": True,
        "score": 60.0,
        "entry_score": 60.0,
        "entry_at": OLD,
        "qty": 10,
        "bucket": "Growth",
        "unrealized_pl_pct": 0.0,
        "crypto": False,
    }
    base.update(overrides)
    return base


def _actions(decisions):
    return [row["action"] for row in decisions]


def test_decay_exit_on_absolute_floor_and_drop_from_entry():
    settings = _settings()
    low = plan_rebalance(
        [_holding(score=10, entry_score=None, entry_at=None)],
        [],
        settings,
        now=NOW,
    )
    assert low[0]["action"] == "decay_exit"
    assert low[0]["qty"] == 10
    assert sell_orders_from_decisions(low)[0]["symbol"] == "AAPL"

    dropped = plan_rebalance(
        [_holding(score=40, entry_score=70)],
        [],
        settings,
        now=NOW,
    )
    assert dropped[0]["action"] == "decay_exit"
    assert "dropped" in dropped[0]["reason"]

    mild = plan_rebalance(
        [_holding(score=60, entry_score=70)],
        [],
        settings,
        now=NOW,
    )
    assert mild[0]["action"] == "hold"
    assert "decay_exit" not in _actions(mild)


def test_decay_still_exits_a_position_bought_this_cycle():
    planned = plan_rebalance(
        [_holding(score=10, entry_score=None, entry_at=NOW.isoformat())],
        [],
        _settings(),
        now=NOW,
        bought_this_cycle={"AAPL"},
    )
    assert planned[0]["action"] == "decay_exit"


def test_swap_requires_margin_plus_fee_points_and_same_bucket():
    settings = _settings()
    weak = _holding(score=50, entry_score=50)
    short = plan_rebalance(
        [weak],
        [{"symbol": "MSFT", "score": 65, "bucket": "Growth"}],
        settings,
        now=NOW,
    )
    assert "swap_sell" not in _actions(short)

    other_bucket = plan_rebalance(
        [weak],
        [{"symbol": "T", "score": 90, "bucket": "Dividend"}],
        settings,
        now=NOW,
    )
    assert "swap_sell" not in _actions(other_bucket)

    cleared = plan_rebalance(
        [weak],
        [{"symbol": "MSFT", "score": 68, "bucket": "Growth"}],
        settings,
        now=NOW,
    )
    sells = [row for row in cleared if row["action"] == "swap_sell"]
    buys = [row for row in cleared if row["action"] == "swap_buy"]
    assert len(sells) == 1
    assert sells[0]["candidate"] == "MSFT"
    assert sells[0]["score"] == 50
    assert sells[0]["candidate_score"] == 68
    assert buys[0]["symbol"] == "MSFT"
    assert "swap_buy" not in [row["action"] for row in sell_orders_from_decisions(cleared)]


def test_swap_hysteresis_min_hold_same_cycle_cap_and_lock():
    candidate = [{"symbol": "MSFT", "score": 90, "bucket": "Growth"}]
    settings = _settings()

    young = plan_rebalance(
        [_holding(entry_at=(NOW - timedelta(hours=1)).isoformat(), score=50, entry_score=50)],
        candidate,
        settings,
        now=NOW,
    )
    assert "swap_sell" not in _actions(young)
    assert any("held under" in row["reason"] for row in young)

    same_cycle = plan_rebalance(
        [_holding(score=50, entry_score=50)],
        candidate,
        _settings(min_hold_hours=0),
        now=NOW,
        bought_this_cycle={"AAPL"},
    )
    assert "swap_sell" not in _actions(same_cycle)
    assert any("bought this cycle" in row["reason"] for row in same_cycle)

    capped = plan_rebalance(
        [_holding(score=50, entry_score=50)],
        candidate,
        settings,
        now=NOW,
        swaps_today=1,
    )
    assert "swap_sell" not in _actions(capped)
    assert any("swap cap" in row["reason"] for row in capped)

    locked = plan_rebalance(
        [_holding(score=50, entry_score=50)],
        candidate,
        settings,
        now=NOW,
        locked_symbols={"MSFT"},
    )
    assert "swap_sell" not in _actions(locked)


def test_toggle_off_plans_nothing_to_sell():
    planned = plan_rebalance(
        [_holding(score=10, entry_score=80)],
        [{"symbol": "MSFT", "score": 99, "bucket": "Growth"}],
        _settings(enabled=False),
        now=NOW,
    )
    assert len(planned) == 1
    assert planned[0]["action"] == "disabled"
    assert sell_orders_from_decisions(planned) == []


def test_profit_take_trims_a_winner_whose_score_faded():
    planned = plan_rebalance(
        [_holding(score=60, entry_score=70, unrealized_pl_pct=0.05, qty=9)],
        [],
        _settings(),
        now=NOW,
    )
    trims = [row for row in planned if row["action"] == "fade_trim"]
    assert len(trims) == 1
    assert trims[0]["qty"] == 2
    assert trims[0]["score"] == 60
    assert trims[0]["entry_score"] == 70

    closed = plan_rebalance(
        [_holding(score=54, entry_score=70, unrealized_pl_pct=0.05, qty=9)],
        [],
        _settings(),
        now=NOW,
    )
    assert closed[0]["action"] == "fade_close"
    assert closed[0]["qty"] == 9


def test_ownership_filter_ignores_foreign_and_manual_orders():
    assert position_owned_by_agenttrade(
        "AAPL",
        [{"symbol": "AAPL", "side": "buy", "client_order_id": "agenttrade-abc"}],
    )
    assert not position_owned_by_agenttrade(
        "AAPL",
        [{"symbol": "AAPL", "side": "buy", "client_order_id": "cryptoagent-hype"}],
    )
    assert not position_owned_by_agenttrade(
        "AAPL",
        [{"symbol": "AAPL", "side": "buy", "client_order_id": ""}],
    )
    assert not position_owned_by_agenttrade(
        "LINKUSD",
        [{"symbol": "LINKUSD", "side": "buy", "client_order_id": "agenttrade-manual-stop-1"}],
    )
    assert position_owned_by_agenttrade(
        "SOLUSD",
        [{"symbol": "SOL/USD", "side": "buy", "client_order_id": "agenttrade-sol"}],
    )

    foreign = plan_rebalance(
        [_holding(symbol="HYPEUSD", owned=False, score=5, bucket="Crypto")],
        [{"symbol": "SOLUSD", "score": 90, "bucket": "Crypto"}],
        _settings(),
        now=NOW,
    )
    assert foreign[0]["action"] == "skip"
    assert foreign[0]["owned"] is False
    assert sell_orders_from_decisions(foreign) == []


def test_sell_proceeds_fund_the_following_buy(monkeypatch):
    import agent_config as cfg

    monkeypatch.setattr(cfg, "MIN_CASH_RESERVE", 5000.0)
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False)
    snap = {
        "account": {"cash": 5500.0, "buying_power": 5500.0},
        "cash": 5500.0,
        "buying_power": 5500.0,
        "agenttrade_open_buy_notional": 0,
        "open_buy_notional": 0,
    }
    allowed, _reason, _ctx = check_buy_allowed(
        800, snapshot=snap, buy_lock={"locks": []}, symbol="MSFT",
    )
    assert allowed is False

    funded = snapshot_with_sell_proceeds(
        snap,
        [{"status": "placed", "side": "sell", "estimated_proceeds": 800}],
        5500.0,
    )
    assert funded["cash"] == 6300.0
    assert funded["account"]["cash"] == 6300.0
    allowed_after, _reason_after, ctx = check_buy_allowed(
        800, snapshot=funded, buy_lock={"locks": []}, symbol="MSFT",
    )
    assert allowed_after is True
    assert ctx["projected_cash"] == 5500.0

    already = snapshot_with_sell_proceeds(funded, [
        {"status": "placed", "side": "sell", "estimated_proceeds": 800},
    ], 5500.0)
    assert already["cash"] == 6300.0


def test_execute_crypto_gtc_equity_day_and_no_daily_trade_increment(monkeypatch):
    import agent_config as cfg

    def _forbid(*_args, **_kwargs):
        raise AssertionError("opportunity sells must not increment daily trades")

    monkeypatch.setattr(cfg, "increment_daily_trades", _forbid)
    captured = {}

    def _post(_endpoint, payload):
        captured["payload"] = payload
        return {"id": "ord-1"}

    monkeypatch.setattr("alpaca_client.alpaca_post", _post)
    crypto = execute_opportunity_sells(
        [{
            "action": "decay_exit",
            "symbol": "LINKUSD",
            "owned": True,
            "qty": 1.5,
            "bucket": "Crypto",
            "reason": "decay exit",
            "score": 10,
            "entry_score": 40,
        }],
        [{"symbol": "LINKUSD", "qty": 1.5, "current_price": 13.0, "asset_class": "crypto"}],
        market_open=False,
        open_orders=[],
    )
    assert crypto[0]["status"] == "placed"
    assert captured["payload"]["time_in_force"] == "gtc"
    assert crypto[0]["estimated_proceeds"] == 19.5

    equity = execute_opportunity_sells(
        [{
            "action": "swap_sell",
            "symbol": "AAPL",
            "owned": True,
            "qty": 4,
            "bucket": "Growth",
            "reason": "swap",
            "score": 40,
            "entry_score": 40,
            "candidate": "MSFT",
            "candidate_score": 80,
        }],
        [{"symbol": "AAPL", "qty": 4, "current_price": 100.0, "asset_class": "us_equity"}],
        market_open=True,
        open_orders=[],
    )
    assert equity[0]["status"] == "placed"
    assert captured["payload"]["time_in_force"] == "day"
    assert captured["payload"]["client_order_id"].startswith("agenttrade-")
    assert not captured["payload"]["client_order_id"].startswith("agenttrade-manual-")

    deferred = execute_opportunity_sells(
        [{
            "action": "decay_exit",
            "symbol": "AAPL",
            "owned": True,
            "qty": 4,
            "bucket": "Growth",
            "reason": "decay",
            "score": 10,
            "entry_score": 40,
        }],
        [{"symbol": "AAPL", "qty": 4, "current_price": 100.0}],
        market_open=False,
        open_orders=[],
    )
    assert deferred[0]["status"] == "deferred"


def test_entry_score_persists_once_and_a_placed_decay_clears_it(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTTRADE_DB_PATH", str(tmp_path / "opp.sqlite3"))
    monkeypatch.setattr("alpaca_client.alpaca_post", lambda _endpoint, _payload: {"id": "ord-decay"})
    import agenttrade.db as db

    db._DB_INITIALIZED = False
    db.init_db()
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO orders(symbol, side, client_order_id, submitted_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("AAPL", "buy", "agenttrade-abc", OLD, "filled"),
        )

    positions = [{
        "symbol": "AAPL",
        "qty": 4,
        "current_price": 100.0,
        "unrealized_plpc": 0.01,
        "asset_class": "us_equity",
    }]
    tags = {"AAPL": "Growth"}
    first = run_opportunity_cycle(
        positions=positions,
        decisions=[{
            "ticker": "AAPL",
            "action": "SKIP",
            "signal_strength": 70,
            "bucket": "Growth",
        }],
        tags=tags,
        market_open=True,
        now=NOW,
        open_orders=[],
        buy_lock={"locked_symbols": []},
        cycle_started_at=NOW,
    )
    assert first["decisions"][0]["action"] == "hold"
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT entry_score, last_score FROM position_entry_scores WHERE symbol='AAPL'"
        ).fetchone()
    assert row["entry_score"] == 70
    assert row["last_score"] == 70

    second = run_opportunity_cycle(
        positions=positions,
        decisions=[{
            "ticker": "AAPL",
            "action": "SKIP",
            "signal_strength": 40,
            "bucket": "Growth",
        }],
        tags=tags,
        market_open=True,
        now=NOW,
        open_orders=[],
        buy_lock={"locked_symbols": []},
        cycle_started_at=NOW,
    )
    assert second["decisions"][0]["action"] == "decay_exit"
    assert second["orders"][0]["status"] == "placed"
    with db.get_connection() as conn:
        left = conn.execute("SELECT COUNT(*) AS n FROM position_entry_scores").fetchone()
    assert left["n"] == 0
