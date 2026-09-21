import os


def test_funnel_and_artifacts_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTRADE_DB_PATH", str(tmp_path / "ledger.sqlite3"))
    from agenttrade import db

    db.init_db()
    cycle_id = db.start_cycle_run("tiered")
    db.insert_funnel_event(cycle_id, "UNIVERSE", symbol="AAPL", bucket="Growth", status="SCREENED", payload={"ticker":"AAPL"})
    db.insert_funnel_events(cycle_id, "CANDIDATE", [{"ticker":"AAPL", "bucket":"Growth", "confidence":0.8}])
    db.insert_funnel_event(cycle_id, "RISK", symbol="AAPL", bucket="Growth", status="BLOCKED", reason="risk_rejected", payload={"ticker":"AAPL", "risk_status":"BLOCKED"})
    db.upsert_cycle_artifact(cycle_id, "universes", {"Growth":["AAPL"]})

    latest = db.get_latest_funnel()
    assert latest["cycle_run_id"] == cycle_id
    assert latest["funnel"]["candidates"][0]["ticker"] == "AAPL"
    assert latest["funnel"]["risk"][0]["blocked_reason"] == "risk_rejected"
    assert latest["artifacts"]["universes"] == {"Growth":["AAPL"]}


def test_manual_stops_use_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTRADE_DB_PATH", str(tmp_path / "ledger.sqlite3"))
    from agenttrade import db

    db.init_db()
    db.set_manual_stop_price("aapl", 123.45678)
    assert db.get_manual_stop_prices() == {"AAPL": 123.4568}
