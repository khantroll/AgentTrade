"""Dashboard projection stays under the size cap without dropping cycle facts."""

import json

from agenttrade.state_size import compact_dashboard_state, projection_utf8_size


def _bloated_cycle_state() -> dict:
    mention = "MENTION_BLOB " + ("reddit post body " * 80)
    mentions = [mention] * 40
    attribution = {
        "NVDA": {
            "signal_strength": 42,
            "signal_mix": {"news_sentiment": 60, "momentum": 40},
            "total_score": 42,
            "reddit_detail": {
                "avg_sentiment": 0.4,
                "quality_score": 1.2,
                "mention_count": len(mentions),
                "mentions": mentions,
            },
            "ohlcv": {"closes": list(range(5000))},
        }
    }
    decision = {
        "ticker": "NVDA",
        "action": "BUY",
        "bucket": "Growth",
        "analysis_path": "deterministic_signal",
        "analysis_reason": "analysis_failed",
        "rationale": "Analysis LLM failed; screener signal_strength 42 supports BUY",
        "signal_strength": 42,
        "current_price": 178.23,
        "ohlcv": {"closes": list(range(8000)), "highs": list(range(8000))},
        "signal_attribution": attribution["NVDA"],
        "prompt": "X" * 5000,
    }
    return {
        "last_run": "2026-10-02T15:00:00",
        "cash": 82000.0,
        "equity": 100000.0,
        "buying_power": 82000.0,
        "portfolio_value": 100000.0,
        "positions": [{"symbol": "LINK/USD", "qty": 100, "market_value": 2600}],
        "recent_fills": [{"ticker": "LINK/USD", "side": "buy", "shares": "100", "price": "26"}],
        "last_orders": [{
            "ticker": "NVDA",
            "status": "placed",
            "order_id": "ord-nvda",
            "side": "buy",
            "analysis_path": "deterministic_signal",
        }],
        "decisions": [decision, {**decision, "ticker": "MSFT", "action": "BUY"}],
        "trade_candidates": [{"ticker": "NVDA", "signal_strength": 42, "ohlcv": decision["ohlcv"]}],
        "blocked_ideas": [],
        "research_status": {"status": "failed", "entry_source": "screener", "reason": "invalid_parse"},
        "screener_sources": {
            "Growth": {
                "momentum": 12,
                "movers": 4,
                "reddit": 6,
                "news": 3,
                "congress": 1,
                "cached": False,
                "attribution": attribution,
                "reddit_intelligence": {"NVDA": {"mentions": mentions}},
                "pipeline_membership": {f"T{i}": {"Momentum": {"rank": i}} for i in range(200)},
            }
        },
    }


def test_compact_drops_mention_dumps_and_keeps_cycle_facts():
    raw = _bloated_cycle_state()
    compact = compact_dashboard_state(raw)
    encoded = json.dumps(compact)
    assert "MENTION_BLOB" not in encoded
    assert "ohlcv" not in encoded
    assert "reddit_intelligence" not in encoded
    assert "pipeline_membership" not in encoded
    decision = compact["decisions"][0]
    assert decision["ticker"] == "NVDA"
    assert decision["action"] == "BUY"
    assert decision["analysis_path"] == "deterministic_signal"
    assert decision["signal_strength"] == 42
    assert compact["positions"][0]["symbol"] == "LINK/USD"
    assert compact["recent_fills"][0]["ticker"] == "LINK/USD"
    assert compact["last_orders"][0]["order_id"] == "ord-nvda"
    assert compact["screener_sources"]["Growth"]["momentum"] == 12
    assert compact["screener_sources"]["Growth"]["attribution"]["NVDA"]["signal_strength"] == 42
    assert compact["research_status"]["entry_source"] == "screener"
    assert projection_utf8_size(json.dumps(raw)) > projection_utf8_size(encoded)


def test_publish_prunes_over_cap_and_replaces_oversized_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_STATE_MAX_BYTES", "12000")
    import agent_config as cfg
    from account_sync import load_state_file
    from agenttrade.publish import publish_dashboard_state

    state_path = tmp_path / "agent_state.json"
    public = tmp_path / "public" / "agent_state.json"
    public.parent.mkdir(parents=True, exist_ok=True)
    bloated_disk = json.dumps({"decisions": [{"ticker": "OLD", "action": "SKIP"}], "blob": "Z" * 20000})
    state_path.write_text(bloated_disk)
    public.write_text(bloated_disk)
    assert state_path.stat().st_size > 12000
    assert load_state_file() == {}

    state = _bloated_cycle_state()
    unpruned = projection_utf8_size(json.dumps(state))
    assert unpruned > 12000

    publish_dashboard_state(state)
    written = state_path.read_text()
    assert len(written.encode("utf-8")) <= 12000
    saved = json.loads(written)
    assert saved["decisions"][0]["action"] == "BUY"
    assert saved["decisions"][0]["analysis_path"] == "deterministic_signal"
    assert saved["positions"][0]["symbol"] == "LINK/USD"
    assert saved["recent_fills"][0]["ticker"] == "LINK/USD"
    assert saved["screener_sources"]["Growth"]["momentum"] == 12
    assert json.loads(public.read_text())["decisions"][0]["ticker"] == "NVDA"
    assert cfg.STATE_FILE == str(state_path)


def test_persisted_funnel_and_artifacts_drop_mention_dumps(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTRADE_DB_PATH", str(tmp_path / "ledger.sqlite3"))
    from agenttrade import db

    db.init_db()
    cycle_id = db.start_cycle_run("tiered")
    blob = "MENTION_BLOB " + ("x" * 2000)
    db.insert_funnel_event(
        cycle_id, "DECISION", symbol="NVDA", bucket="Growth", status="BUY",
        payload={
            "ticker": "NVDA",
            "action": "BUY",
            "analysis_path": "deterministic_signal",
            "ohlcv": {"closes": [1, 2, 3]},
            "reddit_detail": {"mention_count": 3, "mentions": [blob, blob]},
        },
    )
    db.upsert_cycle_artifact(cycle_id, "screener_sources", {
        "Growth": {
            "momentum": 9,
            "reddit_intelligence": {"NVDA": {"mentions": [blob]}},
            "attribution": {"NVDA": {"signal_strength": 20, "mentions": [blob]}},
        }
    })
    latest = db.get_latest_funnel()
    decision = latest["funnel"]["decisions"][0]
    assert decision["analysis_path"] == "deterministic_signal"
    assert decision["action"] == "BUY"
    assert "ohlcv" not in decision
    assert "mentions" not in (decision.get("reddit_detail") or {})
    sources = latest["artifacts"]["screener_sources"]["Growth"]
    assert sources["momentum"] == 9
    assert "reddit_intelligence" not in sources
    assert "MENTION_BLOB" not in json.dumps(latest)
