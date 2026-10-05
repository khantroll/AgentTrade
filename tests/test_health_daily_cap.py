"""Health banner uses the configured daily cap and the Chicago order count."""

import json
import os
from datetime import datetime, timezone

# 10:30 CT on 5 Oct 2026 is 15:30 UTC. Chicago is still CDT (UTC-5).
CYCLE_NOW = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)


def _fill(fill_id: str, order_id: str, symbol: str, ts: str) -> dict:
    return {
        "id": fill_id,
        "order_id": order_id,
        "symbol": symbol,
        "side": "buy",
        "qty": 1,
        "price": 10,
        "transaction_time": ts,
    }


def _banner(monkeypatch, tmp_path, cap: str, env_cap: str, state: dict) -> dict:
    """Point config.json at ``cap`` while the environment says ``env_cap``."""
    import agent_config as cfg
    import config_server
    from health_check import _daily_trades_check

    saved = dict(os.environ)
    monkeypatch.setattr(cfg, "MAX_DAILY_TRADES", cfg.MAX_DAILY_TRADES)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"MAX_DAILY_TRADES": cap}))
    monkeypatch.setattr(config_server, "CONFIG_FILE", str(path))
    monkeypatch.setenv("MAX_DAILY_TRADES", env_cap)
    try:
        row = _daily_trades_check(state, now=CYCLE_NOW)
        assert row["limit"] == int(cap)
        assert cfg.current_max_daily_trades() == int(cap)
        return row
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_health_banner_reads_config_cap_of_10_not_stale_counter(monkeypatch, tmp_path):
    from agenttrade import db

    db.init_db()
    db.insert_fills_from_alpaca(0, [
        _fill("act-1", "ord-1", "AAPL", "2026-10-05T15:00:00Z"),
        _fill("act-2", "ord-1", "AAPL", "2026-10-05T15:01:00Z"),
    ])
    state = {
        "daily_trades": 9,
        "open_orders": [
            {
                "id": "ord-new",
                "status": "accepted",
                "side": "buy",
                "submitted_at": "2026-10-05T15:20:00Z",
            },
            {
                "id": "ord-stop",
                "status": "new",
                "side": "sell",
                "type": "stop",
                "order_class": "bracket",
                "submitted_at": "2026-10-05T15:00:00Z",
            },
            {
                "id": 4,
                "alpaca_order_id": "ord-ledger",
                "status": "accepted",
                "side": "buy",
                "order_type": "market",
                "submitted_at": "2026-10-05T15:25:00Z",
            },
        ],
    }
    row = _banner(monkeypatch, tmp_path, "10", "5", state)
    # Partials of ord-1 count once. The stop does not. The stale 9 does not.
    assert row["daily"] == 3
    assert row["limit"] == 10
    assert row["status"] == "ok"
    assert row["message"] == "3/10 used"


def test_health_banner_reads_config_cap_of_5_under_and_at_limit(monkeypatch, tmp_path):
    from agenttrade import db
    from health_check import _daily_trades_check

    db.init_db()
    db.insert_fills_from_alpaca(0, [
        _fill(f"act-{i}", f"ord-{i}", "MSFT", f"2026-10-05T15:{i:02d}:00Z")
        for i in range(4)
    ])
    under = _banner(monkeypatch, tmp_path, "5", "10", {"daily_trades": 0, "open_orders": []})
    assert under["daily"] == 4
    assert under["limit"] == 5
    assert under["status"] == "ok"
    assert under["message"] == "4/5 used"

    db.insert_fills_from_alpaca(0, [
        _fill("act-4", "ord-4", "NVDA", "2026-10-05T15:10:00Z"),
    ])
    # Same config.json cap of 5. The stale projection says 1 and must not win.
    at_cap = _daily_trades_check({"daily_trades": 1, "open_orders": []}, now=CYCLE_NOW)
    assert at_cap["daily"] == 5
    assert at_cap["limit"] == 5
    assert at_cap["status"] == "warn"
    assert at_cap["message"] == "5/5 used · no new orders today"


def test_dashboard_status_strip_does_not_hardcode_cap():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "dashboard.html"), encoding="utf-8").read()
    assert "max 5 / day" not in html
    assert "Daily trades / 5" not in html
    assert "'/5 used'" not in html
    assert "max_daily_trades" in html
    assert "dailyTradeBanner" in html
    assert "healthTradeLimit" in html
