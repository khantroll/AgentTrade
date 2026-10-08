"""A sell cooldown blocks only that AgentTrade symbol."""

from datetime import datetime, timedelta

from buckets import Bucket


def _unlock() -> str:
    return (datetime.now() + timedelta(hours=12)).isoformat()


def _lock(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "asset_class": "crypto",
        "scope": "symbol",
        "reason": "recent_sell",
        "locked_at": datetime.now().isoformat(),
        "unlock_at": _unlock(),
    }


def _buy(ticker: str) -> dict:
    return {"ticker": ticker, "action": "BUY", "current_price": 10.0, "atr": 0.4}


def _snapshot() -> dict:
    account = {
        "cash": 100000.0,
        "equity": 100000.0,
        "buying_power": 100000.0,
        "portfolio_value": 100000.0,
        "long_market_value": 0.0,
    }
    return {
        "account": account,
        "positions": [],
        "open_buy_notional": 0,
        "agenttrade_open_buy_notional": 0,
        "cash": 100000.0,
        "equity": 100000.0,
        "portfolio_value": 100000.0,
        "high_water_equity": 100000.0,
    }


def _relax(monkeypatch) -> None:
    import agent_config as cfg

    monkeypatch.setattr(cfg, "RESERVE_CASH_PCT", 0.0)
    monkeypatch.setattr(cfg, "MIN_CASH_RESERVE", 0.0)
    monkeypatch.setattr(cfg, "MAX_BUYS_PER_BUCKET", 10)
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False)
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr(cfg, "ALLOW_SAME_DAY_REBUY", False)
    monkeypatch.setattr(cfg, "MAX_INVESTED_PCT", 0.90)
    monkeypatch.setattr(cfg, "MAX_ACCOUNT_DRAWDOWN_PCT", 0.10)
    monkeypatch.setattr("agents.risk.ledger.trading_paused", lambda: False)


def test_symbol_locks_do_not_block_other_buys(monkeypatch):
    from agents.risk import risk_agent

    _relax(monkeypatch)
    buy_lock = {
        "active": True,
        "scope": "symbol",
        "message": "SYMBOL LOCK: LINK, PEPE, SOLUSD",
        "locks": [_lock("LINK"), _lock("PEPEUSD"), _lock("SOLUSD")],
    }
    picks = ["GOOGL", "AAPL", "TENB", "VRNS", "MO", "PEP", "BTC/USD", "SOL/USD"]
    decisions = [_buy(ticker) for ticker in picks]
    snap = _snapshot()
    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth", max_positions=20)
    approved = risk_agent(
        decisions, snap["account"], [], bucket, buy_lock=buy_lock, account_snapshot=snap,
    )
    approved_names = [row["ticker"] for row in approved]
    assert approved_names == ["GOOGL", "AAPL", "TENB", "VRNS", "MO", "PEP", "BTC/USD"]
    sol = decisions[-1]
    assert sol["ticker"] == "SOL/USD"
    assert str(sol["blocked_reason"]).startswith("symbol_lock:")

    again = [_buy("SOLUSD")]
    blocked = risk_agent(
        again, snap["account"], [], bucket, buy_lock=buy_lock, account_snapshot=snap,
    )
    assert blocked == []
    assert str(again[0]["blocked_reason"]).startswith("symbol_lock:")


def test_global_buying_disabled_still_blocks_every_buy(monkeypatch):
    from agents.risk import risk_agent

    _relax(monkeypatch)
    snap = _snapshot()
    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth", max_positions=20)
    approved = risk_agent(
        [_buy("AAPL")],
        snap["account"],
        [],
        bucket,
        buy_lock={"active": True, "scope": "global", "locks": [], "message": "GLOBAL BUY LOCK"},
        account_snapshot=snap,
    )
    assert approved == []


def test_solusd_and_sol_slash_usd_are_the_same_lock():
    import buy_lock

    assert buy_lock.symbols_match("SOLUSD", "SOL/USD")
    assert buy_lock.symbols_match("SOL", "SOL/USD")
    assert buy_lock.symbols_match("LINKUSD", "LINK/USD")
    assert not buy_lock.symbols_match("AAPL", "SOL/USD")
    now = datetime.now()
    status = {
        "locks": [_lock("SOLUSD")],
    }
    locked, reason, _detail = buy_lock.is_buy_locked(
        symbol="SOL/USD", buy_lock=status, now=now,
    )
    assert locked is True
    assert reason == "recent_sell"
    open_name, _, _ = buy_lock.is_buy_locked(symbol="AAPL", buy_lock=status, now=now)
    assert open_name is False


def test_foreign_and_cryptoagent_sells_do_not_lock():
    import buy_lock

    now = datetime.now()
    foreign = {
        "side": "sell",
        "ticker": "PEPEUSD",
        "order_id": "ord-pepe",
        "client_order_id": "cryptoagent-pepe-close",
        "submitted_at": now.isoformat(),
    }
    operator = {
        "side": "sell",
        "ticker": "PEPE/USD",
        "order_id": "ord-pepe-operator",
        "client_order_id": "operator-close",
        "submitted_at": now.isoformat(),
    }
    ctx = buy_lock.sync_sell_fills({}, [foreign, operator], now=now)
    assert not ctx.get("symbol_locks")
    assert int(ctx.get("daily_sells_today") or 0) == 0
    assert "ord-pepe" in ctx["processed_fill_ids"]


def test_agenttrade_sell_and_manual_close_lock_that_symbol():
    import buy_lock

    now = datetime.now()
    fills = [
        {
            "side": "sell",
            "ticker": "SOLUSD",
            "order_id": "ord-sol",
            "client_order_id": "agenttrade-stop-sol",
            "submitted_at": now.isoformat(),
        },
        {
            "side": "sell",
            "ticker": "LINKUSD",
            "order_id": "ord-link",
            "client_order_id": "agenttrade-manual-stop-link",
            "submitted_at": now.isoformat(),
        },
    ]
    ctx = buy_lock.sync_sell_fills({}, fills, now=now)
    locked = {row["symbol"] for row in ctx["symbol_locks"]}
    assert "SOLUSD" in locked
    assert "LINKUSD" in locked
    assert int(ctx["daily_sells_today"]) == 2
    sol_locked, _, _ = buy_lock.is_buy_locked(
        symbol="SOL/USD", buy_lock={"locks": ctx["symbol_locks"]}, now=now,
    )
    link_locked, _, _ = buy_lock.is_buy_locked(
        symbol="LINK/USD", buy_lock={"locks": ctx["symbol_locks"]}, now=now,
    )
    assert sol_locked is True
    assert link_locked is True


def test_fill_without_client_id_uses_the_sqlite_order(monkeypatch):
    from agenttrade import db
    import buy_lock

    db.init_db()
    cycle_id = db.start_cycle_run("paper")
    db.record_submitted_order(cycle_id, {
        "order_id": "ord-sol-ledger",
        "symbol": "SOLUSD",
        "side": "sell",
        "status": "filled",
        "client_order_id": "agenttrade-stop-sol",
    })
    db.record_submitted_order(cycle_id, {
        "order_id": "ord-pepe-ledger",
        "symbol": "PEPEUSD",
        "side": "sell",
        "status": "filled",
        "client_order_id": "cryptoagent-pepe",
    })
    now = datetime.now()
    ctx = buy_lock.sync_sell_fills({}, [
        {"side": "sell", "ticker": "SOLUSD", "order_id": "ord-sol-ledger", "submitted_at": now.isoformat()},
        {"side": "sell", "ticker": "PEPEUSD", "order_id": "ord-pepe-ledger", "submitted_at": now.isoformat()},
    ], now=now)
    symbols = {row["symbol"] for row in ctx.get("symbol_locks") or []}
    assert any(buy_lock.symbols_match(sym, "SOL/USD") for sym in symbols)
    assert not any(buy_lock.symbols_match(sym, "PEPEUSD") for sym in symbols)


def test_load_drops_foreign_pepe_lock_and_keeps_ours():
    from agenttrade import db
    import buy_lock

    db.init_db()
    cycle_id = db.start_cycle_run("paper")
    db.record_submitted_order(cycle_id, {
        "order_id": "ord-sol-keep",
        "symbol": "SOLUSD",
        "side": "sell",
        "status": "filled",
        "client_order_id": "agenttrade-stop-sol",
    })
    db.record_submitted_order(cycle_id, {
        "order_id": "ord-link-keep",
        "symbol": "LINKUSD",
        "side": "sell",
        "status": "filled",
        "client_order_id": "agenttrade-manual-stop-link",
    })
    unlock = _unlock()
    db.set_buy_lock_state({
        "symbol_locks": [_lock("LINKUSD"), _lock("PEPEUSD"), _lock("SOLUSD")],
        "last_sell_symbols": ["LINKUSD", "PEPEUSD", "SOLUSD"],
        "last_sell_at": datetime.now().isoformat(),
    })
    loaded = buy_lock.load_prior_state()
    symbols = {row["symbol"] for row in loaded["symbol_locks"]}
    assert "PEPEUSD" not in symbols
    assert "SOLUSD" in symbols
    assert "LINKUSD" in symbols
    assert "PEPEUSD" not in loaded["last_sell_symbols"]
    assert "SOLUSD" in loaded["last_sell_symbols"]
    assert unlock  # lock rows carry a future unlock; load does not expire them


def test_filled_exit_replaces_the_sqlite_position_book():
    from agenttrade import db

    db.init_db()
    cycle_id = db.start_cycle_run("paper")
    db.insert_account_snapshot(cycle_id, "alpaca", {
        "equity": 10000,
        "cash": 8000,
        "buying_power": 8000,
        "portfolio_value": 10000,
        "long_market_value": 200,
    })
    db.insert_positions(cycle_id, "alpaca", [
        {"symbol": "SOLUSD", "qty": 1, "current_price": 100, "market_value": 100},
        {"symbol": "AAPL", "qty": 1, "current_price": 100, "market_value": 100},
    ])
    db.insert_positions(cycle_id, "alpaca", [
        {"symbol": "AAPL", "qty": 1, "current_price": 100, "market_value": 100},
    ])
    assert {row["symbol"] for row in db.get_latest_positions()} == {"AAPL"}
    db.insert_positions(cycle_id, "alpaca", [])
    assert db.get_latest_positions() == []
