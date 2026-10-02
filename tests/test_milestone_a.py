"""Milestone A: research failure vs empty, parse-gated routing, screener fallback, exits."""

import json
import logging
import time

import pytest

from agents.research import ResearchOutcome
from agents.screener_fallback import (
    deterministic_screener_decisions,
    resolve_entry_candidates,
    screener_fallback_candidates,
)
from buckets import ALLOCATION_DEFAULTS, Bucket, allocation_pct_for
from cycle import summarize_research_status


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


def test_research_failed_is_not_reported_as_empty():
    failed = summarize_research_status({
        "Growth": {"status": "failed", "reason": "invalid_parse", "entry_source": "screener", "candidates": 0},
    })
    empty = summarize_research_status({
        "Growth": {"status": "empty", "reason": "valid_empty_selected", "entry_source": "screener", "candidates": 0},
    })
    ok = summarize_research_status({
        "Growth": {"status": "ok", "reason": "", "entry_source": "research", "candidates": 2},
    })
    assert failed["status"] == "failed"
    assert failed["entry_source"] == "screener"
    assert failed["failed_buckets"] == ["Growth"]
    assert empty["status"] == "empty"
    assert empty["entry_source"] == "screener"
    assert ok["status"] == "ok"
    assert ok["entry_source"] == "research"
    assert summarize_research_status({})["status"] == "not_run"


def test_screener_fallback_keeps_universe_order_and_provenance():
    """Research failure still uses the screener. Strength, not list order, ranks it.

    ``total_score`` without ``signal_strength`` is the legacy strength field.
    ``screener_rank`` stays the original ensemble position.
    """
    universe = ["MSFT", "AAPL"]
    sources = {
        "attribution": {
            "AAPL": {"total_score": 100, "components": {"momentum": 100}, "pipelines": {"Momentum": {"rank": 0}}},
            "MSFT": {"total_score": 10, "components": {"news_sentiment": 10}, "pipelines": {"News Sentiment": {"rank": 0}}},
        }
    }
    rows = screener_fallback_candidates(universe, sources, "Growth", top_n=5)
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"]
    assert all(r["source"] == "screener" for r in rows)
    assert all(r["candidate_source"] == "screener" for r in rows)
    assert rows[0]["screener_rank"] == 2
    assert rows[0]["signal_strength"] == 100
    assert rows[0]["total_score"] == 100
    assert "Screener ensemble rank 2/2" in rows[0]["reason"]
    assert "signal_strength 100" in rows[0]["reason"]

    failed = ResearchOutcome([], "failed", "invalid_parse")
    picked, source = resolve_entry_candidates(failed, universe, sources, "Growth", 5)
    assert source == "screener"
    assert [r["ticker"] for r in picked] == ["AAPL", "MSFT"]

    empty = ResearchOutcome([], "empty", "valid_empty_selected")
    picked, source = resolve_entry_candidates(empty, universe, sources, "Growth", 5)
    assert source == "screener"

    ok = ResearchOutcome([{"ticker": "nvda", "reason": "llm", "confidence": 0.8}], "ok", "")
    picked, source = resolve_entry_candidates(ok, universe, sources, "Growth", 5)
    assert source == "research"
    assert picked[0]["ticker"] == "NVDA"
    assert picked[0]["source"] == "research"
    assert picked[0]["candidate_source"] == "research"

    none_rows, none_source = resolve_entry_candidates(failed, [], {}, "Growth", 5)
    assert none_rows == []
    assert none_source == "none"


def test_deterministic_screener_decisions_do_not_call_analysis_llm(monkeypatch):
    import market_data

    monkeypatch.setattr(
        market_data, "fetch_stock_data",
        lambda ticker: {"current_price": 20.0, "atr_pct": 2.5, "atr14": 0.5, "symbol": ticker},
    )
    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth")
    candidates = screener_fallback_candidates(["AAA", "BBB", "CCC"], {}, "Growth", 5)
    decisions = deterministic_screener_decisions(candidates, bucket)
    buys = [d for d in decisions if d["action"] == "BUY"]
    assert buys
    assert all(d["analysis_path"] == "deterministic_screener" for d in decisions)
    assert all(d["source"] == "screener" for d in decisions)
    assert all(d["current_price"] == 20.0 for d in buys)
    assert len(buys) == 2  # MAX_ANALYSIS_CANDIDATES


def test_unusable_body_is_not_success_and_next_model_is(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    marks = []
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: marks.append(provider))
    monkeypatch.setattr(llm_router, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("groq_qwen", "groq", "dead"),
        ("gemini_flash", "gemini", "ok"),
    ])
    monkeypatch.setattr(llm_router, "_provider_ready", lambda provider: True)
    bodies = {
        "dead": "Here is the JSON requested",
        "ok": json.dumps({"selected": [{"ticker": "AAPL", "reason": "parsed", "confidence": 0.8}]}),
    }

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        assert mark_success is False
        return bodies[model]

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router._tiered_research("prompt", agent_tag="growth_research")
    assert result["research_status"] == "ok"
    assert result["selected"][0]["ticker"] == "AAPL"
    assert marks == ["gemini"]


def test_empty_object_and_fence_only_do_not_count_as_success(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    marks = []
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: marks.append(provider))
    monkeypatch.setattr(llm_router, "_effective_mode", lambda: "groq_qwen")
    monkeypatch.setattr(llm_router, "_pick_model", lambda phase, mode: ("groq", "bad"))
    monkeypatch.setattr(llm_router, "_fallback_models", lambda phase, exclude_provider="": [("gemini", "good")])
    monkeypatch.setattr(llm_router, "_provider_ready", lambda provider: True)
    monkeypatch.setattr(llm_router, "_model_is_suppressed", lambda provider, model: False)
    bodies = {"bad": "{}", "good": json.dumps({"selected": [{"ticker": "MSFT", "reason": "ok", "confidence": 0.7}]})}
    seen = []

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        seen.append(model)
        return bodies[model]

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router.query_research("prompt")
    assert seen == ["bad", "good"]
    assert result["research_status"] == "ok"
    assert result["selected"][0]["ticker"] == "MSFT"
    assert marks == ["gemini"]

    marks.clear()
    seen.clear()
    bodies["bad"] = "```json\n```"
    result = llm_router.query_research("prompt")
    assert result["selected"][0]["ticker"] == "MSFT"
    assert marks == ["gemini"]


def test_valid_empty_selected_is_empty_not_failed(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    marks = []
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: marks.append(provider))
    monkeypatch.setattr(llm_router, "_effective_mode", lambda: "groq_qwen")
    monkeypatch.setattr(llm_router, "_pick_model", lambda phase, mode: ("groq", "empty"))
    monkeypatch.setattr(llm_router, "_fallback_models", lambda phase, exclude_provider="": [("gemini", "should-not")])
    monkeypatch.setattr(llm_router, "_provider_ready", lambda provider: True)
    monkeypatch.setattr(llm_router, "_model_is_suppressed", lambda provider, model: False)

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        assert model == "empty"
        return '{"selected": []}'

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router.query_research("prompt")
    assert result["research_status"] == "empty"
    assert result["selected"] == []
    assert marks == ["groq"]


def test_tiered_research_walks_until_n_valid_parses(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    marks = []
    monkeypatch.setenv("LLM_TIERED_FANOUT", "2")
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: marks.append(provider))
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("groq_qwen", "groq", "prose"),
        ("gemini_flash", "gemini", "empty-object"),
        ("mistral_small", "mistral", "ok1"),
        ("nvidia_llama", "nvidia", "ok2"),
        ("gpt4o_mini", "openai", "extra"),
    ])
    monkeypatch.setattr(llm_router, "_provider_ready", lambda provider: True)
    bodies = {
        "prose": "Sure, I would buy something.",
        "empty-object": "{}",
        "ok1": json.dumps({"selected": [{"ticker": "AAPL", "reason": "one", "confidence": 0.8}]}),
        "ok2": json.dumps({"selected": [{"ticker": "MSFT", "reason": "two", "confidence": 0.7}]}),
        "extra": json.dumps({"selected": [{"ticker": "NVDA", "reason": "nope", "confidence": 0.9}]}),
    }
    seen = []

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        seen.append(model)
        return bodies[model]

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router._tiered_research("prompt")
    assert seen == ["prose", "empty-object", "ok1", "ok2"]
    assert marks == ["mistral", "nvidia"]
    assert result["research_status"] == "ok"
    assert {row["ticker"] for row in result["selected"]} == {"AAPL", "MSFT"}


def test_rate_limit_skips_to_next_tier_and_is_not_an_empty_day(isolated_llm, monkeypatch):
    llm_router = isolated_llm
    marks = []
    monkeypatch.setenv("LLM_RATE_LIMIT_COOLDOWN_MINUTES", "120")
    monkeypatch.setattr(llm_router, "_mark_provider_success", lambda provider: marks.append(provider))
    monkeypatch.setattr(llm_router, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm_router, "_provider_ready", lambda provider: True)
    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("mistral_small", "mistral", "mistral-small"),
        ("mistral_large", "mistral", "mistral-large"),
        ("gemini_flash", "gemini", "gemini"),
    ])
    seen = []

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        seen.append(model)
        if provider == "mistral":
            raise RuntimeError("mistral HTTP 429: rate limit exceeded")
        return json.dumps({"selected": [{"ticker": "AAPL", "reason": "fallback", "confidence": 0.8}]})

    monkeypatch.setattr(llm_router, "_call_provider", fake_call)
    result = llm_router._tiered_research("prompt")
    assert seen == ["mistral-small", "gemini"]
    assert "mistral-large" not in seen
    assert marks == ["gemini"]
    assert result["research_status"] == "ok"
    assert result["selected"][0]["ticker"] == "AAPL"
    assert llm_router._PROVIDER_COOLDOWN_UNTIL["mistral"] - time.time() > 110 * 60

    monkeypatch.setattr(llm_router, "_tiered_choices", lambda phase, prompt: [
        ("mistral_small", "mistral", "mistral-small"),
    ])
    llm_router._PROVIDER_COOLDOWN_UNTIL.clear()
    failed = llm_router._tiered_research("prompt")
    assert failed["research_status"] == "failed"
    assert failed["research_reason"] == "rate_limited"
    assert failed["selected"] == []


def test_model_not_found_suppresses_for_hours_and_is_skipped(isolated_llm, monkeypatch, caplog):
    llm_router = isolated_llm
    monkeypatch.setenv("LLM_MODEL_NOT_FOUND_SUPPRESS_HOURS", "24")
    err = RuntimeError(
        'groq HTTP 404: {"error":{"code":"model_not_found","message":"The model `llama-3.3-70b-versatile` does not exist"}}'
    )
    with caplog.at_level(logging.INFO):
        llm_router._classify_cooldown("groq", err, model="llama-3.3-70b-versatile")
    assert llm_router._model_is_suppressed("groq", "llama-3.3-70b-versatile")
    assert not llm_router._model_is_suppressed("groq", "qwen/qwen3.8-27b")
    remaining = llm_router._suppressed_until("groq", "llama-3.3-70b-versatile") - time.time()
    assert remaining > 23 * 3600
    assert llm_router._PROVIDER_COOLDOWN_UNTIL.get("groq", 0) <= time.time()
    assert "Suppressing model groq/llama-3.3-70b-versatile" in caplog.text

    monkeypatch.setattr(llm_router, "_tiered_aliases", lambda phase: ["groq_llama", "gemini_flash"])
    monkeypatch.setattr(llm_router, "_provider_ready", lambda provider: True)
    monkeypatch.setattr(llm_router, "LLM_AUTO_ADAPT", False)
    previous = llm_router.MODEL_ALIASES["groq_llama"]
    llm_router.MODEL_ALIASES["groq_llama"] = ("groq", "llama-3.3-70b-versatile")
    try:
        caplog.clear()
        with caplog.at_level(logging.INFO):
            choices = llm_router._tiered_choices("research", "prompt")
        assert [alias for alias, _provider, _model in choices] == ["gemini_flash"]
        assert "Skipping suppressed model groq/llama-3.3-70b-versatile" in caplog.text
    finally:
        llm_router.MODEL_ALIASES["groq_llama"] = previous


def test_groq_and_bucket_defaults():
    text = open("llm_router.py", encoding="utf-8").read()
    assert 'GROQ_QWEN_MODEL",     "qwen/qwen3.8-27b"' in text
    assert 'GROQ_LLAMA_MODEL",    "openai/gpt-oss-120b"' in text
    assert "llama-3.1-8b-instant" not in text
    assert 'GROQ_LLAMA_MODEL",    "llama-3.3-70b-versatile"' not in text
    defaults = [item[1] for item in ALLOCATION_DEFAULTS.values()]
    assert defaults == [0.45, 0.25, 0.20, 0.10]
    assert sum(defaults) == pytest.approx(1.0)


def test_allocation_env_override(monkeypatch):
    monkeypatch.setenv("CRYPTO_MAX_ALLOCATION", "0")
    monkeypatch.setenv("GROWTH_ALLOCATION", "0.40")
    assert allocation_pct_for("Crypto") == 0.0
    assert allocation_pct_for("Growth") == 0.40
    monkeypatch.delenv("SWING_ALLOCATION", raising=False)
    assert allocation_pct_for("Swing") == 0.20


def _run_stubbed_cycle(monkeypatch, *, budget, outcome, universe):
    import cycle
    from agenttrade.reconciliation import ReconciliationResult

    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth")
    account = {
        "portfolio_value": "100000",
        "equity": "100000",
        "cash": "80000",
        "buying_power": "80000",
        "long_market_value": "20000",
        "short_market_value": "0",
        "multiplier": "1",
    }
    positions = [{
        "symbol": "GAIN", "qty": "10", "avg_entry_price": "12",
        "current_price": "9", "market_value": "90", "side": "long",
    }]
    snapshot = {"account": account, "positions": positions, "open_orders": [], "recent_fills": []}
    calls = {"review": 0, "hard": 0, "research": 0, "analysis": 0}

    monkeypatch.setattr(cycle, "budget_exhausted", lambda: budget)
    monkeypatch.setattr(cycle, "active_mode", lambda: "tiered")
    monkeypatch.setattr(cycle, "is_market_open", lambda: True)
    monkeypatch.setattr(cycle, "usage_summary", lambda: {
        "total_tokens": 0, "total_cost_usd": 0, "calls": 0, "by_model": {}, "calls_log": [],
    })
    monkeypatch.setattr(cycle, "get_recent_fills", lambda n=30: [])
    monkeypatch.setattr(cycle, "refresh_alpaca_snapshot", lambda: snapshot)
    monkeypatch.setattr(cycle, "load_prior_state", lambda: {})
    monkeypatch.setattr(cycle, "sync_sell_fills", lambda ctx, fills: ctx or {})
    monkeypatch.setattr(cycle, "evaluate_buy_lock", lambda ctx: {"active": False, "locked_symbols": []})
    monkeypatch.setattr(cycle, "apply_lock_fields_to_state", lambda state, ctx, lock: state)
    monkeypatch.setattr(cycle, "merge_snapshot_into_state", lambda state, snap, source="cycle": state)
    monkeypatch.setattr(cycle, "append_cycle_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "publish_history", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "sync_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "publish_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "evaluate_pending_outcomes", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "append_order_meta", lambda *a, **k: None)

    def review(*_a, **_k):
        calls["review"] += 1
        return []

    def hard(*_a, **_k):
        calls["hard"] += 1
        return [], []

    def research(*_a, **_k):
        calls["research"] += 1
        return outcome

    def analysis(*_a, **_k):
        calls["analysis"] += 1
        return [{
            "ticker": "NVDA", "action": "BUY", "confidence": 0.8,
            "rationale": "llm", "source": "research", "current_price": 50,
        }]

    monkeypatch.setattr(cycle, "review_positions", review)
    monkeypatch.setattr(cycle, "hard_rebalance_agent", hard)
    monkeypatch.setattr(cycle, "research_agent", research)
    monkeypatch.setattr(cycle, "analysis_agent", analysis)
    monkeypatch.setattr(cycle, "execution_agent", lambda *a, **k: [])

    sources = {"momentum": 2, "mode": "growth", "attribution": {
        "MSFT": {"total_score": 10, "components": {"momentum": 10}, "pipelines": {"Momentum": {"rank": 0}}},
    }}
    monkeypatch.setattr(cycle, "get_universe", lambda **_k: (list(universe), sources))

    import agent_config as cfg
    monkeypatch.setattr(cfg, "refresh_config", lambda: None)
    monkeypatch.setattr(cfg.bucket_manager, "rebalance_report", lambda positions, pv: {
        "Growth": {"target_pct": 45, "current_pct": 10, "drift_$": -1000, "action": "ADD", "positions": []},
    })
    monkeypatch.setattr(cfg.bucket_manager, "prioritize_buckets", lambda report: [bucket])

    import agenttrade.reconciliation as recon
    monkeypatch.setattr(recon, "run_reconciliation_gate", lambda *a, **k: ReconciliationResult(
        passed=True, status="passed", message="ok", snapshot=snapshot,
    ))
    monkeypatch.setattr(recon, "sync_ledger_from_alpaca", lambda *a, **k: snapshot)

    import agenttrade.risk as risk
    monkeypatch.setattr(risk, "get_consecutive_loss_status", lambda limit=20: {
        "count": 0, "source": "test", "ledger_complete": True, "message": "",
    })
    monkeypatch.setattr(risk, "check_consecutive_loss_pause", lambda: (False, ""))

    import market_data
    monkeypatch.setattr(market_data, "fetch_stock_data", lambda ticker: {
        "current_price": 50.0, "atr_pct": 2.0, "atr14": 1.0, "symbol": ticker,
    })

    cycle.run_trading_cycle()
    return calls


def test_exits_run_and_screener_entries_when_research_fails(monkeypatch):
    calls = _run_stubbed_cycle(
        monkeypatch,
        budget=False,
        outcome=ResearchOutcome([], "failed", "invalid_parse"),
        universe=["MSFT", "AAPL"],
    )
    assert calls["review"] == 1
    assert calls["hard"] == 1
    assert calls["research"] == 1
    assert calls["analysis"] == 0

    import agent_config as cfg
    state = json.loads(open(cfg.STATE_FILE, encoding="utf-8").read())
    assert state["research_status"]["status"] == "failed"
    assert state["research_status"]["entry_source"] == "screener"
    assert state["research_status"]["status"] != "empty"
    tickers = [c["ticker"] for c in state["trade_candidates"]]
    assert tickers[:2] == ["MSFT", "AAPL"]
    assert all(c.get("source") == "screener" for c in state["trade_candidates"])
    assert any(d.get("action") == "BUY" and d.get("analysis_path") == "deterministic_screener" for d in state["decisions"])


def test_budget_exhaustion_still_reviews_positions_and_uses_screener(monkeypatch):
    calls = _run_stubbed_cycle(
        monkeypatch,
        budget=True,
        outcome=ResearchOutcome([{"ticker": "SHOULD_NOT", "confidence": 0.9, "reason": "llm"}], "ok", ""),
        universe=["MSFT"],
    )
    assert calls["review"] == 1
    assert calls["hard"] == 1
    assert calls["research"] == 0
    assert calls["analysis"] == 0
    import agent_config as cfg
    state = json.loads(open(cfg.STATE_FILE, encoding="utf-8").read())
    assert state["research_status"]["status"] == "failed"
    assert state["research_status"]["buckets"]["Growth"]["reason"] == "budget_exhausted"
    assert state["research_status"]["entry_source"] == "screener"
    assert state["trade_candidates"][0]["ticker"] == "MSFT"
    assert state["trade_candidates"][0]["source"] == "screener"


def test_successful_research_keeps_llm_picks(monkeypatch):
    calls = _run_stubbed_cycle(
        monkeypatch,
        budget=False,
        outcome=ResearchOutcome([{"ticker": "NVDA", "reason": "llm", "confidence": 0.8}], "ok", ""),
        universe=["MSFT", "AAPL"],
    )
    assert calls["review"] == 1
    assert calls["analysis"] == 1
    import agent_config as cfg
    state = json.loads(open(cfg.STATE_FILE, encoding="utf-8").read())
    assert state["research_status"]["status"] == "ok"
    assert state["research_status"]["entry_source"] == "research"
    assert state["trade_candidates"][0]["ticker"] == "NVDA"
    assert state["trade_candidates"][0]["source"] == "research"
