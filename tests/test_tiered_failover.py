"""Tiered research failover: one cooldown must not empty a pool that still has a model."""

import json
import time

import pytest


@pytest.fixture
def isolated_llm(monkeypatch, tmp_path):
    import llm_router

    monkeypatch.setattr(llm_router, "LLM_HEALTH_FILE", str(tmp_path / "llm_health.json"))
    monkeypatch.setattr(llm_router, "TOKEN_USAGE_FILE", str(tmp_path / "token_usage.json"))
    monkeypatch.setattr(llm_router, "LLM_AUTO_ADAPT", False)
    monkeypatch.setattr(llm_router, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(llm_router, "NVIDIA_API_KEY", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_TIERED_RESEARCH_MODELS", "groq_qwen,gemini_flash,mistral_small")
    monkeypatch.setenv("LLM_TIERED_ANALYSIS_MODELS", "gemini_flash,groq_llama,mistral_small")
    monkeypatch.setenv("LLM_TIERED_AUTO_FAILOVER", "1")
    monkeypatch.setenv("LLM_TIERED_FANOUT", "3")
    for provider in ("groq", "gemini", "mistral", "nvidia", "openrouter"):
        monkeypatch.setitem(llm_router.PROVIDER_KEYS, provider, False)
    llm_router._MODEL_SUPPRESS_UNTIL.clear()
    llm_router._PROVIDER_COOLDOWN_UNTIL.clear()
    yield llm_router
    llm_router._MODEL_SUPPRESS_UNTIL.clear()
    llm_router._PROVIDER_COOLDOWN_UNTIL.clear()


def _enable(llm, *providers):
    for provider in providers:
        llm.PROVIDER_KEYS[provider] = True


def _cool(llm, *providers, seconds=3600):
    until = time.time() + seconds
    for provider in providers:
        llm._PROVIDER_COOLDOWN_UNTIL[provider] = until


def _picks(ticker="AAPL"):
    return json.dumps({"selected": [{"ticker": ticker, "reason": "parsed", "confidence": 0.8}]})


def test_one_provider_cooldown_uses_the_next_ready_tier(isolated_llm, monkeypatch):
    llm = isolated_llm
    _enable(llm, "groq", "gemini", "mistral")
    _cool(llm, "groq")
    seen = []

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        seen.append((provider, model))
        assert provider != "groq"
        return _picks("MSFT")

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    result = llm._tiered_research("prompt")
    assert result["research_status"] == "ok"
    assert result["research_reason"] != "no_models_available"
    assert result["selected"][0]["ticker"] == "MSFT"
    assert seen
    assert all(provider != "groq" for provider, _model in seen)
    assert "groq_qwen" not in result["tiered_models_used"]


def test_in_walk_429_does_not_skip_a_different_ready_provider(isolated_llm, monkeypatch):
    llm = isolated_llm
    _enable(llm, "groq", "gemini")
    monkeypatch.setenv("LLM_TIERED_RESEARCH_MODELS", "groq_qwen,gemini_flash")
    monkeypatch.setenv("LLM_RATE_LIMIT_COOLDOWN_MINUTES", "120")
    seen = []

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        seen.append(provider)
        if provider == "groq":
            raise RuntimeError("groq HTTP 429: rate limit exceeded")
        return _picks()

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    result = llm._tiered_research("prompt")
    assert seen == ["groq", "gemini"]
    assert result["research_status"] == "ok"
    assert result["selected"][0]["ticker"] == "AAPL"
    assert llm._PROVIDER_COOLDOWN_UNTIL["groq"] - time.time() > 110 * 60
    assert llm._provider_cooldown_remaining("gemini") == 0


def test_all_configured_providers_cooling_is_no_models_available(isolated_llm, monkeypatch):
    llm = isolated_llm
    _enable(llm, "groq", "gemini", "mistral")
    _cool(llm, "groq", "gemini", "mistral")
    monkeypatch.setenv("NVIDIA_API_KEY", "<SET_NVIDIA_API_KEY>")
    calls = []
    monkeypatch.setattr(llm, "_call_provider", lambda *a, **k: calls.append(a) or _picks())
    result = llm._tiered_research("prompt")
    assert calls == []
    assert result["research_status"] == "failed"
    assert result["research_reason"] == "no_models_available"
    assert result["selected"] == []
    assert llm._tiered_choices("research", "prompt") == []


def test_openrouter_key_is_used_when_paper_tiers_are_cooling(isolated_llm, monkeypatch):
    llm = isolated_llm
    _enable(llm, "groq", "gemini", "mistral")
    _cool(llm, "groq", "gemini", "mistral")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
    seen = []

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        seen.append((provider, model, agent_tag))
        return _picks("NVDA")

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    choices = llm._tiered_choices("research", "prompt")
    assert [alias for alias, _provider, _model in choices] == ["openrouter_free"]
    result = llm._tiered_research("prompt")
    assert result["research_status"] == "ok"
    assert result["selected"][0]["ticker"] == "NVDA"
    assert seen == [("openrouter", "openrouter/free", "research_openrouter_free")]
    assert "groq" not in {provider for provider, _model, _tag in seen}


def test_openrouter_http_wiring_uses_env_base_model_and_key(isolated_llm, monkeypatch):
    llm = isolated_llm
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setenv("OPENROUTER_MODEL", "meta-llama/llama-3.2-3b-instruct:free")
    captured = {}

    class Resp:
        status_code = 200

        def __init__(self):
            body = {
                "choices": [{"message": {"content": _picks("IBM")}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
            self.text = json.dumps(body)

        def json(self):
            return json.loads(self.text)

    def fake_post(url, api_key, payload):
        captured["url"] = url
        captured["api_key"] = api_key
        captured["payload"] = payload
        return Resp()

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    text = llm._call_provider("openrouter", llm._openrouter_model(), "prompt", agent_tag="research_openrouter_free", mark_success=False)
    assert captured["url"] == "https://openrouter.example/api/v1/chat/completions"
    assert captured["api_key"] == "sk-or-test"
    assert captured["payload"]["model"] == "meta-llama/llama-3.2-3b-instruct:free"
    assert "IBM" in text


def test_nvidia_failover_normalizes_base_url_and_is_tried_after_cooldowns(isolated_llm, monkeypatch):
    llm = isolated_llm
    _enable(llm, "groq", "gemini", "mistral")
    _cool(llm, "groq", "gemini", "mistral")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
    assert llm._chat_completions_url(llm._nvidia_base_url()) == "https://integrate.api.nvidia.com/v1/chat/completions"
    choices = llm._tiered_choices("research", "prompt")
    assert [alias for alias, provider, _model in choices] == ["nvidia_llama"]
    assert choices[0][1] == "nvidia"

    captured = {}

    class Resp:
        status_code = 200

        def __init__(self):
            body = {
                "choices": [{"message": {"content": _picks("AMD")}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
            self.text = json.dumps(body)

        def json(self):
            return json.loads(self.text)

    def fake_post(url, api_key, payload):
        captured["url"] = url
        captured["api_key"] = api_key
        captured["model"] = payload["model"]
        return Resp()

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    result = llm._tiered_research("prompt")
    assert result["research_status"] == "ok"
    assert result["selected"][0]["ticker"] == "AMD"
    assert captured["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert captured["api_key"] == "nvapi-test"
    assert captured["model"] == llm.NVIDIA_LLAMA


def test_failover_is_not_called_when_earlier_tiers_fill_the_fanout(isolated_llm, monkeypatch):
    llm = isolated_llm
    _enable(llm, "groq", "gemini", "mistral")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("LLM_TIERED_FANOUT", "2")
    seen = []

    def fake_call(provider, model, prompt, max_tokens=600, agent_tag="", mark_success=True):
        seen.append(provider)
        return _picks()

    monkeypatch.setattr(llm, "_call_provider", fake_call)
    result = llm._tiered_research("prompt")
    assert result["research_status"] == "ok"
    assert seen == ["groq", "gemini"]
    assert "openrouter" not in seen


def test_substring_rate_is_not_a_two_hour_provider_cooldown(isolated_llm):
    llm = isolated_llm
    err = RuntimeError("Please separate the JSON object from the prose")
    assert llm._is_rate_limited_error(err) is False
    assert llm._is_rate_limited_error(RuntimeError("HTTP 429: too many requests")) is True
    llm._classify_cooldown("groq", err, model="qwen/qwen3.8-27b")
    remaining = llm._provider_cooldown_remaining("groq")
    assert remaining < 10 * 60
    saved = json.load(open(llm.LLM_HEALTH_FILE, encoding="utf-8"))
    assert saved["groq"]["last_error"] == "transient LLM error"
    assert "rate-limit" not in saved["groq"]["last_error"]
