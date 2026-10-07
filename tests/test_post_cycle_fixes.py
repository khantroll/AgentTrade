"""Oct 7 paper-cycle fixes: JSON in reasoning, cooldown policy, foreign open buys."""

import json
import time

import pytest

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


class _Resp:
    def __init__(self, status, body, headers=None, usage=None):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = headers or {}
        self._usage = usage

    def json(self):
        payload = json.loads(self.text)
        if self.status_code < 400 and self._usage is not None and isinstance(payload, dict):
            payload = dict(payload)
            payload["usage"] = self._usage
        return payload


DECISION = {"decision": "HOLD", "confidence": 0.44, "rationale": "wait for a cleaner setup"}


def test_reasoning_field_json_is_parsed_when_content_is_prose():
    import llm_router

    data = {
        "choices": [{
            "message": {
                "content": "JSON mode was rejected. The decision is in the reasoning trace.",
                "reasoning": "<think>\n" + json.dumps(DECISION) + "\n</think>",
            }
        }]
    }
    parsed = llm_router._parse_json(llm_router._message_text(data))
    assert parsed["decision"] == "HOLD"
    assert parsed["confidence"] == 0.44


def test_decision_inside_think_block_is_kept():
    import llm_router

    text = "<think>\n" + json.dumps({
        "action": "SKIP",
        "confidence": 0.22,
        "rationale": "no edge",
    }) + "\n</think>"
    parsed = llm_router._parse_json(text)
    assert str(parsed.get("action") or parsed.get("decision")).upper() == "SKIP"


def test_prose_without_a_decision_does_not_become_a_buy():
    import llm_router
    from agents.screener_fallback import analysis_result_failed, deterministic_signal_decision

    prose = "JSON mode was rejected. I will only explain why KMI looks interesting."
    assert llm_router._parse_json(prose) is None
    failure = {
        "action": "SKIP",
        "decision": "SKIP",
        "analysis_status": "failed",
        "analysis_reason": "invalid_parse",
        "rationale": "All tiered analysis models failed or returned invalid JSON",
    }
    assert analysis_result_failed(failure) is True
    decision = deterministic_signal_decision(
        {"ticker": "KMI", "signal_strength": 80, "entry_source": "research"},
        Bucket(name="Growth", allocation_pct=0.45, mode="growth"),
        market={"current_price": 28.5},
        failure=failure,
    )
    assert decision["action"] == "SKIP"
    assert decision["analysis_path"] == "fail_closed"


def test_tiered_analysis_accepts_json_embedded_in_reasoning_prose(isolated_llm, monkeypatch):
    llm = isolated_llm
    monkeypatch.setattr(llm, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm, "_provider_ready", lambda provider: True)
    monkeypatch.setattr(llm, "_model_is_suppressed", lambda *a, **k: False)
    monkeypatch.setattr(llm, "_mark_provider_success", lambda provider: None)
    monkeypatch.setattr(llm, "_classify_cooldown", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_tiered_choices", lambda phase, prompt: [
        ("groq_llama", "groq", "openai/gpt-oss-120b"),
    ])
    body = (
        "The model rejected JSON mode and answered in reasoning prose.\n"
        "Scratch note {not json}.\n"
        + json.dumps({"decision": "BUY", "confidence": 0.71, "rationale": "momentum"})
    )

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        return body

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    result = llm._tiered_analysis("decide", agent_tag="Growth_analysis")
    assert result["action"] == "BUY"
    assert result["analysis_status"] == "ok"


def test_tiered_analysis_prose_without_json_stays_invalid_parse(isolated_llm, monkeypatch):
    llm = isolated_llm
    monkeypatch.setattr(llm, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm, "_provider_ready", lambda provider: True)
    monkeypatch.setattr(llm, "_model_is_suppressed", lambda *a, **k: False)
    monkeypatch.setattr(llm, "_mark_provider_success", lambda provider: None)
    monkeypatch.setattr(llm, "_classify_cooldown", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_tiered_choices", lambda phase, prompt: [
        ("groq_llama", "groq", "openai/gpt-oss-120b"),
    ])

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        return "Rejected JSON mode. Reasoning only, no decision object for KMI."

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    result = llm._tiered_analysis("decide", agent_tag="Growth_analysis")
    assert result["analysis_status"] == "failed"
    assert result["analysis_reason"] == "invalid_parse"
    assert result["action"] == "SKIP"


def test_gpt_oss_reads_reasoning_field_and_raises_token_budget(monkeypatch):
    import llm_router

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        body = {
            "choices": [{
                "message": {
                    "content": "JSON mode rejected; see reasoning.",
                    "reasoning": json_lib_dumps(),
                }
            }]
        }
        return _Resp(200, body, usage={"prompt_tokens": 8, "completion_tokens": 12})

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    monkeypatch.setattr(llm_router, "_record_usage", lambda *a, **k: None)
    text = llm_router._call_chat_endpoint(
        "groq", "openai/gpt-oss-120b", "analyze KMI", 120, "analysis",
        "https://example.invalid/v1/chat/completions", "test-key",
    )
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_completion_tokens"] >= 1024
    assert calls[0]["reasoning_effort"] == "low"
    assert "reasoning_format" not in calls[0]
    assert llm_router._parse_json(text)["decision"] == "SKIP"


def json_lib_dumps():
    return json.dumps({"decision": "SKIP", "confidence": 0.31, "rationale": "no edge"})


def test_json_mode_http_200_prose_retries_once_without_response_format(monkeypatch):
    import llm_router

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            body = {"choices": [{"message": {"content": "I will not emit JSON."}}]}
            return _Resp(200, body, usage={"prompt_tokens": 1, "completion_tokens": 2})
        body = {
            "choices": [{
                "message": {
                    "content": '{"decision": "SELL", "confidence": 0.6, "rationale": "broken trend"}',
                }
            }]
        }
        return _Resp(200, body, usage={"prompt_tokens": 3, "completion_tokens": 4})

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    monkeypatch.setattr(llm_router, "_record_usage", lambda *a, **k: None)
    text = llm_router._call_chat_endpoint(
        "groq", "openai/gpt-oss-120b", "analyze", 120, "analysis",
        "https://example.invalid/v1/chat/completions", "test-key",
    )
    assert len(calls) == 2
    assert "response_format" not in calls[1]
    assert llm_router._parse_json(text)["decision"] == "SELL"


def test_unusable_prose_after_plain_retry_is_not_a_decision(monkeypatch):
    import llm_router

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        body = {"choices": [{"message": {"content": "Still only reasoning prose."}}]}
        return _Resp(200, body, usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    monkeypatch.setattr(llm_router, "_record_usage", lambda *a, **k: None)
    text = llm_router._call_chat_endpoint(
        "groq", "qwen/qwen3.8-27b", "analyze", 120, "analysis",
        "https://example.invalid/v1/chat/completions", "test-key",
    )
    assert len(calls) == 2
    assert llm_router._parse_json(text) is None


def test_groq_tpm_five_second_wait_is_not_a_two_hour_bench(isolated_llm):
    llm = isolated_llm
    err = RuntimeError(
        "groq HTTP 429: {\"error\":{\"message\":\"Rate limit reached for model "
        "`openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Used 7500, "
        "Requested 1200. Please try again in 5.4s.\",\"code\":\"rate_limit_exceeded\"}}"
    )
    llm._classify_cooldown("groq", err, model="openai/gpt-oss-120b")
    remaining = llm._provider_cooldown_remaining("groq")
    assert 1 <= remaining < 30
    assert remaining < 7200
    saved = json.load(open(llm.LLM_HEALTH_FILE, encoding="utf-8"))
    assert saved["groq"]["last_error"] == "short rate-limit (provider wait)"


def test_retry_after_header_seconds_are_capped_for_a_short_wait(isolated_llm):
    llm = isolated_llm
    err = RuntimeError("mistral HTTP 429: too many requests Retry-After: 8")
    llm._classify_cooldown("mistral", err, model="mistral-small-latest")
    remaining = llm._provider_cooldown_remaining("mistral")
    assert 1 <= remaining <= 15


def test_unspecified_429_keeps_the_long_rate_limit_bench(isolated_llm, monkeypatch):
    llm = isolated_llm
    monkeypatch.setenv("LLM_RATE_LIMIT_COOLDOWN_MINUTES", "120")
    err = RuntimeError("mistral HTTP 429: rate limit exceeded")
    llm._classify_cooldown("mistral", err, model="mistral-small-latest")
    remaining = llm._provider_cooldown_remaining("mistral")
    assert remaining > 110 * 60


def test_gemini_billing_429_is_a_quota_bench_not_24h(isolated_llm):
    llm = isolated_llm
    err = RuntimeError(
        "gemini HTTP 429: {\"error\":{\"code\":429,\"message\":\"You exceeded your current quota, "
        "please check your plan and billing details.\",\"status\":\"RESOURCE_EXHAUSTED\"}}"
    )
    llm._classify_cooldown("gemini", err, model="gemini-2.5-flash")
    remaining = llm._provider_cooldown_remaining("gemini")
    assert 50 * 60 < remaining < 70 * 60
    assert remaining < 23 * 3600
    saved = json.load(open(llm.LLM_HEALTH_FILE, encoding="utf-8"))
    assert saved["gemini"]["last_error"] == "quota/billing 429"


def test_hard_billing_without_429_is_hours_not_a_full_day(isolated_llm):
    llm = isolated_llm
    err = RuntimeError("openai HTTP 402: billing account disabled, payment required")
    llm._classify_cooldown("openai", err, model="gpt-4o-mini")
    remaining = llm._provider_cooldown_remaining("openai")
    assert 5 * 3600 < remaining < 7 * 3600
    assert remaining < 23 * 3600
    saved = json.load(open(llm.LLM_HEALTH_FILE, encoding="utf-8"))
    assert saved["openai"]["last_error"] == "billing/balance error"


def test_http_410_suppresses_the_model_and_does_not_cool_the_provider(isolated_llm):
    llm = isolated_llm
    err = RuntimeError(
        "nvidia HTTP 410: {\"error\":{\"message\":\"meta/llama-3.1-70b-instruct "
        "end of life since 2026-08-26\"}}"
    )
    llm._classify_cooldown("nvidia", err, model="meta/llama-3.1-70b-instruct")
    assert llm._provider_cooldown_remaining("nvidia") == 0
    assert llm._model_is_suppressed("nvidia", "meta/llama-3.1-70b-instruct")
    assert not llm._model_is_suppressed("nvidia", llm._DEFAULT_NVIDIA_LLAMA)
    remaining = llm._suppressed_until("nvidia", "meta/llama-3.1-70b-instruct") - time.time()
    assert remaining > 23 * 3600
    saved = json.load(open(llm.LLM_HEALTH_FILE, encoding="utf-8"))
    assert "nvidia" not in saved or float(saved.get("nvidia", {}).get("cooldown_until") or 0) <= time.time()


def test_retired_nvidia_llama_id_is_replaced(monkeypatch):
    import llm_router

    assert llm_router._DEFAULT_NVIDIA_LLAMA == "nvidia/nemotron-3-super-120b-a12b"
    monkeypatch.setenv("NVIDIA_LLAMA_MODEL", "meta/llama-3.1-70b-instruct")
    assert llm_router._resolved_nvidia_llama() == "nvidia/nemotron-3-super-120b-a12b"
    monkeypatch.setenv("NVIDIA_LLAMA_MODEL", "nvidia/custom-current")
    assert llm_router._resolved_nvidia_llama() == "nvidia/custom-current"


CASH = 12451.00
BUYING_POWER = 263000.00
HYPE_NOTIONAL = 3287.30
MIN_RESERVE = 5000.0


def _books(portfolio, orders, agent_open=0.0, foreign_open=0.0):
    return {
        "account": {
            "cash": CASH,
            "equity": portfolio,
            "portfolio_value": portfolio,
            "buying_power": BUYING_POWER,
            "long_market_value": 0,
        },
        "cash": CASH,
        "equity": portfolio,
        "portfolio_value": portfolio,
        "buying_power": BUYING_POWER,
        "open_orders": orders,
        "open_buy_notional": round(agent_open + foreign_open, 2),
        "agenttrade_open_buy_notional": agent_open,
        "foreign_open_buy_notional": foreign_open,
        "high_water_equity": portfolio,
    }


def _buy(ticker="AAPL"):
    return {
        "ticker": ticker,
        "action": "BUY",
        "decision": "BUY",
        "current_price": 100.0,
        "confidence": 0.8,
    }


def test_foreign_hype_buy_is_not_agenttrade_reserved_cash(monkeypatch, caplog):
    """Oct 7 paper book: cash ~$12,451, HYPE/USD open buy ~$3,287.30, BP ~$263k.

    The old formula floored usable cash at $0 when portfolio value was large
    enough that the foreign notional plus the 10% reserve exceeded cash.
    The foreign order is not reserved. The 10% reserve and $5,000 floor stay.
    """
    import logging

    import agent_config as cfg
    from account_sync import explain_equity_gap, reserved_open_buy_notional, split_open_buy_notionals
    from agents.risk import risk_agent

    hype = {
        "id": "crypto-hype",
        "symbol": "HYPE/USD",
        "side": "buy",
        "notional": "3287.30",
        "client_order_id": "cryptoagent-hype",
    }
    total, agent, foreign = split_open_buy_notionals([hype], set())
    assert total == pytest.approx(HYPE_NOTIONAL)
    assert agent == 0
    assert foreign == pytest.approx(HYPE_NOTIONAL)

    # equity - (cash + positions) == the foreign hold. That is not an alarm.
    gap = explain_equity_gap(CASH + 50000 + HYPE_NOTIONAL, CASH, 50000, foreign)
    assert gap["equity_mismatch_raw_delta"] == pytest.approx(HYPE_NOTIONAL)
    assert gap["equity_mismatch"] is False
    assert gap["equity_mismatch_delta"] == pytest.approx(0)
    assert gap["equity_mismatch_message"] is None

    # A further $100 gap is still an alarm.
    extra = explain_equity_gap(CASH + 50000 + HYPE_NOTIONAL + 100, CASH, 50000, foreign)
    assert extra["equity_mismatch"] is True
    assert extra["equity_mismatch_delta"] == pytest.approx(100)

    ours = {
        "id": "ord-at",
        "symbol": "AAPL",
        "side": "buy",
        "notional": "3287.30",
        "client_order_id": "agenttrade-abc",
    }
    _, agent_only, foreign_only = split_open_buy_notionals([ours], set())
    assert agent_only == pytest.approx(HYPE_NOTIONAL)
    assert foreign_only == 0
    own_gap = explain_equity_gap(CASH + 50000 + HYPE_NOTIONAL, CASH, 50000, foreign_only)
    assert own_gap["equity_mismatch"] is True

    legacy = {"id": "ord-legacy", "symbol": "MSFT", "side": "buy", "notional": "100"}
    _, legacy_agent, legacy_foreign = split_open_buy_notionals([legacy], {"ord-legacy"})
    assert legacy_agent == pytest.approx(100)
    assert legacy_foreign == 0

    monkeypatch.setattr(cfg, "RESERVE_CASH_PCT", 0.10)
    monkeypatch.setattr(cfg, "MIN_CASH_RESERVE", MIN_RESERVE)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False)
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False)
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr("agents.risk.ledger.trading_paused", lambda: False)

    # Portfolio $100k: old formula floors at $0. New usable is cash minus the
    # 10% reserve only, which is still under the $5,000 floor.
    large = _books(100000, [hype], agent_open=0, foreign_open=HYPE_NOTIONAL)
    old_usable = max(CASH - HYPE_NOTIONAL - 100000 * 0.10, 0)
    assert old_usable == 0
    new_usable = max(CASH - reserved_open_buy_notional(large) - 100000 * 0.10, 0)
    assert new_usable == pytest.approx(2451.00)
    assert new_usable < MIN_RESERVE
    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth")
    with caplog.at_level(logging.WARNING):
        blocked = risk_agent([_buy()], large["account"], [], bucket, account_snapshot=large)
    assert blocked == []
    assert "2451.00" in caplog.text
    assert "5000.00" in caplog.text

    # Portfolio $70k: excluding the foreign $3,287.30 clears the $5,000 floor.
    # The same notional on an AgentTrade order does not.
    caplog.clear()
    mid = _books(70000, [hype], agent_open=0, foreign_open=HYPE_NOTIONAL)
    mid_usable = max(CASH - reserved_open_buy_notional(mid) - 70000 * 0.10, 0)
    assert mid_usable == pytest.approx(5451.00)
    assert mid_usable >= MIN_RESERVE
    allowed = risk_agent([_buy()], mid["account"], [], bucket, account_snapshot=mid)
    assert [row["ticker"] for row in allowed] == ["AAPL"]
    assert "below MIN_CASH_RESERVE" not in caplog.text

    reserved = _books(70000, [ours], agent_open=HYPE_NOTIONAL, foreign_open=0)
    reserved_usable = max(CASH - reserved_open_buy_notional(reserved) - 70000 * 0.10, 0)
    assert reserved_usable == pytest.approx(CASH - HYPE_NOTIONAL - 7000)
    assert reserved_usable < MIN_RESERVE
    with caplog.at_level(logging.WARNING):
        still_blocked = risk_agent(
            [_buy("MSFT")], reserved["account"], [], bucket, account_snapshot=reserved,
        )
    assert still_blocked == []
    assert "below MIN_CASH_RESERVE" in caplog.text


NEMOTRON_PROSE = "We need to output JSON with selected picks and a confidence for each ticker."
NEMOTRON_MODEL = "nvidia/nemotron-3-super-120b-a12b"


def test_prose_before_json_is_parsed_and_prose_only_is_not():
    import llm_router

    embedded = NEMOTRON_PROSE + "\n" + json.dumps({
        "selected": [{"ticker": "AAPL", "reason": "momentum", "confidence": 0.72}],
    })
    parsed = llm_router._parse_json(embedded)
    assert parsed["selected"][0]["ticker"] == "AAPL"
    assert llm_router._parse_json(NEMOTRON_PROSE) is None


def test_nemotron_request_disables_thinking_on_any_host(monkeypatch):
    import llm_router

    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("LLM_REASONING_JSON_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_THINKING_MODELS", raising=False)
    base = {"max_tokens": 180, "messages": [{"role": "user", "content": "pick"}]}
    nvidia = llm_router._adjust_chat_payload("nvidia", NEMOTRON_MODEL, dict(base))
    routed = llm_router._adjust_chat_payload("openrouter", NEMOTRON_MODEL, dict(base))
    groq = llm_router._adjust_chat_payload("groq", "qwen/qwen3.8-27b", dict(base, max_tokens=140))
    assert nvidia["chat_template_kwargs"] == {"enable_thinking": False}
    assert nvidia["max_tokens"] >= 1024
    assert "max_completion_tokens" not in nvidia
    assert routed["chat_template_kwargs"]["enable_thinking"] is False
    assert "chat_template_kwargs" not in groq
    assert groq["max_tokens"] == 140

    monkeypatch.setenv("LLM_ENABLE_THINKING", "1")
    thinking = llm_router._adjust_chat_payload("nvidia", NEMOTRON_MODEL, dict(base))
    assert "chat_template_kwargs" not in thinking
    assert thinking["max_tokens"] >= 1024


def test_host_that_rejects_thinking_kwargs_is_retried_without_them(monkeypatch):
    import llm_router

    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _Resp(400, {"error": {"message": "unexpected field chat_template_kwargs.enable_thinking"}})
        body = (
            '{"choices":[{"message":{"content":'
            '"{\\"selected\\":[{\\"ticker\\":\\"MSFT\\",\\"reason\\":\\"ready\\",\\"confidence\\":0.4}]}"'
            '}}]}'
        )
        return _Resp(200, body, usage={"prompt_tokens": 4, "completion_tokens": 8})

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    monkeypatch.setattr(llm_router, "_record_usage", lambda *a, **k: None)
    text = llm_router._call_chat_endpoint(
        "nvidia", NEMOTRON_MODEL, "pick", 180, "research",
        "https://example.invalid/v1/chat/completions", "test-key",
    )
    assert len(calls) == 2
    assert calls[0]["chat_template_kwargs"]["enable_thinking"] is False
    assert "chat_template_kwargs" not in calls[1]
    assert calls[1]["max_tokens"] >= 1024
    assert llm_router._parse_json(text)["selected"][0]["ticker"] == "MSFT"


def _research_choices(phase, prompt):
    return [
        ("nvidia_llama", "nvidia", NEMOTRON_MODEL),
        ("groq_qwen", "groq", "qwen/qwen3.8-27b"),
    ]


def test_unusable_nemotron_research_fails_over_to_the_next_ready_provider(isolated_llm, monkeypatch):
    llm = isolated_llm
    cooled = []
    monkeypatch.setattr(llm, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm, "_provider_ready", lambda provider: True)
    monkeypatch.setattr(llm, "_model_is_suppressed", lambda *a, **k: False)
    monkeypatch.setattr(llm, "_mark_provider_success", lambda provider: None)
    monkeypatch.setattr(llm, "_classify_cooldown", lambda *a, **k: cooled.append(a))
    monkeypatch.setattr(llm, "_tiered_choices", _research_choices)

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        if provider == "nvidia":
            return NEMOTRON_PROSE
        return json.dumps({
            "selected": [{"ticker": "MSFT", "reason": "next provider", "confidence": 0.61}],
        })

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    result = llm._tiered_research("pick names", agent_tag="Growth_research")
    assert result["research_status"] == "ok"
    assert result["selected"][0]["ticker"] == "MSFT"
    assert result["tiered_models_used"] == ["groq_qwen"]
    assert cooled == []


def test_prose_only_research_stays_invalid_parse_when_no_other_provider_is_ready(isolated_llm, monkeypatch):
    llm = isolated_llm
    monkeypatch.setattr(llm, "_tiered_fanout", lambda phase: 1)
    monkeypatch.setattr(llm, "_provider_ready", lambda provider: provider == "nvidia")
    monkeypatch.setattr(llm, "_model_is_suppressed", lambda *a, **k: False)
    monkeypatch.setattr(llm, "_mark_provider_success", lambda provider: None)
    monkeypatch.setattr(llm, "_classify_cooldown", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_tiered_choices", lambda phase, prompt: [
        ("nvidia_llama", "nvidia", NEMOTRON_MODEL),
    ])

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        return NEMOTRON_PROSE

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    result = llm._tiered_research("pick names", agent_tag="Growth_research")
    assert result["research_status"] == "failed"
    assert result["research_reason"] == "invalid_parse"
    assert result["selected"] == []


def test_legacy_7200s_rate_limit_is_clamped_on_load(isolated_llm, monkeypatch):
    llm = isolated_llm
    monkeypatch.setenv("LLM_SHORT_RATE_LIMIT_CAP_SECONDS", "900")
    now = time.time()
    health = {
        "groq": {
            "success": 1, "fail": 2, "cooldown_until": now + 7200,
            "last_error": "rate-limit (429)", "last_ok": "",
        },
        "mistral": {
            "success": 1, "fail": 2, "cooldown_until": now + 7000,
            "last_error": "rate-limit (429)", "last_ok": "",
        },
        "gemini": {
            "success": 0, "fail": 1, "cooldown_until": now + 3600,
            "last_error": "quota/billing 429", "last_ok": "",
        },
        "openai": {
            "success": 0, "fail": 1, "cooldown_until": now + 6 * 3600,
            "last_error": "billing/balance error", "last_ok": "",
        },
    }
    with open(llm.LLM_HEALTH_FILE, "w", encoding="utf-8") as fh:
        json.dump(health, fh)
    llm._PROVIDER_COOLDOWN_UNTIL.clear()
    assert 800 < llm._provider_cooldown_remaining("groq") <= 900.5
    assert 800 < llm._provider_cooldown_remaining("mistral") <= 900.5
    assert llm._provider_cooldown_remaining("gemini") > 3000
    assert llm._provider_cooldown_remaining("openai") > 5 * 3600
    saved = json.load(open(llm.LLM_HEALTH_FILE, encoding="utf-8"))
    assert saved["groq"]["cooldown_class"] == "legacy_rate_limit_clamped"
    assert saved["mistral"]["cooldown_class"] == "legacy_rate_limit_clamped"
    assert "cooldown_class" not in saved["gemini"]
    assert "cooldown_class" not in saved["openai"]


def test_classified_long_rate_limit_is_not_clamped_on_load(isolated_llm, monkeypatch):
    llm = isolated_llm
    monkeypatch.setenv("LLM_SHORT_RATE_LIMIT_CAP_SECONDS", "900")
    now = time.time()
    health = {
        "mistral": {
            "success": 0, "fail": 1, "cooldown_until": now + 7200,
            "last_error": "rate-limit (429)", "cooldown_class": "rate_limit",
            "cooldown_seconds": 7200,
        }
    }
    with open(llm.LLM_HEALTH_FILE, "w", encoding="utf-8") as fh:
        json.dump(health, fh)
    llm._PROVIDER_COOLDOWN_UNTIL.clear()
    assert llm._provider_cooldown_remaining("mistral") > 110 * 60


def test_live_book_cash_reserve_block_shows_the_numbers(monkeypatch, caplog):
    """Oct 7 14:00 CT book: cash $12,451, equity ~$105.9k, usable $1,860, floor $5,000."""
    import logging

    import agent_config as cfg
    from agents.risk import cash_gate_summary, risk_agent

    equity = 105910.0
    usable = CASH - equity * 0.10
    assert usable == pytest.approx(1860.0)
    monkeypatch.setattr(cfg, "RESERVE_CASH_PCT", 0.10)
    monkeypatch.setattr(cfg, "MIN_CASH_RESERVE", MIN_RESERVE)
    monkeypatch.setattr(cfg, "ALLOW_NEGATIVE_CASH", False)
    monkeypatch.setattr(cfg, "ALLOW_MARGIN", False)
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr("agents.risk.ledger.trading_paused", lambda: False)
    book = _books(equity, [], agent_open=0, foreign_open=0)
    decisions = [_buy("AAA"), _buy("BBB"), {"ticker": "CCC", "action": "SKIP", "current_price": 10}]
    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth")
    with caplog.at_level(logging.WARNING):
        approved = risk_agent(decisions, book["account"], [], bucket, account_snapshot=book)
    assert approved == []
    assert decisions[0]["blocked_reason"].startswith("below_min_cash_reserve")
    assert "1860.00" in decisions[0]["blocked_reason"]
    assert "5000.00" in decisions[0]["blocked_reason"]
    assert decisions[0]["cash_gate"]["usable_cash"] == pytest.approx(1860.0)
    assert decisions[0]["cash_gate"]["cash"] == pytest.approx(CASH)
    assert decisions[0]["cash_gate"]["min_cash_reserve"] == pytest.approx(MIN_RESERVE)
    assert decisions[0]["cash_gate"]["reserve_dollars"] == pytest.approx(10591.0)
    assert "blocked_reason" not in decisions[2]
    summary = cash_gate_summary(decisions)
    assert summary["buys_blocked"] == 2
    assert summary["usable_cash"] == pytest.approx(1860.0)
    assert "Buys blocked by cash reserve" in caplog.text
    assert "1860.00" in caplog.text


def test_deploy_sha_files_are_rewritten_together(tmp_path):
    import subprocess
    from pathlib import Path

    from deploy_sha import reconcile_deploy_sha, write_both

    script = Path(__file__).resolve().parents[1] / "write_deploy_sha.sh"
    subprocess.check_call(["bash", str(script), str(tmp_path), "60a1dc10"])
    assert (tmp_path / "DEPLOY_SHA.txt").read_text(encoding="utf-8").strip() == "60a1dc10"
    assert (tmp_path / "DEPLOY_SHA").read_text(encoding="utf-8").strip() == "60a1dc10"

    (tmp_path / "DEPLOY_SHA").write_text("4cc04198\n", encoding="utf-8")
    subprocess.check_call([
        "bash", "-c",
        'source "$1" && reconcile_deploy_sha "$2"',
        "bash", str(script), str(tmp_path),
    ])
    assert (tmp_path / "DEPLOY_SHA.txt").read_text(encoding="utf-8").strip() == "60a1dc10"
    assert (tmp_path / "DEPLOY_SHA").read_text(encoding="utf-8").strip() == "60a1dc10"

    other = tmp_path / "py"
    other.mkdir()
    (other / "DEPLOY_SHA.txt").write_text("60a1dc10\n", encoding="utf-8")
    (other / "DEPLOY_SHA").write_text("4cc04198\n", encoding="utf-8")
    assert reconcile_deploy_sha(other) == "60a1dc10"
    assert (other / "DEPLOY_SHA").read_text(encoding="utf-8").strip() == "60a1dc10"
    assert write_both(other, "abcdef0") == "abcdef0"
    assert (other / "DEPLOY_SHA.txt").read_text(encoding="utf-8").strip() == "abcdef0"
    assert (other / "DEPLOY_SHA").read_text(encoding="utf-8").strip() == "abcdef0"
