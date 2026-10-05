"""Production overcount: activity ids, take-profit legs, and shared-account orders.

On 5 Oct 2026 the banner showed 13, and 19 when it fell back onto stored
open orders. AgentTrade had placed 7 entries: META, NET, SO, SOL/USD,
NVDA, SMCI, MO. NET filled in two pieces (10 + 4) and SOL filled in two
pieces. Ledger rows kept the Alpaca activity id; state.recent_fills kept
the order UUID. Six bracket take-profit limit sells were still stored as
status=new. state.open_orders held 100 CRM sells from 28 Aug.
"""

import json
from datetime import datetime, timezone

CYCLE_NOW = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)
DAY = "2026-10-05T15:00:00Z"

# Real broker order ids. Ledger fills below do not store these.
ORDERS = {
    "META": "aaaaaaaa-1111-4111-8111-111111111111",
    "NET": "bbbbbbbb-2222-4222-8222-222222222222",
    "SO": "cccccccc-3333-4333-8333-333333333333",
    "SOL/USD": "dddddddd-4444-4444-8444-444444444444",
    "NVDA": "eeeeeeee-5555-4555-8555-555555555555",
    "SMCI": "ffffffff-6666-4666-8666-666666666666",
    "MO": "99999999-7777-4777-8777-777777777777",
}
PEPE = "pepepepe-8888-4888-8888-888888888888"
HYPE = "hypehype-9999-4999-8999-999999999999"

# Activity ids stored in fills.alpaca_order_id on the host.
ACTIVITIES = {
    "META": [("20261005150000001::meta", 5)],
    "NET": [("20261005150100001::net-a", 10), ("20261005150100002::net-b", 4)],
    "SO": [("20261005150200001::so", 8)],
    "SOL/USD": [("20261005150300001::sol-a", 1.5), ("20261005150300002::sol-b", 0.5)],
}
TP_SYMBOLS = ("META", "NET", "SO", "NVDA", "SMCI", "MO")


def _ledger_fill(activity_id: str, symbol: str, qty, ts: str = DAY) -> None:
    """A fill row keyed by the activity id, with no broker order id in raw_json."""
    from agenttrade import db

    raw = {"id": activity_id, "symbol": symbol, "side": "buy", "qty": qty}
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fills(
                alpaca_order_id, alpaca_fill_id, filled_at, symbol, side, qty, price, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (activity_id, activity_id, ts, symbol, "buy", qty, 10, json.dumps(raw)),
        )


def _recent(activity_id: str, order_id: str, symbol: str, qty, ts: str = DAY) -> dict:
    """Shape returned by alpaca_client.get_recent_fills."""
    return {
        "id": activity_id,
        "order_id": order_id,
        "ticker": symbol,
        "symbol": symbol,
        "side": "buy",
        "shares": qty,
        "qty": qty,
        "price": 10,
        "status": "filled",
        "submitted_at": ts,
        "filled_at": ts,
        "source": "alpaca_fill",
    }


def _own(order_id: str, symbol: str, ts: str = DAY) -> None:
    from agenttrade import db

    db.record_submitted_order(None, {
        "order_id": order_id,
        "symbol": symbol,
        "side": "buy",
        "status": "filled",
        "submitted_at": ts,
        "client_order_id": f"agenttrade-{order_id[:8]}",
    }, strategy_name="Crypto" if "/" in symbol else "Growth")


def _tp_leg(symbol: str) -> dict:
    """Stored bracket take-profit. status=new, no class, no parent. Alpaca: expired."""
    return {
        "id": f"tp-{symbol}",
        "symbol": symbol,
        "side": "sell",
        "type": "limit",
        "order_type": "limit",
        "order_class": "",
        "status": "new",
        "submitted_at": "2026-10-05T15:05:00Z",
        "limit_price": 99,
    }


def _crm_ghost(index: int) -> dict:
    return {
        "id": f"crm-{index}",
        "symbol": "CRM",
        "side": "sell",
        "type": "market",
        "status": "new",
        "submitted_at": "2026-08-28T15:00:00Z",
    }


def _seed_production_shape():
    from agenttrade import db

    db.init_db()
    recent = []
    for symbol, pieces in ACTIVITIES.items():
        _own(ORDERS[symbol], symbol)
        for activity_id, qty in pieces:
            _ledger_fill(activity_id, symbol, qty)
            recent.append(_recent(activity_id, ORDERS[symbol], symbol, qty))
    for symbol in ("NVDA", "SMCI", "MO"):
        _own(ORDERS[symbol], symbol)
        activity_id = f"20261005150400000::{symbol.lower()}"
        # These three were already stored under the real order id, so they
        # did not add a second key. Keep one ledger row and one recent fill.
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO fills(
                    alpaca_order_id, alpaca_fill_id, filled_at, symbol, side, qty, price, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ORDERS[symbol],
                    activity_id,
                    DAY,
                    symbol,
                    "buy",
                    3,
                    20,
                    json.dumps({
                        "id": activity_id,
                        "order_id": ORDERS[symbol],
                        "symbol": symbol,
                        "side": "buy",
                        "qty": 3,
                    }),
                ),
            )
        recent.append(_recent(activity_id, ORDERS[symbol], symbol, 3))
    for symbol in TP_SYMBOLS:
        db.insert_orders(None, [_tp_leg(symbol)])
    recent.append(_recent("20261005150500001::pepe", PEPE, "PEPE/USD", 1000))
    recent.append(_recent("20261005150500002::hype", HYPE, "HYPE/USD", 20))
    ghosts = [_crm_ghost(i) for i in range(100)]
    return recent, ghosts


def test_activity_ids_and_uuids_collapse_without_counting_exits():
    from trading_day import explain_trades_for_day

    recent, ghosts = _seed_production_shape()
    from agenttrade import db

    ledger_fills = db.fills_in_trading_day(CYCLE_NOW)
    fills = ledger_fills + recent
    orders = ghosts + [_tp_leg(symbol) for symbol in TP_SYMBOLS]
    # Without an ownership filter the two CryptoAgent buys are still entries.
    unfiltered = explain_trades_for_day(fills, orders, now=CYCLE_NOW)
    assert unfiltered["count"] == 9
    owned = explain_trades_for_day(
        fills, orders, now=CYCLE_NOW, owned_order_ids=db.agent_submitted_order_ids(),
    )
    assert owned["count"] == 7
    assert {row["symbol"] for row in owned["counted"]} == set(ORDERS)
    assert all(row["side"] == "buy" for row in owned["counted"])

    net = next(row for row in owned["counted"] if row["symbol"] == "NET")
    assert {row["activity_id"] for row in net["merges"]} == {
        "20261005150100001::net-a",
        "20261005150100002::net-b",
    }
    assert {float(row["qty"]) for row in net["merges"]} == {10.0, 4.0}
    assert all(row["order_id"] == ORDERS["NET"] for row in net["merges"])

    sol = next(row for row in owned["counted"] if row["symbol"] == "SOL/USD")
    assert len(sol["merges"]) == 2
    assert {row["activity_id"] for row in sol["merges"]} == {
        "20261005150300001::sol-a",
        "20261005150300002::sol-b",
    }

    for symbol in ("META", "SO"):
        row = next(item for item in owned["counted"] if item["symbol"] == symbol)
        assert len(row["merges"]) == 1
        assert row["merges"][0]["order_id"] == ORDERS[symbol]
        assert row["order_id"] == ORDERS[symbol]

    tp = [row for row in owned["excluded"] if str(row["order_id"]).startswith("tp-")]
    assert {row["symbol"] for row in tp} == set(TP_SYMBOLS)
    assert all(row["reason"] == "exit / protective take-profit leg" for row in tp)
    assert all(row["status"] == "new" for row in tp)

    crm = [row for row in owned["excluded"] if row["symbol"] == "CRM"]
    assert len(crm) == 100
    assert all(row["reason"] == "outside America/Chicago day" for row in crm)

    foreign = {row["symbol"] for row in owned["excluded"] if row["reason"] == "not an AgentTrade order"}
    assert foreign == {"PEPE/USD", "HYPE/USD"}


def test_banner_counts_seven_with_ghosts_and_without_them(monkeypatch, tmp_path):
    import agent_config as cfg
    import config_server
    from health_check import _daily_trades_check

    recent, ghosts = _seed_production_shape()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"MAX_DAILY_TRADES": "10"}))
    monkeypatch.setattr(config_server, "CONFIG_FILE", str(path))
    monkeypatch.setenv("MAX_DAILY_TRADES", "5")

    with_ghosts = {
        "daily_trades": 9,
        "recent_fills": recent,
        "open_orders": ghosts,
    }
    row = _daily_trades_check(with_ghosts, now=CYCLE_NOW)
    assert row["daily"] == 7
    assert row["limit"] == 10
    assert row["status"] == "ok"
    assert row["message"] == "7/10 used"
    assert cfg.current_max_daily_trades() == 10

    # Empty open orders used to fall through to every stored "new" order
    # and add the six take-profit legs (the path that showed 19).
    without_ghosts = {"daily_trades": 19, "recent_fills": recent, "open_orders": []}
    again = _daily_trades_check(without_ghosts, now=CYCLE_NOW)
    assert again["daily"] == 7
    assert again["message"] == "7/10 used"

    from account_sync import _count_trades_today

    assert _count_trades_today(recent, ghosts, now=CYCLE_NOW) == 7


def test_reimport_persists_broker_order_id_over_activity_id():
    from agenttrade import db

    recent, _ghosts = _seed_production_shape()
    with db.get_connection() as conn:
        before = conn.execute(
            "SELECT alpaca_order_id FROM fills WHERE alpaca_fill_id=?",
            ("20261005150100001::net-a",),
        ).fetchone()[0]
    assert before == "20261005150100001::net-a"

    db.insert_fills_from_alpaca(0, recent)
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT alpaca_fill_id, alpaca_order_id FROM fills
            WHERE symbol='NET' ORDER BY alpaca_fill_id
            """
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("20261005150100001::net-a", ORDERS["NET"]),
        ("20261005150100002::net-b", ORDERS["NET"]),
    ]
    assert db.count_fills_today(now=CYCLE_NOW) == 7


def test_live_expired_status_overrides_a_stale_open_buy():
    from trading_day import explain_trades_for_day

    orders = [
        {
            "id": "buy-1",
            "symbol": "GAIN",
            "side": "buy",
            "type": "market",
            "status": "new",
            "submitted_at": DAY,
            "_count_origin": "ledger",
            "client_order_id": "agenttrade-buy-1",
        },
        {
            "id": "buy-1",
            "symbol": "GAIN",
            "side": "buy",
            "type": "market",
            "status": "expired",
            "submitted_at": DAY,
            "_count_origin": "live",
            "client_order_id": "agenttrade-buy-1",
        },
    ]
    report = explain_trades_for_day([], orders, now=CYCLE_NOW, owned_order_ids=set())
    assert report["count"] == 0
    assert report["excluded"][0]["reason"] == "status expired"


def test_explain_cli_shows_merges_and_take_profit_legs(capsys, tmp_path, monkeypatch):
    import agent_config as cfg
    from agenttrade.daily_trades import main

    recent, ghosts = _seed_production_shape()
    state_path = tmp_path / "agent_state.json"
    state_path.write_text(json.dumps({
        "recent_fills": recent,
        "open_orders": ghosts,
        "daily_trades": 13,
    }))
    monkeypatch.setattr(cfg, "STATE_FILE", str(state_path))

    assert main(["--explain", "--now", "2026-10-05T15:30:00+00:00"]) == 0
    text = capsys.readouterr().out
    assert "7 counted" in text
    assert "merge activity 20261005150100001::net-a -> " + ORDERS["NET"] in text
    assert "qty 10" in text
    assert "qty 4" in text
    assert "merge activity 20261005150300001::sol-a -> " + ORDERS["SOL/USD"] in text
    assert "META sell tp-META type=limit status=new reason: exit / protective take-profit leg" in text
    assert "reason: outside America/Chicago day" in text
    assert "reason: not an AgentTrade order" in text
    assert "PEPE/USD" in text
