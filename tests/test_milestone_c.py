"""Milestone C: cycle cash, global held set, no implicit average-down, stress gates."""

from __future__ import annotations

import agent_config as cfg
from agenttrade.risk import CycleRiskState, evaluate_batch
from agents.risk import risk_agent
from buckets import Bucket


def _growth() -> Bucket:
    return Bucket(name="Growth", allocation_pct=0.50, mode="growth", max_positions=8)


def _dividend() -> Bucket:
    return Bucket(name="Dividend", allocation_pct=0.25, mode="dividend", max_positions=8)


def _relax_cash(monkeypatch) -> None:
    monkeypatch.setattr(cfg, "RESERVE_CASH_PCT", 0.0)
    monkeypatch.setattr(cfg, "MIN_CASH_RESERVE", 0.0)
    monkeypatch.setattr(cfg, "MAX_BUYS_PER_BUCKET", 5)
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False)
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr(cfg, "MAX_INVESTED_PCT", 0.90)
    monkeypatch.setattr(cfg, "MAX_ACCOUNT_DRAWDOWN_PCT", 0.10)
    monkeypatch.setattr(cfg, "ALLOW_POSITION_ADDS", False)
    monkeypatch.setattr(cfg, "ALLOW_AVERAGE_DOWN", False)


def _snapshot(cash: float, equity: float, long_mv: float = 0.0, high_water: float | None = None, positions=None) -> dict:
    account = {
        "cash": cash,
        "equity": equity,
        "buying_power": cash,
        "portfolio_value": equity,
        "long_market_value": long_mv,
    }
    snap = {
        "account": account,
        "positions": list(positions or []),
        "open_buy_notional": 0,
        "cash": cash,
        "equity": equity,
        "portfolio_value": equity,
    }
    if high_water is not None:
        snap["high_water_equity"] = high_water
    return snap


def _buy(ticker: str, price: float = 100.0) -> dict:
    # ATR 4 with the growth 2.5x multiplier is a $10 stop. At 0.5% of $10k
    # equity that quotes 5 shares ($500), so two buys do not fit in $550.
    return {"ticker": ticker, "action": "BUY", "current_price": price, "atr": 4.0}


def test_cash_is_decremented_across_multi_approve(monkeypatch):
    from agenttrade import db

    db.init_db()
    _relax_cash(monkeypatch)
    snap = _snapshot(cash=550.0, equity=10000.0, long_mv=0.0, high_water=10000.0)
    decisions = [_buy("AAA"), _buy("BBB")]
    state = CycleRiskState.from_snapshot(snap, [])

    tier1 = risk_agent(
        decisions, snap["account"], [], _growth(), None,
        account_snapshot=snap, cycle_state=state,
    )
    assert [d["ticker"] for d in tier1] == ["AAA"]
    assert decisions[1]["blocked_reason"] == "insufficient_cash"
    assert state.remaining_cash < state.starting_cash
    assert state.remaining_cash < 100

    tier2 = evaluate_batch(tier1, snap, _growth(), cycle_state=state)
    assert len(tier2) == 1
    assert float(tier2[0]["estimated_notional"]) <= 550
    assert state.remaining_cash < 100
    assert state.reserved_total() <= state.starting_cash + 0.01

    # Tier 2 alone also refuses the second buy when the first consumed the cash.
    fresh = [_buy("CCC"), _buy("DDD")]
    fresh_state = CycleRiskState.from_snapshot(snap, [])
    approved = evaluate_batch(fresh, snap, _growth(), cycle_state=fresh_state)
    assert [d["ticker"] for d in approved] == ["CCC"]
    assert fresh[1]["blocked_reason"] == "insufficient_cash"
    assert fresh_state.remaining_cash < fresh_state.starting_cash
    assert sum(float(d["estimated_notional"]) for d in approved) <= 550


def test_global_held_blocks_cross_bucket_duplicate(monkeypatch):
    from agenttrade import db

    db.init_db()
    _relax_cash(monkeypatch)
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {"AAPL": "Growth"})
    positions = [{
        "symbol": "AAPL",
        "qty": 1,
        "market_value": 100,
        "unrealized_pl": 15,
        "current_price": 100,
    }]
    snap = _snapshot(cash=20000.0, equity=30000.0, long_mv=100.0, high_water=30000.0, positions=positions)
    held = [_buy("AAPL")]
    approved = risk_agent(
        held, snap["account"], positions, _dividend(), None, account_snapshot=snap,
    )
    assert approved == []
    assert held[0]["blocked_reason"] == "cross_bucket_duplicate"

    # Same cycle: Growth takes the name before any broker position exists.
    snap_empty = _snapshot(cash=20000.0, equity=30000.0, long_mv=0.0, high_water=30000.0)
    state = CycleRiskState.from_snapshot(snap_empty, [])
    first = [_buy("MSFT")]
    second = [_buy("MSFT")]
    growth_ok = risk_agent(
        first, snap_empty["account"], [], _growth(), None,
        account_snapshot=snap_empty, cycle_state=state,
    )
    dividend_block = risk_agent(
        second, snap_empty["account"], [], _dividend(), None,
        account_snapshot=snap_empty, cycle_state=state,
    )
    assert [d["ticker"] for d in growth_ok] == ["MSFT"]
    assert dividend_block == []
    assert second[0]["blocked_reason"] == "cross_bucket_duplicate"


def test_average_down_blocked_without_explicit_gate(monkeypatch):
    from agenttrade import db

    db.init_db()
    _relax_cash(monkeypatch)
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {"AAPL": "Growth"})
    positions = [{
        "symbol": "AAPL",
        "qty": 1,
        "market_value": 100.0,
        "unrealized_pl": -25.0,
        "current_price": 80.0,
    }]
    snap = _snapshot(cash=20000.0, equity=30000.0, long_mv=100.0, high_water=30000.0, positions=positions)
    bucket = _growth()
    max_dollars = cfg.bucket_manager.max_position_dollars(bucket, 30000.0)
    assert 100.0 < max_dollars * 0.90  # room under the cap is not permission to add

    loser = [_buy("AAPL")]
    approved = risk_agent(
        loser, snap["account"], positions, bucket, None, account_snapshot=snap,
    )
    assert approved == []
    assert loser[0]["blocked_reason"] == "average_down_blocked"

    # Enabling adds still refuses a loser until average-down is explicit.
    monkeypatch.setattr(cfg, "ALLOW_POSITION_ADDS", True)
    still_loser = [_buy("AAPL")]
    approved = risk_agent(
        still_loser, snap["account"], positions, bucket, None, account_snapshot=snap,
    )
    assert approved == []
    assert still_loser[0]["blocked_reason"] == "average_down_blocked"

    monkeypatch.setattr(cfg, "ALLOW_AVERAGE_DOWN", True)
    explicit = [_buy("AAPL")]
    approved = risk_agent(
        explicit, snap["account"], positions, bucket, None, account_snapshot=snap,
    )
    assert [d["ticker"] for d in approved] == ["AAPL"]

    # A winner still needs the add gate; average-down alone is not that gate.
    monkeypatch.setattr(cfg, "ALLOW_POSITION_ADDS", False)
    monkeypatch.setattr(cfg, "ALLOW_AVERAGE_DOWN", False)
    winner_positions = [{
        "symbol": "AAPL",
        "qty": 1,
        "market_value": 100.0,
        "unrealized_pl": 20.0,
        "current_price": 120.0,
    }]
    winner_snap = _snapshot(
        cash=20000.0, equity=30000.0, long_mv=100.0, high_water=30000.0, positions=winner_positions,
    )
    winner = [_buy("AAPL")]
    approved = risk_agent(
        winner, winner_snap["account"], winner_positions, bucket, None, account_snapshot=winner_snap,
    )
    assert approved == []
    assert winner[0]["blocked_reason"] == "add_not_allowed"

    monkeypatch.setattr(cfg, "ALLOW_POSITION_ADDS", True)
    winner_add = [_buy("AAPL")]
    approved = risk_agent(
        winner_add, winner_snap["account"], winner_positions, bucket, None, account_snapshot=winner_snap,
    )
    assert [d["ticker"] for d in approved] == ["AAPL"]


def test_drawdown_and_max_invested_block_when_threshold_hit(monkeypatch):
    from agenttrade import db

    db.init_db()
    _relax_cash(monkeypatch)
    drawn = _snapshot(cash=20000.0, equity=9000.0, long_mv=1000.0, high_water=10000.0)
    draw_decisions = [_buy("AAA")]
    approved = risk_agent(
        draw_decisions, drawn["account"], [], _growth(), None, account_snapshot=drawn,
    )
    assert approved == []
    assert draw_decisions[0]["blocked_reason"] == "drawdown_pause"

    # Tier 2 cannot bypass the same gate.
    tier2 = evaluate_batch(draw_decisions, drawn, _growth())
    assert tier2 == []
    assert draw_decisions[0]["blocked_reason"] == "drawdown_pause"

    stressed = _snapshot(cash=20000.0, equity=10000.0, long_mv=9500.0, high_water=10000.0)
    invested = [_buy("BBB")]
    approved = risk_agent(
        invested, stressed["account"], [], _growth(), None, account_snapshot=stressed,
    )
    assert approved == []
    assert invested[0]["blocked_reason"] == "max_invested"

    # Under the cap before the order, but the quoted notional would cross it.
    crossing = _snapshot(cash=20000.0, equity=10000.0, long_mv=8600.0, high_water=10000.0)
    cross_decisions = [_buy("CCC")]
    approved = risk_agent(
        cross_decisions, crossing["account"], [], _growth(), None, account_snapshot=crossing,
    )
    assert approved == []
    assert cross_decisions[0]["blocked_reason"] == "max_invested"
