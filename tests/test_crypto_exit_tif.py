"""Crypto exits must not be sent as equity day orders.

Alpaca reports LINKUSD / SOLUSD. bucket_tags.json stores SOL/USD, and LINK
was never tagged. Those positions used to take the equity 7% stop and
time_in_force=day, which Alpaca rejects with HTTP 422.
"""

import json

import pytest

import agent_config as cfg
from order_utils import order_time_in_force


def _pos(symbol, price, entry, qty=1.5, asset_class=None):
    pos = {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": entry,
        "current_price": price,
        "market_value": round(qty * price, 2),
    }
    if asset_class:
        pos["asset_class"] = asset_class
    return pos


@pytest.fixture
def review_calls(monkeypatch):
    import agents.position_review as review

    calls = []

    def _post(path, payload):
        calls.append(payload)
        return {"id": "sell-1"}

    monkeypatch.setattr(review, "get_open_orders", lambda: [])
    monkeypatch.setattr(review, "alpaca_post", _post)
    monkeypatch.setattr(cfg.bucket_manager, "untag_position", lambda symbol: None)
    monkeypatch.setattr(cfg, "daily_trades", 0)
    monkeypatch.setattr(cfg, "SELLING_ENABLED", True)
    return review, calls


def test_order_time_in_force_rejects_day_for_crypto_symbols():
    assert order_time_in_force("LINKUSD") == "gtc"
    assert order_time_in_force("SOL/USD") == "gtc"
    assert order_time_in_force({"symbol": "SOLUSD", "asset_class": "crypto"}) == "gtc"
    assert order_time_in_force("AAPL") == "day"
    assert order_time_in_force("NVDA", bucket_is_crypto=False) == "day"


def test_untagged_linkusd_uses_crypto_stop_and_gtc(review_calls, monkeypatch):
    review, calls = review_calls
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {})

    # $13.00 is through the old 7% stop ($13.07) and still above the 8% crypto stop.
    held = review.review_positions(
        [_pos("LINKUSD", 13.00, 14.06)],
        {"portfolio_value": 100000},
        {},
        True,
    )
    assert held == []
    assert calls == []

    review.review_positions(
        [_pos("LINKUSD", 12.90, 14.06)],
        {"portfolio_value": 100000},
        {},
        False,
    )
    assert len(calls) == 1
    assert calls[0]["symbol"] == "LINKUSD"
    assert calls[0]["side"] == "sell"
    assert calls[0]["time_in_force"] == "gtc"
    assert calls[0]["time_in_force"] != "day"


def test_solusd_matches_slash_tag_and_uses_crypto_stop(review_calls, monkeypatch):
    review, calls = review_calls
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {"SOL/USD": "Crypto"})

    # Host diagnosis: 7% stop about $111.41, price about $112.20, entry ~$119.80.
    # $111.00 is through that 7% stop and still above the 8% crypto stop ($110.22).
    above_crypto_stop = review.review_positions(
        [_pos("SOLUSD", 111.00, 119.80, asset_class="crypto")],
        {"portfolio_value": 100000},
        {},
        True,
    )
    assert above_crypto_stop == []
    assert calls == []

    sells = review.review_positions(
        [_pos("SOLUSD", 110.00, 119.80, asset_class="crypto")],
        {"portfolio_value": 100000},
        {},
        True,
    )
    assert len(sells) == 1
    assert sells[0]["status"] == "placed"
    assert sells[0]["bucket"] == "Crypto"
    assert calls[0]["time_in_force"] == "gtc"
    assert "8%" in sells[0]["rationale"] or "stop-loss" in sells[0]["rationale"]
    assert "110.22" in sells[0]["rationale"] or "110.21" in sells[0]["rationale"]


def test_equity_stop_still_uses_day(review_calls, monkeypatch):
    review, calls = review_calls
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {})
    sells = review.review_positions(
        [_pos("AAPL", 90.0, 100.0, qty=4, asset_class="us_equity")],
        {"portfolio_value": 100000},
        {},
        True,
    )
    assert sells[0]["status"] == "placed"
    assert calls[0]["time_in_force"] == "day"


def test_failed_crypto_exit_is_reported_and_retried(review_calls, monkeypatch, caplog):
    review, calls = review_calls
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {})
    untagged = []
    monkeypatch.setattr(cfg.bucket_manager, "untag_position", lambda symbol: untagged.append(symbol))

    def _reject(path, payload):
        calls.append(payload)
        raise RuntimeError("HTTP 422 invalid crypto time_in_force")

    monkeypatch.setattr(review, "alpaca_post", _reject)
    with caplog.at_level("INFO"):
        first = review.review_positions(
            [_pos("LINKUSD", 12.90, 14.06)],
            {"portfolio_value": 100000},
            {},
            True,
        )
    assert first[0]["status"] == "failed"
    assert first[0]["retry_next_cycle"] is True
    assert "422" in first[0]["error"]
    assert untagged == []
    assert "No exit conditions triggered" not in caplog.text
    assert "EXIT FAILED" in caplog.text
    assert calls[0]["time_in_force"] == "gtc"

    second = review.review_positions(
        [_pos("LINKUSD", 12.90, 14.06)],
        {"portfolio_value": 100000},
        {},
        True,
    )
    assert second[0]["status"] == "failed"
    assert len(calls) == 2


def test_untag_drops_slash_spelling(tmp_path, monkeypatch):
    path = tmp_path / "bucket_tags.json"
    path.write_text(json.dumps({"SOL/USD": "Crypto", "AAPL": "Growth"}), encoding="utf-8")
    monkeypatch.setattr(cfg.bucket_manager, "TAG_FILE", str(path))
    cfg.bucket_manager.untag_position("SOLUSD")
    tags = json.loads(path.read_text(encoding="utf-8"))
    assert "SOL/USD" not in tags
    assert tags["AAPL"] == "Growth"


def test_hard_rebalance_crypto_symbol_uses_gtc_without_crypto_flag(monkeypatch):
    import agents.execution as execution

    posted = []
    monkeypatch.setenv("HARD_REBALANCE", "true")
    monkeypatch.setattr(cfg, "SELLING_ENABLED", True)
    monkeypatch.setattr(cfg, "daily_trades", 0)
    monkeypatch.setattr(execution, "get_open_orders", lambda: [])
    monkeypatch.setattr(execution, "alpaca_post", lambda path, payload: posted.append(payload) or {"id": "rb"})
    monkeypatch.setattr(cfg.bucket_manager, "untag_position", lambda symbol: None)
    monkeypatch.setattr(
        cfg.bucket_manager,
        "plan_hard_rebalance",
        lambda *a, **k: [{
            "symbol": "SOLUSD",
            "qty": 0.25,
            "is_crypto": False,
            "asset_class": "us_equity",
            "bucket": "Growth",
            "reason": "drift",
        }],
    )
    orders, _plans = execution.hard_rebalance_agent({}, [{"symbol": "SOLUSD", "qty": 1}], 100000, False)
    assert orders[0]["status"] == "placed"
    assert posted[0]["time_in_force"] == "gtc"


def test_legacy_post_sell_cooldown_env(monkeypatch, caplog):
    monkeypatch.delenv("POST_SELL_COOLDOWN_HOURS", raising=False)
    monkeypatch.setenv("POST_SELL_COOLDOWN", "24")
    with caplog.at_level("WARNING"):
        cfg.refresh_config()
    assert cfg.POST_SELL_COOLDOWN_HOURS == 24.0
    assert "deprecated" in caplog.text

    monkeypatch.setenv("POST_SELL_COOLDOWN_HOURS", "6")
    monkeypatch.setenv("POST_SELL_COOLDOWN", "24")
    cfg.refresh_config()
    assert cfg.POST_SELL_COOLDOWN_HOURS == 6.0
    monkeypatch.delenv("POST_SELL_COOLDOWN", raising=False)
    monkeypatch.setenv("POST_SELL_COOLDOWN_HOURS", "24")
    cfg.refresh_config()


def test_manual_stop_sell_without_lot_does_not_fault_the_ledger():
    from agenttrade import db
    from agenttrade.ledger import process_fill_into_lot_ledger

    db.init_db()
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fills (alpaca_fill_id, filled_at, symbol, side, qty, price, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fill-link-manual",
                "2026-10-08T14:05:00+00:00",
                "LINKUSD",
                "sell",
                1.0,
                13.0,
                json.dumps({"client_order_id": "agenttrade-manual-stop-link"}),
            ),
        )
        fill_id = conn.execute("SELECT id FROM fills WHERE alpaca_fill_id=?", ("fill-link-manual",)).fetchone()[0]

    result = process_fill_into_lot_ledger(int(fill_id))
    assert result["ok"] is True
    assert result["action"] == "sell_no_cost_basis"
    assert "agenttrade-manual-stop-link" in result["message"]
    events = db.get_recent_risk_events(10)
    assert events
    assert all(row["severity"] != "critical" for row in events)
    assert events[0]["event_type"] == "LEDGER_SELL_NO_COST_BASIS"


def test_sell_matches_lot_stored_under_slash_symbol():
    from agenttrade import db
    from agenttrade.ledger import process_fill_into_lot_ledger

    db.init_db()
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fills (alpaca_fill_id, filled_at, symbol, side, qty, price)
            VALUES ('buy-link', '2026-10-01T14:00:00+00:00', 'LINK/USD', 'buy', 1.0, 14.06)
            """
        )
        buy_id = conn.execute("SELECT id FROM fills WHERE alpaca_fill_id='buy-link'").fetchone()[0]
        conn.execute(
            """
            INSERT INTO fills (alpaca_fill_id, filled_at, symbol, side, qty, price, raw_json)
            VALUES ('sell-link', '2026-10-08T14:05:00+00:00', 'LINKUSD', 'sell', 1.0, 13.0, ?)
            """,
            (json.dumps({"client_order_id": "agenttrade-manual-stop-link"}),),
        )
        sell_id = conn.execute("SELECT id FROM fills WHERE alpaca_fill_id='sell-link'").fetchone()[0]
    db.insert_trade_lot(
        "LINK/USD", "crypto", "Crypto", int(buy_id), "ord-buy",
        "2026-10-01T14:00:00+00:00", 1.0, 14.06,
    )
    result = process_fill_into_lot_ledger(int(sell_id))
    assert result["ok"] is True
    assert result["action"] == "sell_matched"
    assert result["matches_created"] == 1


def test_manual_stop_fill_is_an_exit_not_a_daily_trade():
    from trading_day import explain_trades_for_day

    report = explain_trades_for_day(
        [{
            "order_id": "ord-manual",
            "symbol": "LINKUSD",
            "side": "sell",
            "status": "filled",
            "client_order_id": "agenttrade-manual-stop-link",
            "filled_at": "2026-10-08T14:05:00+00:00",
        }],
        [],
        now="2026-10-08T15:00:00+00:00",
        owned_order_ids=set(),
    )
    assert report["count"] == 0
    assert report["excluded"]
    assert "exit" in report["excluded"][0]["reason"]
