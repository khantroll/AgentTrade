"""Analysis LLM failures fail closed while valid model votes remain authoritative.

Research-stage screener fallback is a separate degraded entry path. Once an
analysis LLM is invoked, provider exhaustion, invalid JSON, or otherwise
unusable output must become SKIP regardless of signal strength. Protective
sells remain independent of analysis availability.
"""

import json

import pytest

import agent_config as cfg
from agents.screener_fallback import analysis_result_failed
from buckets import Bucket


@pytest.fixture
def isolated_llm(monkeypatch, tmp_path):
    import llm_router

    monkeypatch.setattr(llm_router, "LLM_HEALTH_FILE", str(tmp_path / "llm_health.json"))
    monkeypatch.setattr(llm_router, "TOKEN_USAGE_FILE", str(tmp_path / "token_usage.json"))
    llm_router._MODEL_SUPPRESS_UNTIL.clear()
    llm_router._PROVIDER_COOLDOWN_UNTIL.clear()
    yield llm_router
    llm_router._MODEL_SUPPRESS_UNTIL.clear()
    llm_router._PROVIDER_COOLDOWN_UNTIL.clear()


def _growth() -> Bucket:
    return Bucket(name="Growth", allocation_pct=0.45, mode="growth", max_positions=8)


def _quote(ticker):
    return {
        "symbol": ticker,
        "current_price": 100.0,
        "atr14": 4.0,
        "atr_pct": 4.0,
    }


def _paper_snapshot(cash=85000.0, equity=107000.0, long_mv=22000.0, high_water=107000.0, positions=None):
    account = {
        "cash": cash,
        "equity": equity,
        "buying_power": cash,
        "portfolio_value": equity,
        "long_market_value": long_mv,
    }
    return {
        "account": account,
        "positions": list(positions or []),
        "open_buy_notional": 0,
        "cash": cash,
        "equity": equity,
        "portfolio_value": equity,
        "high_water_equity": high_water,
    }


def _held_positions():
    return [
        {"symbol": "GAIN", "qty": 10, "market_value": 12000, "unrealized_pl": 400, "current_price": 12, "avg_entry_price": 10},
        {"symbol": "MA", "qty": 5, "market_value": 10000, "unrealized_pl": 200, "current_price": 400, "avg_entry_price": 380},
    ]


def _use_live_risk_defaults(monkeypatch):
    """Pin Milestone C defaults so the test does not depend on a mutated process."""
    monkeypatch.setattr(cfg, "RESERVE_CASH_PCT", 0.10)
    monkeypatch.setattr(cfg, "MIN_CASH_RESERVE", 5000.0)
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False)
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr(cfg, "SELLING_ENABLED", True)
    monkeypatch.setattr(cfg, "MAX_INVESTED_PCT", 0.90)
    monkeypatch.setattr(cfg, "MAX_ACCOUNT_DRAWDOWN_PCT", 0.10)
    monkeypatch.setattr(cfg, "ALLOW_POSITION_ADDS", False)
    monkeypatch.setattr(cfg, "ALLOW_AVERAGE_DOWN", False)
    monkeypatch.setattr(cfg, "MAX_ANALYSIS_CANDIDATES", 3)
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {"GAIN": "Growth", "MA": "Dividend"})


def _candidates():
    return [
        {
            "ticker": "NVDA",
            "confidence": 0.90,
            "reason": "screener news and congress",
            "source": "research",
            "entry_source": "research",
            "screener_rank": 1,
            "signal_strength": 60.0,
            "total_score": 60.0,
            "signal_mix": {"news_sentiment": 50, "congress": 50},
        },
        {
            "ticker": "SO",
            "confidence": 0.80,
            "reason": "lone momentum",
            "source": "research",
            "entry_source": "research",
            "screener_rank": 4,
            "total_score": 10.0,
            "signal_mix": {"momentum": 100},
        },
        {
            "ticker": "GBDC",
            "confidence": 0.70,
            "reason": "no ensemble score",
            "source": "research",
            "entry_source": "research",
        },
    ]


def _attribution():
    return {
        "NVDA": {"signal_strength": 60.0, "signal_mix": {"news_sentiment": 50, "congress": 50}},
        "SO": {"signal_strength": 10.0, "signal_mix": {"momentum": 100}, "total_score": 10.0},
    }


def test_salvage_truncated_analysis_decision_without_inventing_research_picks():
    from llm_router import _parse_json

    text = (
        '{"decision": "BUY", "confidence": 0.81, "rationale": "congress and news'
        ', "ticker": "NVDA"}'
    )
    parsed = _parse_json(text)
    assert parsed["action"] == "BUY"
    assert parsed["decision"] == "BUY"
    assert parsed["confidence"] == 0.81
    assert parsed["json_repaired"] is True
    assert "selected" not in parsed
    assert "congress and news" in parsed["rationale"]


def test_unusable_analysis_does_not_count_and_next_model_votes(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    marks = []
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: marks.append(provider))
    monkeypatch.setattr(llm_router, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("gemini_flash", "gemini", "prose-model"),
        ("groq_llama", "groq", "ok-model"),
        ("mistral_small", "mistral", "unused-model"),
    ])
    calls = []
    bodies = {
        "prose-model": "Here is the JSON requested",
        "ok-model": json.dumps({"decision": "BUY", "confidence": 0.77, "rationale": "news hit"}),
    }

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        assert mark_success is False
        assert "analysis" in agent_tag
        calls.append((model, prompt))
        return bodies[model]

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router._tiered_analysis("decide", agent_tag="Growth_analysis")
    assert result["action"] == "BUY"
    assert result["tiered_status"] == "buy_votes"
    assert result["analysis_status"] == "ok"
    assert result["tiered_models_used"] == ["groq_llama"]
    assert "shares" not in result
    assert marks == ["groq"]
    assert [model for model, _prompt in calls] == ["prose-model", "prose-model", "ok-model"]
    assert llm_router._STRICT_ANALYSIS_JSON_INSTRUCTION in calls[1][1]
    assert llm_router._STRICT_ANALYSIS_JSON_INSTRUCTION not in calls[0][1]


def test_all_invalid_analysis_json_is_a_failure_not_a_skip_vote(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    marks = []
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: marks.append(provider))
    monkeypatch.setattr(llm_router, "_tiered_fanout", lambda phase: 3)
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("gemini_flash", "gemini", "bad-json"),
        ("groq_llama", "groq", "empty-object"),
        ("mistral_small", "mistral", "prose"),
    ])
    bodies = {
        "bad-json": '{"decision": ',
        "empty-object": "{}",
        "prose": "I think we should buy NVDA.",
    }

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        return bodies[model]

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router._tiered_analysis("decide", agent_tag="Growth_analysis")
    assert result["tiered_status"] == "error"
    assert result["analysis_status"] == "failed"
    assert result["analysis_reason"] == "invalid_parse"
    assert result["rationale"] == "All tiered analysis models failed or returned invalid JSON"
    assert marks == []
    assert analysis_result_failed(result) is True


def test_provider_errors_are_analysis_failure(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    monkeypatch.setattr(llm_router, "_tiered_fanout", lambda phase: 3)
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("gemini_flash", "gemini", "down"),
        ("groq_llama", "groq", "down-too"),
    ])

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        raise RuntimeError(f"{provider} HTTP 500: boom")

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router._tiered_analysis("decide", agent_tag="Growth_analysis")
    assert result["tiered_status"] == "error"
    assert result["analysis_status"] == "failed"
    assert result["analysis_reason"] == "analysis_failed"
    assert analysis_result_failed(result) is True


def test_valid_skip_vote_is_not_treated_as_analysis_failure(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    monkeypatch.setattr(llm_router, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("gemini_flash", "gemini", "skip-model"),
    ])
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: None)

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        return json.dumps({"decision": "SKIP", "confidence": 0.4, "rationale": "RSI extended"})

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router._tiered_analysis("decide", agent_tag="Growth_analysis")
    assert result["action"] == "SKIP"
    assert result["analysis_status"] == "ok"
    assert result["tiered_status"] == "no_buy_consensus"
    assert analysis_result_failed(result) is False


def test_analysis_failure_fails_closed_regardless_of_signal_strength(monkeypatch):
    """Research names exist, but every analysis model returns invalid JSON."""
    from agenttrade import db
    from agents.analysis import analysis_agent
    from agents.risk import risk_agent

    db.init_db()
    _use_live_risk_defaults(monkeypatch)
    monkeypatch.setattr("agents.analysis.audit_log", lambda *args, **kwargs: None)
    monkeypatch.setattr("agents.analysis.fetch_stock_data", _quote)
    monkeypatch.setattr("agents.analysis.fetch_crypto_data", _quote)

    import llm_router

    monkeypatch.setattr(llm_router, "_effective_mode", lambda: "tiered")
    monkeypatch.setattr(llm_router, "_tiered_fanout", lambda phase: 3)
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("gemini_flash", "gemini", "bad"),
        ("groq_llama", "groq", "also-bad"),
        ("mistral_small", "mistral", "still-bad"),
    ])
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: None)
    monkeypatch.setattr(
        llm_router,
        "_call_provider",
        lambda provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True: "not json at all",
    )

    candidates = _candidates()
    candidates[0]["signal_strength"] = 70.0
    candidates[0]["total_score"] = 70.0
    candidates[1]["signal_strength"] = 39.1
    candidates[1]["total_score"] = 39.1
    attribution = {
        "NVDA": {"signal_strength": 70.0, "signal_mix": {"news_sentiment": 50, "congress": 50}},
        "SO": {"signal_strength": 39.1, "signal_mix": {"momentum": 100}},
    }

    decisions = analysis_agent(
        candidates,
        {"cash": 85000, "portfolio_value": 107000},
        _held_positions(),
        _growth(),
        attribution,
    )
    by_ticker = {d["ticker"]: d for d in decisions}
    assert by_ticker["NVDA"]["action"] == "SKIP"
    assert by_ticker["NVDA"]["analysis_path"] == "fail_closed"
    assert by_ticker["NVDA"]["analysis_status"] == "failed"
    assert by_ticker["NVDA"]["analysis_reason"] == "invalid_parse"
    assert by_ticker["NVDA"]["skip_reason"] == "analysis_failed"
    assert by_ticker["NVDA"]["signal_strength"] == 70.0
    assert by_ticker["SO"]["action"] == "SKIP"
    assert by_ticker["SO"]["signal_strength"] == 39.1
    assert by_ticker["SO"]["skip_reason"] == "analysis_failed"
    assert by_ticker["GBDC"]["action"] == "SKIP"
    assert by_ticker["GBDC"]["skip_reason"] == "analysis_failed"
    assert all(d["action"] != "SELL" for d in decisions)

    snap = _paper_snapshot(positions=_held_positions())
    approved = risk_agent(
        decisions,
        snap["account"],
        _held_positions(),
        _growth(),
        None,
        account_snapshot=snap,
    )
    assert approved == []


def test_valid_model_skip_is_not_overridden_by_strong_screener_evidence(monkeypatch):
    from agents.analysis import analysis_agent

    monkeypatch.setattr("agents.analysis.audit_log", lambda *args, **kwargs: None)
    monkeypatch.setattr("agents.analysis.fetch_stock_data", _quote)
    monkeypatch.setattr(cfg, "MAX_ANALYSIS_CANDIDATES", 2)

    def query(prompt, agent_tag="analysis"):
        return {
            "action": "SKIP",
            "decision": "SKIP",
            "confidence": 0.42,
            "rationale": "RSI extended",
            "analysis_status": "ok",
            "tiered_status": "no_buy_consensus",
        }

    monkeypatch.setattr("agents.analysis.query_analysis", query)
    decisions = analysis_agent(
        [_candidates()[0]],
        {"cash": 85000, "portfolio_value": 107000},
        [],
        _growth(),
        _attribution(),
    )
    assert decisions[0]["action"] == "SKIP"
    assert decisions[0].get("analysis_path") != "deterministic_signal"
    assert "RSI extended" in decisions[0]["rationale"] or "BUY threshold" in decisions[0]["rationale"] or decisions[0]["tiered_status"] == "no_buy_consensus"


def test_failed_analysis_never_buys_on_signal_strength_or_price():
    from agents.screener_fallback import deterministic_signal_decision

    bucket = _growth()
    failure = {
        "tiered_status": "error",
        "analysis_status": "failed",
        "analysis_reason": "invalid_parse",
        "rationale": "All tiered analysis models failed or returned invalid JSON",
    }
    priced = {"current_price": 50.0, "atr14": 1.5}

    strong = deterministic_signal_decision(
        {"ticker": "MSFT", "signal_strength": 70.0, "entry_source": "research"},
        bucket,
        market=priced,
        failure=failure,
    )
    assert strong["action"] == "SKIP"
    assert strong["analysis_path"] == "fail_closed"
    assert strong["skip_reason"] == "analysis_failed"
    assert strong["signal_strength"] == 70.0

    medium = deterministic_signal_decision(
        {"ticker": "S", "signal_strength": 39.1, "entry_source": "research"},
        bucket,
        market=priced,
        failure=failure,
    )
    assert medium["action"] == "SKIP"
    assert medium["skip_reason"] == "analysis_failed"

    no_price = deterministic_signal_decision(
        {"ticker": "NVDA", "signal_strength": 90.0},
        bucket,
        market={"current_price": 0},
        failure=failure,
    )
    assert no_price["action"] == "SKIP"
    assert no_price["skip_reason"] == "analysis_failed"


def test_provider_exhaustion_failure_is_fail_closed():
    from agents.screener_fallback import deterministic_signal_decision

    decision = deterministic_signal_decision(
        {"ticker": "MSFT", "signal_strength": 95.0, "entry_source": "research"},
        _growth(),
        market={"current_price": 500.0},
        failure={
            "analysis_status": "failed",
            "tiered_status": "error",
            "analysis_reason": "analysis_failed",
        },
    )
    assert decision["action"] == "SKIP"
    assert decision["analysis_reason"] == "analysis_failed"
    assert decision["skip_reason"] == "analysis_failed"


def test_protective_sell_still_runs_without_analysis(monkeypatch):
    import agents.position_review as review

    from agenttrade import db

    db.init_db()
    _use_live_risk_defaults(monkeypatch)
    monkeypatch.setattr(review, "get_open_orders", lambda: [])
    monkeypatch.setattr(review, "alpaca_post", lambda path, payload: {"id": "sell-1"})
    monkeypatch.setattr(cfg.bucket_manager, "untag_position", lambda symbol: None)
    monkeypatch.setattr(cfg, "daily_trades", 0)
    positions = [{
        "symbol": "GAIN",
        "qty": 10,
        "avg_entry_price": 100.0,
        "current_price": 80.0,
        "market_value": 800.0,
    }]
    sells = review.review_positions(positions, {"portfolio_value": 107000}, {}, True)
    assert len(sells) == 1
    assert sells[0]["side"] == "sell"
    assert sells[0]["ticker"] == "GAIN"
    assert sells[0]["status"] == "placed"
    assert sells[0]["source"] == "position_review"
    assert cfg.daily_trades == 0
