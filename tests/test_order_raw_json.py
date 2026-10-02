"""orders.raw_json stays KB-scale and is not reloaded into the dashboard."""

import json

from agenttrade.state_size import MAX_ORDER_RAW_BYTES, order_raw_json


def _fat_order() -> dict:
    blob = "STATE_BLOB " + ("screener mention " * 4000)
    nested = {
        "screener_sources": {"Growth": {"reddit_intelligence": {"mentions": [blob]}}},
        "ohlcv": {"closes": [blob]},
        "prompt": blob,
        "raw_response": blob,
        "decisions": [{"ticker": "NVDA", "ohlcv": blob}],
        "raw_json": blob,
    }
    return {
        "id": "ord-nvda-1",
        "symbol": "NVDA",
        "side": "buy",
        "qty": "2",
        "status": "new",
        "type": "limit",
        "limit_price": "178.50",
        "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": "165.78"},
        "take_profit": {"limit_price": "210.13"},
        "analysis_path": "deterministic_signal",
        "rationale": "screener signal_strength 42 supports BUY",
        "screener_sources": nested["screener_sources"],
        "ohlcv": nested["ohlcv"],
        "prompt": blob,
        "raw_json": json.dumps(nested),
        "legs": [
            {
                "id": "leg-stop",
                "symbol": "NVDA",
                "side": "sell",
                "type": "stop",
                "stop_price": "165.78",
                "status": "held",
                "qty": "2",
                "raw_json": blob,
                "ohlcv": nested["ohlcv"],
            }
        ],
    }


def test_order_raw_json_drops_nested_state():
    encoded = order_raw_json(_fat_order())
    assert len(encoded.encode("utf-8")) <= MAX_ORDER_RAW_BYTES
    assert "STATE_BLOB" not in encoded
    parsed = json.loads(encoded)
    assert parsed["symbol"] == "NVDA"
    assert parsed["qty"] == "2"
    assert parsed["analysis_path"] == "deterministic_signal"
    assert parsed["stop_loss"]["stop_price"] == "165.78"
    assert parsed["legs"][0]["stop_price"] == "165.78"
    assert "raw_json" not in parsed
    assert "ohlcv" not in parsed
    assert "screener_sources" not in parsed
    assert "raw_json" not in parsed["legs"][0]


def test_record_submitted_order_stores_slim_blob():
    from agenttrade import db

    db.init_db()
    cycle_id = db.start_cycle_run("paper")
    order = _fat_order()
    order["order_id"] = order["id"]
    order["ticker"] = "NVDA"
    order["shares"] = 2
    db.record_submitted_order(cycle_id, order, strategy_name="Growth")

    with db.get_connection() as conn:
        row = conn.execute("SELECT symbol, side, qty, raw_json FROM orders").fetchone()
    assert row["symbol"] == "NVDA"
    assert row["side"] == "buy"
    assert "STATE_BLOB" not in (row["raw_json"] or "")
    assert len(row["raw_json"]) <= MAX_ORDER_RAW_BYTES
    stored = json.loads(row["raw_json"])
    assert stored["analysis_path"] == "deterministic_signal"
    assert "raw_json" not in stored

    # A second save of the stored blob plus another fat embed does not grow.
    again = dict(stored)
    again["id"] = "ord-nvda-1"
    again["raw_json"] = row["raw_json"] + ("STATE_BLOB " * 1000)
    again["ohlcv"] = {"closes": ["STATE_BLOB"] * 1000}
    db.sync_open_orders_from_alpaca([again])
    with db.get_connection() as conn:
        rewritten = conn.execute("SELECT raw_json FROM orders WHERE alpaca_order_id='ord-nvda-1'").fetchone()
    assert "STATE_BLOB" not in rewritten["raw_json"]
    assert len(rewritten["raw_json"]) <= MAX_ORDER_RAW_BYTES


def test_open_order_load_does_not_rehydrate_fat_raw_json():
    from agenttrade import db
    from agenttrade.publish import build_dashboard_state

    db.init_db()
    blob = "STATE_BLOB " + ("x" * 200_000)
    fat = json.dumps({
        "id": "ord-fat",
        "symbol": "MSFT",
        "side": "buy",
        "status": "new",
        "raw_json": blob,
        "screener_sources": {"Growth": blob},
        "ohlcv": blob,
    })
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO orders(
                cycle_run_id, alpaca_order_id, submitted_at, symbol, side, qty,
                status, order_type, stop_price, raw_json
            ) VALUES (NULL, 'ord-fat', '2026-10-02T20:00:00+00:00', 'MSFT', 'buy', 3,
                      'new', 'market', NULL, ?)
            """,
            (fat,),
        )
        before = conn.execute("SELECT length(raw_json) FROM orders WHERE alpaca_order_id='ord-fat'").fetchone()[0]

    loaded = db.get_latest_open_orders()
    assert any(row.get("alpaca_order_id") == "ord-fat" for row in loaded)
    dumped = json.dumps(loaded)
    assert "STATE_BLOB" not in dumped
    assert all("raw_json" not in row for row in loaded)

    snapshot = {
        "account": {
            "cash": 82000.0, "equity": 100000.0, "buying_power": 82000.0,
            "portfolio_value": 100000.0, "long_market_value": 18000.0,
            "short_market_value": 0.0, "multiplier": 1.0,
        },
        "positions": [],
        "open_orders": [],
        "recent_fills": [],
    }
    state = build_dashboard_state(cached_funnel={}, live_snapshot=snapshot)
    assert "STATE_BLOB" not in json.dumps(state.get("open_orders") or [])
    with db.get_connection() as conn:
        after = conn.execute("SELECT length(raw_json) FROM orders WHERE alpaca_order_id='ord-fat'").fetchone()[0]
    assert after == before


def test_prune_orders_raw_json_shrinks_fat_row_without_deleting_it():
    from agenttrade import db

    db.init_db()
    blob = "STATE_BLOB " + ("nested state " * 5000)
    fat = json.dumps({
        "id": "ord-xom",
        "symbol": "XOM",
        "side": "buy",
        "qty": "4",
        "status": "accepted",
        "type": "market",
        "stop_price": "100.00",
        "analysis_path": "deterministic_signal",
        "raw_json": json.dumps({"raw_json": blob, "ohlcv": blob, "screener_sources": blob}),
        "ohlcv": {"closes": [blob]},
        "screener_sources": {"Dividend": blob},
    })
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO orders(
                alpaca_order_id, symbol, side, qty, status, raw_json
            ) VALUES ('ord-xom', 'XOM', 'buy', 4, 'accepted', ?)
            """,
            (fat,),
        )
        before_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        before_len = conn.execute("SELECT length(raw_json) FROM orders WHERE alpaca_order_id='ord-xom'").fetchone()[0]

    stats = db.prune_orders_raw_json()
    assert stats["rows"] >= 1
    assert stats["bytes_after"] < stats["bytes_before"]

    with db.get_connection() as conn:
        after_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        row = conn.execute(
            "SELECT symbol, qty, raw_json, length(raw_json) AS n FROM orders WHERE alpaca_order_id='ord-xom'"
        ).fetchone()
    assert after_count == before_count
    assert row["symbol"] == "XOM"
    assert row["qty"] == 4
    assert row["n"] < before_len
    assert row["n"] <= MAX_ORDER_RAW_BYTES
    parsed = json.loads(row["raw_json"])
    assert parsed["symbol"] == "XOM"
    assert parsed["analysis_path"] == "deterministic_signal"
    assert "STATE_BLOB" not in row["raw_json"]


def test_reclaim_running_cycle_keeps_the_row():
    from agenttrade import db

    db.init_db()
    stuck = db.start_cycle_run("paper")
    done = db.start_cycle_run("paper")
    db.finish_cycle_run(done, "completed", "4 orders")

    reclaimed = db.reclaim_running_cycles(
        stuck,
        notes="OOM exit 137 during cycle; reclaimed without deleting history",
    )
    assert [row["id"] for row in reclaimed] == [stuck]

    with db.get_connection() as conn:
        stuck_row = conn.execute("SELECT status, finished_at, notes FROM cycle_runs WHERE id=?", (stuck,)).fetchone()
        done_row = conn.execute("SELECT status, notes FROM cycle_runs WHERE id=?", (done,)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM cycle_runs").fetchone()[0]
    assert stuck_row["status"] == "interrupted"
    assert stuck_row["finished_at"]
    assert "OOM" in stuck_row["notes"]
    assert done_row["status"] == "completed"
    assert count == 2
    assert db.reclaim_running_cycles(stuck) == []
