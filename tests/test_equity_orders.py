"""Equity order bodies that Alpaca would 422: stop side, price ticks, and qty."""

import datetime as dt

import pytest

from buckets import Bucket
from order_utils import (
    build_equity_buy_payload,
    fit_buy_protection,
    plan_equity_buy_vs_open_orders,
)


def _decimals(text: str) -> int:
    if "." not in text:
        return 0
    return len(text.split(".")[-1])


def test_equity_qty_is_whole_shares():
    for shares in (2, 2.0, "3.0", 2.9):
        payload = build_equity_buy_payload("NVDA", shares, order_type="market", current_price=178.23)
        assert payload["qty"] == str(int(float(shares)))
        assert payload["qty"].isdigit()
        assert "." not in payload["qty"]

    with pytest.raises(ValueError):
        build_equity_buy_payload("NVDA", 0.4, order_type="market", current_price=178.23)


def test_bracket_prices_use_equity_ticks_and_stay_below_and_above_market():
    payload = build_equity_buy_payload(
        "NVDA",
        2.0,
        order_type="market",
        current_price=178.23,
        stop_price=165.7891,
        take_profit_price=210.1234,
    )
    stop = payload["stop_loss"]["stop_price"]
    take = payload["take_profit"]["limit_price"]
    assert payload["order_class"] == "bracket"
    assert payload["qty"] == "2"
    assert stop == "165.78"
    assert take == "210.13"
    assert _decimals(stop) <= 2
    assert _decimals(take) <= 2
    assert float(stop) < 178.23 < float(take)
    assert "165.7891" not in stop


def test_stop_above_market_is_pulled_one_tick_below():
    fitted = fit_buy_protection(190.5, 200.0, market_price=178.23)
    assert fitted["stop_adjusted"] is True
    assert fitted["stop_price"] == pytest.approx(178.22)
    assert fitted["stop_price"] < 178.23

    payload = build_equity_buy_payload(
        "MSFT",
        1,
        order_type="limit",
        current_price=100.0,
        limit_price=101.50,
        stop_price=101.25,
        take_profit_price=101.0,
    )
    stop = float(payload["stop_loss"]["stop_price"])
    take = float(payload["take_profit"]["limit_price"])
    limit_px = float(payload["limit_price"])
    assert stop < min(100.0, limit_px)
    assert take > max(100.0, limit_px)
    assert payload["take_profit"]["limit_price"] == "101.51"
    assert _decimals(payload["stop_loss"]["stop_price"]) <= 2
    assert payload["_stop_adjusted"] is True
    assert payload["_take_profit_adjusted"] is True


def test_open_order_plan_cancels_resting_buys_and_skips_partials():
    resting = plan_equity_buy_vs_open_orders("NVDA", [{
        "id": "ord-resting",
        "symbol": "NVDA",
        "side": "buy",
        "status": "new",
        "filled_qty": "0",
    }])
    assert resting["action"] == "cancel_then_submit"
    assert resting["cancel_ids"] == ["ord-resting"]
    assert resting["include_bracket"] is True

    partial = plan_equity_buy_vs_open_orders("MSFT", [{
        "id": "ord-partial",
        "symbol": "MSFT",
        "side": "buy",
        "status": "partially_filled",
        "filled_qty": "1",
    }])
    assert partial["action"] == "skip"
    assert partial["reason"] == "open_buy_partial_fill"

    sell = plan_equity_buy_vs_open_orders("XOM", [{
        "id": "ord-stop",
        "symbol": "XOM",
        "side": "sell",
        "status": "new",
        "filled_qty": "0",
    }])
    assert sell["action"] == "submit"
    assert sell["cancel_ids"] == []
    assert sell["include_bracket"] is False

    payload = build_equity_buy_payload(
        "XOM", 4, order_type="market", current_price=110.0,
        stop_price=100.0, take_profit_price=120.0, include_bracket=False,
    )
    assert "order_class" not in payload
    assert payload["qty"] == "4"


def test_execution_submits_valid_equity_bracket_and_replaces_resting_buy(monkeypatch):
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
    monkeypatch.setattr(cfg.bucket_manager, "tag_position", lambda *_a, **_k: None)
    monkeypatch.setattr("agenttrade.strategy_modes.mode_blocks_order", lambda *_a, **_k: (False, ""))
    monkeypatch.setattr("agenttrade.strategy_modes.get_bucket_execution_mode", lambda *_a, **_k: "live")
    monkeypatch.setattr("agents.execution.check_buy_allowed", lambda *a, **k: (True, "", {"cash": 80000}))
    monkeypatch.setattr("agents.execution.get_account", lambda: {"cash": 80000, "equity": 100000})
    monkeypatch.setattr("agents.execution.enforce_no_margin_order_guard", lambda *_a, **_k: (True, ""))

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 2, 11, 0, 0)

    monkeypatch.setattr("agents.execution.datetime", Frozen)

    posted = {}
    cancelled = []

    def _post(_path, payload):
        posted["payload"] = payload
        return {"id": "ord-new"}

    def _delete(path):
        cancelled.append(path)
        return {}

    monkeypatch.setattr("agents.execution.alpaca_post", _post)
    monkeypatch.setattr("agents.execution.alpaca_delete", _delete)

    snapshot = {
        "account": {"cash": 80000, "equity": 100000, "buying_power": 80000, "portfolio_value": 100000},
        "positions": [],
        "open_orders": [{
            "id": "ord-old",
            "symbol": "NVDA",
            "side": "buy",
            "status": "accepted",
            "filled_qty": "0",
        }],
        "open_buy_notional": 0,
    }
    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth")
    results = execution_agent(
        [{
            "ticker": "NVDA",
            "shares": 2.0,
            "current_price": 178.23,
            "atr_pct": 1.0,
            "stop_loss_price": 190.55,
            "take_profit_price": 210.129,
            "analysis_path": "deterministic_signal",
        }],
        bucket,
        account_snapshot=snapshot,
    )
    assert results[0]["status"] == "placed"
    assert cancelled == ["/v2/orders/ord-old"]
    body = posted["payload"]
    assert body["qty"] == "2"
    assert body["side"] == "buy"
    assert body["order_class"] == "bracket"
    assert set(body) <= {
        "symbol", "qty", "side", "type", "time_in_force", "limit_price",
        "order_class", "stop_loss", "take_profit",
    }
    stop = float(body["stop_loss"]["stop_price"])
    take = float(body["take_profit"]["limit_price"])
    assert stop < 178.23
    assert take > float(body["limit_price"])
    assert _decimals(body["stop_loss"]["stop_price"]) <= 2
    assert _decimals(body["take_profit"]["limit_price"]) <= 2
    assert results[0]["analysis_path"] == "deterministic_signal"


def test_execution_does_not_double_submit_a_partial_buy(monkeypatch):
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
    monkeypatch.setattr("agents.execution.check_buy_allowed", lambda *a, **k: (True, "", {}))
    monkeypatch.setattr("agents.execution.get_account", lambda: {"cash": 80000, "equity": 100000})
    monkeypatch.setattr("agents.execution.enforce_no_margin_order_guard", lambda *_a, **_k: (True, ""))
    called = {"post": 0}
    monkeypatch.setattr(
        "agents.execution.alpaca_post",
        lambda *_a, **_k: called.__setitem__("post", called["post"] + 1),
    )
    snapshot = {
        "account": {"cash": 80000, "equity": 100000},
        "open_orders": [{
            "id": "ord-partial",
            "symbol": "MSFT",
            "side": "buy",
            "status": "partially_filled",
            "filled_qty": "1",
        }],
    }
    results = execution_agent(
        [{"ticker": "MSFT", "shares": 3, "current_price": 400.0, "stop_loss_price": 380, "take_profit_price": 450}],
        Bucket(name="Growth", allocation_pct=0.45, mode="growth"),
        account_snapshot=snapshot,
    )
    assert called["post"] == 0
    assert results[0]["status"] == "blocked"
    assert results[0]["blocked_reason"] == "open_buy_partial_fill"


def test_crypto_market_notional_path_unchanged(monkeypatch):
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
    monkeypatch.setattr(cfg.bucket_manager, "tag_position", lambda *_a, **_k: None)
    monkeypatch.setattr("agenttrade.strategy_modes.mode_blocks_order", lambda *_a, **_k: (False, ""))
    monkeypatch.setattr("agenttrade.strategy_modes.get_bucket_execution_mode", lambda *_a, **_k: "live")
    monkeypatch.setattr("agents.execution.check_buy_allowed", lambda *a, **k: (True, "", {}))
    monkeypatch.setattr("agents.execution.get_account", lambda: {"cash": 80000, "equity": 100000})
    monkeypatch.setattr("agents.execution.enforce_no_margin_order_guard", lambda *_a, **_k: (True, ""))
    posted = {}

    def _post(_path, payload):
        posted["payload"] = payload
        return {"id": "crypto-1"}

    monkeypatch.setattr("agents.execution.alpaca_post", _post)
    bucket = Bucket(name="Crypto", allocation_pct=0.1, mode="crypto", asset_class="crypto", is_crypto=True)
    results = execution_agent(
        [{"ticker": "LINK/USD", "notional_usd": 2600.25, "current_price": 26.0}],
        bucket,
        account_snapshot={"account": {"cash": 80000}, "open_orders": []},
    )
    assert results[0]["status"] == "placed"
    body = posted["payload"]
    assert body["symbol"] == "LINK/USD"
    assert body["type"] == "market"
    assert body["time_in_force"] == "gtc"
    assert body["notional"] == "2600.25"
    assert "order_class" not in body
    assert "qty" not in body
