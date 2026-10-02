"""
llm_router.py — Multi-Provider LLM Router
=========================================
Centralises all AI model calls so agent.py never imports provider SDKs directly.

Supported single-provider modes:
  claude_haiku | claude_sonnet | claude_opus
  gpt4o_mini | gpt4o
  gemini_flash | gemini_pro
  mistral_small | mistral_large
  deepseek_chat | deepseek_reasoner   ("deepspeak_*" aliases also accepted)
  groq_llama | groq_qwen
  nvidia_llama | nvidia_qwen | nvidia_deepseek

Supported strategy modes:
  tiered   — cheap model for research/screening; stronger model for analysis
  dual     — Claude + OpenAI both evaluate; BUY only if both agree
  economy  — cheapest configured provider

Environment keys:
  ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, MISTRAL_API_KEY,
  DEEPSEEK_API_KEY, GROQ_API_KEY, NVIDIA_API_KEY
"""

import os
import json
import logging
import time
import hashlib
import re
from datetime import datetime, date
from typing import Optional, Iterable

import requests

# Load .env/config.json before reading provider keys. This matters because agent.py imports
# llm_router before it copies config.json values into os.environ.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass
try:
    from config_server import load_config, apply_config_to_env
    apply_config_to_env()
except Exception:
    pass

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
def _configured_llm_mode() -> str:
    """Read LLM_MODE fresh from config.json (UI source of truth)."""
    try:
        from config_server import load_config
        mode = load_config().get("LLM_MODE", "")
        if mode:
            return str(mode).lower()
    except Exception:
        pass
    return (os.getenv("LLM_MODE") or "tiered").lower()

LLM_MODE            = _configured_llm_mode()
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
MISTRAL_API_KEY     = os.getenv("MISTRAL_API_KEY", "")
DEEPSEEK_API_KEY    = os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("DEEPSPEAK_API_KEY", "")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
NVIDIA_API_KEY      = os.getenv("NVIDIA_API_KEY", "") or os.getenv("NIM_API_KEY", "")
NVIDIA_BASE_URL     = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
DAILY_TOKEN_BUDGET  = int(os.getenv("DAILY_TOKEN_BUDGET", "200000"))
TOKEN_USAGE_FILE    = "token_usage.json"
LLM_HEALTH_FILE     = "llm_health.json"
LLM_AUTO_ADAPT      = os.getenv("LLM_AUTO_ADAPT", "1").lower() not in ("0", "false", "no")

# Model identifiers. Override with env vars if a provider changes aliases.
CLAUDE_HAIKU  = os.getenv("CLAUDE_HAIKU_MODEL",  "claude-3-5-haiku-latest")
CLAUDE_SONNET = os.getenv("CLAUDE_SONNET_MODEL", "claude-sonnet-4-5")
CLAUDE_OPUS   = os.getenv("CLAUDE_OPUS_MODEL",   "claude-opus-4-1")
GPT4O_MINI    = os.getenv("OPENAI_MINI_MODEL",   "gpt-4o-mini")
GPT4O         = os.getenv("OPENAI_FULL_MODEL",   "gpt-4o")
GEMINI_FLASH  = os.getenv("GEMINI_FLASH_MODEL",  "gemini-2.5-flash")
GEMINI_PRO    = os.getenv("GEMINI_PRO_MODEL",    "gemini-2.5-pro")
MISTRAL_SMALL = os.getenv("MISTRAL_SMALL_MODEL", "mistral-small-latest")
MISTRAL_LARGE = os.getenv("MISTRAL_LARGE_MODEL", "mistral-small-latest")  # safer default; override if paid tier supports large
DEEPSEEK_CHAT = os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-chat")
DEEPSEEK_REASONER = os.getenv("DEEPSEEK_REASONER_MODEL", "deepseek-reasoner")
GROQ_LLAMA    = os.getenv("GROQ_LLAMA_MODEL",    "openai/gpt-oss-120b")
GROQ_QWEN     = os.getenv("GROQ_QWEN_MODEL",     "qwen/qwen3.8-27b")  # JSON-object capable Groq default
NVIDIA_LLAMA  = os.getenv("NVIDIA_LLAMA_MODEL",  "meta/llama-3.1-70b-instruct")
NVIDIA_QWEN   = os.getenv("NVIDIA_QWEN_MODEL",   "qwen/qwen3-235b-a22b")
NVIDIA_DEEPSEEK = os.getenv("NVIDIA_DEEPSEEK_MODEL", "deepseek-ai/deepseek-r1")

PROVIDER_KEYS = {
    "claude": bool(ANTHROPIC_API_KEY),
    "openai": bool(OPENAI_API_KEY),
    "gemini": bool(GEMINI_API_KEY),
    "mistral": bool(MISTRAL_API_KEY),
    "deepseek": bool(DEEPSEEK_API_KEY),
    "groq": bool(GROQ_API_KEY),
    "nvidia": bool(NVIDIA_API_KEY),
}

# Runtime cooldowns keep one bad provider/key/rate-limit from crashing a whole cycle.
_PROVIDER_COOLDOWN_UNTIL: dict[str, float] = {}

def _load_health() -> dict:
    try:
        with open(LLM_HEALTH_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_health(data: dict):
    try:
        with open(LLM_HEALTH_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"[LLM] Could not save provider health: {e}")

def _health(provider: str) -> dict:
    data = _load_health()
    rec = data.setdefault(provider, {"success": 0, "fail": 0, "cooldown_until": 0, "last_error": "", "last_ok": ""})
    return rec

def _provider_ready(provider: str) -> bool:
    if not PROVIDER_KEYS.get(provider, False):
        return False
    now = time.time()
    mem_cd = _PROVIDER_COOLDOWN_UNTIL.get(provider, 0)
    disk_cd = float(_health(provider).get("cooldown_until", 0) or 0)
    return now >= max(mem_cd, disk_cd)

def _score_provider(provider: str, phase: str, input_tokens: int = 0) -> float:
    # Higher is better. Auto-adapts using recent success/fail counts and prompt size.
    base = {
        "research": {"groq": 92, "nvidia": 90, "gemini": 88, "mistral": 72, "deepseek": 70, "openai": 65, "claude": 60},
        "analysis": {"gemini": 92, "nvidia": 88, "groq": 84, "mistral": 74, "deepseek": 72, "openai": 68, "claude": 62},
    }.get(phase, {}).get(provider, 50)
    rec = _health(provider)
    success = int(rec.get("success", 0) or 0)
    fail = int(rec.get("fail", 0) or 0)
    reliability = min(15, success * 2) - min(45, fail * 8)
    # Penalize providers that have small free-tier TPM limits when the prompt is too large.
    size_penalty = 0
    if provider == "groq" and input_tokens > 4500: size_penalty = 30
    if provider == "mistral" and input_tokens > 6000: size_penalty = 18
    if provider == "nvidia" and input_tokens > 8000: size_penalty = 12
    if provider == "gemini" and input_tokens > 10000: size_penalty = 8
    return base + reliability - size_penalty

def _ready_models(candidates: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    ready = []
    for provider, model in candidates:
        if not _provider_ready(provider):
            continue
        if _model_is_suppressed(provider, model):
            _log_suppressed_skip(provider, model)
            continue
        ready.append((provider, model))
    return ready


def _provider_sorted(candidates: Iterable[tuple[str, str]], phase: str, prompt: str = "") -> list[tuple[str, str]]:
    input_tokens = _estimate_tokens(prompt)
    ready = _ready_models(candidates)
    if not LLM_AUTO_ADAPT:
        return ready
    return sorted(ready, key=lambda pm: _score_provider(pm[0], phase, input_tokens), reverse=True)

def _mark_provider_success(provider: str):
    data = _load_health()
    rec = data.setdefault(provider, {"success": 0, "fail": 0, "cooldown_until": 0, "last_error": "", "last_ok": ""})
    rec["success"] = int(rec.get("success", 0) or 0) + 1
    rec["fail"] = max(0, int(rec.get("fail", 0) or 0) - 1)
    rec["cooldown_until"] = 0
    rec["last_ok"] = datetime.now().isoformat(timespec="seconds")
    _save_health(data)

def _cooldown_provider(provider: str, seconds: int, reason: str = ""):
    until = time.time() + seconds
    _PROVIDER_COOLDOWN_UNTIL[provider] = until
    data = _load_health()
    rec = data.setdefault(provider, {"success": 0, "fail": 0, "cooldown_until": 0, "last_error": "", "last_ok": ""})
    rec["fail"] = int(rec.get("fail", 0) or 0) + 1
    rec["cooldown_until"] = until
    rec["last_error"] = reason
    _save_health(data)
    log.warning(f"[LLM] Cooling down {provider} for {seconds}s. {reason}")

# Per-model suppression for dead model ids (404 / model_not_found). Hours, not minutes,
# so a known-dead Groq model is not retried on every cron cycle.
_MODEL_SUPPRESS_UNTIL: dict[str, float] = {}
_MODELS_HEALTH_KEY = "_models"
DEFAULT_MODEL_NOT_FOUND_SUPPRESS_HOURS = 24


def model_not_found_suppress_seconds() -> int:
    raw = os.getenv("LLM_MODEL_NOT_FOUND_SUPPRESS_HOURS", str(DEFAULT_MODEL_NOT_FOUND_SUPPRESS_HOURS))
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        hours = float(DEFAULT_MODEL_NOT_FOUND_SUPPRESS_HOURS)
    if hours < 1:
        hours = float(DEFAULT_MODEL_NOT_FOUND_SUPPRESS_HOURS)
    return int(hours * 3600)


def rate_limit_cooldown_seconds() -> int:
    """How long a 429 backs off a provider before the next tier is worth retrying."""
    raw = os.getenv("LLM_RATE_LIMIT_COOLDOWN_MINUTES", "120")
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = 120.0
    if minutes < 1:
        minutes = 120.0
    return int(minutes * 60)


def _model_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _load_model_suppressions() -> dict:
    models = _load_health().get(_MODELS_HEALTH_KEY)
    return models if isinstance(models, dict) else {}


def _suppressed_until(provider: str, model: str) -> float:
    key = _model_key(provider, model)
    mem = float(_MODEL_SUPPRESS_UNTIL.get(key, 0) or 0)
    disk = float((_load_model_suppressions().get(key) or {}).get("suppress_until") or 0)
    return max(mem, disk)


def _model_is_suppressed(provider: str, model: str) -> bool:
    if not model:
        return False
    return _suppressed_until(provider, model) > time.time()


def _log_suppressed_skip(provider: str, model: str) -> None:
    until = _suppressed_until(provider, model)
    rec = _load_model_suppressions().get(_model_key(provider, model)) or {}
    when = datetime.fromtimestamp(until).isoformat(timespec="seconds") if until else "?"
    log.info(
        "[LLM] Skipping suppressed model %s/%s until %s. %s",
        provider,
        model,
        when,
        rec.get("reason") or "model-not-found",
    )


def _suppress_model(provider: str, model: str, seconds: int, reason: str) -> None:
    key = _model_key(provider, model)
    until = time.time() + max(1, int(seconds))
    _MODEL_SUPPRESS_UNTIL[key] = until
    data = _load_health()
    models = data.setdefault(_MODELS_HEALTH_KEY, {})
    if not isinstance(models, dict):
        models = {}
        data[_MODELS_HEALTH_KEY] = models
    models[key] = {
        "provider": provider,
        "model": model,
        "suppress_until": until,
        "reason": reason,
        "suppressed_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_health(data)
    log.warning(
        "[LLM] Suppressing model %s/%s for %.1fh until %s. %s",
        provider,
        model,
        seconds / 3600,
        datetime.fromtimestamp(until).isoformat(timespec="seconds"),
        reason,
    )


def _is_model_not_found_error(err: Exception) -> bool:
    msg = str(err).lower()
    if "model_not_found" in msg or "model not found" in msg:
        return True
    if "does not exist" in msg and "model" in msg:
        return True
    if "unknown model" in msg or "invalid model" in msg:
        return True
    if "http 404" in msg or " 404:" in msg or "status 404" in msg:
        return True
    return False


def _is_rate_limited_error(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        "429" in msg
        or "too many requests" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or "rate" in msg
    )


def _classify_cooldown(provider: str, err: Exception, model: str = ""):
    if model and _is_model_not_found_error(err):
        _suppress_model(provider, model, model_not_found_suppress_seconds(), "model-not-found")
        return
    msg = str(err).lower()
    if "401" in msg or "authentication" in msg or "invalid x-api-key" in msg or "invalid api key" in msg:
        _cooldown_provider(provider, 24*3600, "auth/key error")
    elif "insufficient balance" in msg or "payment" in msg or "billing" in msg:
        _cooldown_provider(provider, 24*3600, "billing/balance error")
    elif _is_rate_limited_error(err):
        seconds = rate_limit_cooldown_seconds()
        _cooldown_provider(provider, seconds, "rate-limit (429)")
        log.warning(
            "[LLM] %s rate-limited — backing off %.0f min and skipping to the next tier.",
            provider,
            seconds / 60,
        )
    elif "capacity" in msg or "tokens per minute" in msg or "request too large" in msg:
        _cooldown_provider(provider, 20*60, "rate/capacity/token-limit error")
    elif "503" in msg or "unavailable" in msg or "high demand" in msg:
        _cooldown_provider(provider, 7*60, "temporary provider outage")
    else:
        _cooldown_provider(provider, 3*60, "transient LLM error")

def _compact_prompt(prompt: str, provider: str, phase: str) -> str:
    # Keep prompts small enough for free/low-tier TPM limits. Preserve beginning instructions and ending JSON schema.
    phase = phase or "research"
    caps = {
        "research": {"groq": 5200, "mistral": 6500, "nvidia": 8000, "gemini": 9000, "deepseek": 6500, "openai": 9000, "claude": 9000},
        "analysis": {"groq": 7000, "mistral": 8500, "nvidia": 10000, "gemini": 12000, "deepseek": 8500, "openai": 12000, "claude": 12000},
    }
    cap = caps.get(phase, caps["research"]).get(provider, 7000)
    if len(prompt) <= cap:
        return prompt
    head = prompt[: int(cap * 0.28)]
    tail = prompt[-int(cap * 0.62):]
    return head + "\n\n[...auto-adaptive compression: removed lower-priority market/news rows to fit this provider...]\n\n" + tail

def _json_system_prefix(phase: str) -> str:
    return "Respond with compact valid JSON only. No markdown, no prose. "

def _max_tokens_for(provider: str, phase: str) -> int:
    # Small responses reduce cost and reduce free-tier rate-limit failures.
    if phase == "research":
        return {"groq": 140, "mistral": 160, "nvidia": 180, "gemini": 220, "deepseek": 180, "openai": 220, "claude": 220}.get(provider, 180)
    return {"groq": 120, "mistral": 140, "nvidia": 160, "gemini": 180, "deepseek": 160, "openai": 180, "claude": 180}.get(provider, 160)

# Approximate blended cost per 1k tokens. Update as your actual usage dictates.
def _key_available(provider: str) -> bool:
    return bool({"anthropic":ANTHROPIC_API_KEY,"openai":OPENAI_API_KEY,
                 "gemini":GEMINI_API_KEY,"groq":GROQ_API_KEY,"nvidia":NVIDIA_API_KEY,
                 "mistral":MISTRAL_API_KEY,"deepseek":DEEPSEEK_API_KEY}.get(provider,""))

COST_PER_1K = {
    CLAUDE_HAIKU:  0.00120,
    CLAUDE_SONNET: 0.00900,
    CLAUDE_OPUS:   0.04500,
    GPT4O_MINI:    0.00038,
    GPT4O:         0.00625,
    GEMINI_FLASH:  0.00070,
    GEMINI_PRO:    0.00450,
    MISTRAL_SMALL: 0.00040,
    MISTRAL_LARGE: 0.00400,
    DEEPSEEK_CHAT: 0.00070,
    DEEPSEEK_REASONER: 0.00250,
    GROQ_LLAMA:    0.00060,
    GROQ_QWEN:     0.00040,
    NVIDIA_LLAMA:  0.00060,
    NVIDIA_QWEN:   0.00060,
    NVIDIA_DEEPSEEK: 0.00060,
}

_anthropic_client = None
_openai_client = None


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def _openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


# ── Token budget tracker ──────────────────────────────────────────────────────
def _fresh_usage() -> dict:
    return {"date": str(date.today()), "total_tokens": 0, "total_cost_usd": 0.0,
            "calls": 0, "by_model": {}, "by_agent": {}, "calls_log": []}


def _load_usage() -> dict:
    try:
        with open(TOKEN_USAGE_FILE) as f:
            data = json.load(f)
        return data if data.get("date") == str(date.today()) else _fresh_usage()
    except Exception:
        return _fresh_usage()


def _save_usage(data: dict):
    try:
        with open(TOKEN_USAGE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"[LLM] Could not save token usage: {e}")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _record_usage(model: str, input_tokens: int, output_tokens: int, agent_tag: str = ""):
    usage = _load_usage()
    total = input_tokens + output_tokens
    cost = total / 1000 * COST_PER_1K.get(model, 0.001)
    usage["total_tokens"] += total
    usage["total_cost_usd"] += cost
    usage["calls"] += 1
    usage["by_model"].setdefault(model, {"tokens": 0, "cost": 0.0, "calls": 0})
    usage["by_model"][model]["tokens"] += total
    usage["by_model"][model]["cost"] += cost
    usage["by_model"][model]["calls"] += 1
    if agent_tag:
        usage["by_agent"].setdefault(agent_tag, {"tokens": 0, "cost": 0.0})
        usage["by_agent"][agent_tag]["tokens"] += total
        usage["by_agent"][agent_tag]["cost"] += cost
    usage["calls_log"].append({"time": datetime.now().strftime("%H:%M:%S"), "model": model,
                               "tokens": total, "cost": round(cost, 5), "agent": agent_tag})
    usage["calls_log"] = usage["calls_log"][-100:]
    _save_usage(usage)
    log.info(f"[LLM] {model} | {total} tokens | ${cost:.4f} | daily: {usage['total_tokens']} / {DAILY_TOKEN_BUDGET}")
    return usage


def budget_pct() -> float:
    return _load_usage().get("total_tokens", 0) / max(DAILY_TOKEN_BUDGET, 1)


def budget_exhausted() -> bool:
    return budget_pct() >= 1.0



# ── Proactive tiered model selection ──────────────────────────────────────────
MODEL_ALIASES = {
    "claude_haiku": ("claude", CLAUDE_HAIKU),
    "claude_sonnet": ("claude", CLAUDE_SONNET),
    "claude_opus": ("claude", CLAUDE_OPUS),
    "gpt4o_mini": ("openai", GPT4O_MINI),
    "gpt4o": ("openai", GPT4O),
    "gemini_flash": ("gemini", GEMINI_FLASH),
    "gemini_pro": ("gemini", GEMINI_PRO),
    "mistral_small": ("mistral", MISTRAL_SMALL),
    "mistral_large": ("mistral", MISTRAL_LARGE),
    "deepseek_chat": ("deepseek", DEEPSEEK_CHAT),
    "deepseek_reasoner": ("deepseek", DEEPSEEK_REASONER),
    "deepspeak_chat": ("deepseek", DEEPSEEK_CHAT),
    "deepspeak_reasoner": ("deepseek", DEEPSEEK_REASONER),
    "groq_llama": ("groq", GROQ_LLAMA),
    "groq_qwen": ("groq", GROQ_QWEN),
    "nvidia_llama": ("nvidia", NVIDIA_LLAMA),
    "nvidia_qwen": ("nvidia", NVIDIA_QWEN),
    "nvidia_deepseek": ("nvidia", NVIDIA_DEEPSEEK),
    "nim_llama": ("nvidia", NVIDIA_LLAMA),
    "nim_qwen": ("nvidia", NVIDIA_QWEN),
    "nim_deepseek": ("nvidia", NVIDIA_DEEPSEEK),
}

DEFAULT_TIERED_RESEARCH = "groq_qwen,nvidia_llama,gemini_flash,mistral_small,deepseek_chat,gpt4o_mini,claude_haiku"
DEFAULT_TIERED_ANALYSIS = "gemini_flash,nvidia_llama,groq_llama,mistral_small,deepseek_chat,gpt4o_mini,claude_haiku"


def _tiered_aliases(phase: str) -> list[str]:
    env_name = "LLM_TIERED_RESEARCH_MODELS" if phase == "research" else "LLM_TIERED_ANALYSIS_MODELS"
    default = DEFAULT_TIERED_RESEARCH if phase == "research" else DEFAULT_TIERED_ANALYSIS
    raw = os.getenv(env_name, default)
    aliases = [x.strip().lower() for x in raw.split(",") if x.strip()]
    # Keep only known aliases, preserving user order.
    return [a for a in aliases if a in MODEL_ALIASES]


def _tiered_fanout(phase: str) -> int:
    """Stop after this many schema-valid parses. Failures do not consume a slot."""
    if phase == "research":
        raw = (
            os.getenv("LLM_TIERED_VALID_PARSES")
            or os.getenv("LLM_TIERED_RESEARCH_FANOUT")
            or os.getenv("LLM_TIERED_FANOUT", "3")
        )
    else:
        raw = os.getenv("LLM_TIERED_ANALYSIS_FANOUT") or os.getenv("LLM_TIERED_FANOUT", "3")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 3


def _tiered_choices(phase: str, prompt: str) -> list[tuple[str, str, str]]:
    """Return every configured, healthy, non-suppressed tiered participant in try order.

    Research walks this full list until it has N valid parses. The fanout cap is
    applied by the caller, not by dropping later models before they are needed.
    """
    candidates = []
    for alias in _tiered_aliases(phase):
        provider, model = MODEL_ALIASES[alias]
        if not _provider_ready(provider):
            continue
        if _model_is_suppressed(provider, model):
            _log_suppressed_skip(provider, model)
            continue
        candidates.append((alias, provider, model))
    if LLM_AUTO_ADAPT:
        candidates = sorted(candidates, key=lambda apm: _score_provider(apm[1], phase, _estimate_tokens(prompt)), reverse=True)
    return candidates


def _is_proactive_tiered(mode: str) -> bool:
    return mode in ("tiered", "adaptive", "auto") and os.getenv("LLM_TIERED_PROACTIVE", "1").lower() not in ("0", "false", "no")


# Appended once when a tiered research model returns parseable JSON with an empty
# selected list (or salvage finds nothing). One retry, then move on.
_STRICT_RESEARCH_JSON_INSTRUCTION = (
    "JSON only. No prose and no markdown. "
    "selected must be a non-empty array of objects with ticker, reason, and confidence."
)


def _strict_research_prompt(prompt: str) -> str:
    return (prompt or "").rstrip() + "\n\n" + _STRICT_RESEARCH_JSON_INSTRUCTION


def _selected_picks(parsed) -> list:
    if not isinstance(parsed, dict):
        return []
    selected = parsed.get("selected")
    return selected if isinstance(selected, list) else []


def _research_payload_is_valid(parsed) -> bool:
    """Schema-valid research object. Empty ``selected`` is valid; ``{}`` and prose are not.

    Preamble-only and fence-only bodies never reach this as a dict with ``selected``.
    The salvage path counts only when it yields a ``selected`` list.
    """
    return isinstance(parsed, dict) and isinstance(parsed.get("selected"), list)


def _failed_research(reason: str, **extra) -> dict:
    out = {
        "selected": [],
        "research_status": "failed",
        "research_reason": reason,
    }
    out.update(extra)
    return out


def _annotate_research_status(parsed: dict, reason: str = "") -> dict:
    selected = parsed.get("selected") if isinstance(parsed.get("selected"), list) else []
    out = dict(parsed)
    out["selected"] = selected
    out["research_status"] = "ok" if selected else "empty"
    if reason:
        out["research_reason"] = reason
    elif not selected:
        out.setdefault("research_reason", "valid_empty_selected")
    return out


def _merge_research_picks(merged: dict, picks: list, alias: str) -> None:
    for pick in picks or []:
        if not isinstance(pick, dict):
            continue
        ticker = str(pick.get("ticker", "")).upper().strip()
        if not ticker:
            continue
        rec = merged.setdefault(ticker, {"ticker": ticker, "votes": 0, "confidence_sum": 0.0, "sources": [], "reasons": []})
        rec["votes"] += 1
        rec["confidence_sum"] += float(pick.get("confidence", 0.5) or 0.5)
        rec["sources"].append(alias)
        reason = str(pick.get("reason", "")).strip()
        if reason:
            rec["reasons"].append(f"{alias}: {reason}")


def _research_result_from_merge(merged: dict, successful: list, reason: str = "") -> dict:
    if not successful:
        return _failed_research(reason or "research_failed", tiered_models_used=[])
    if not merged:
        return {
            "selected": [],
            "research_status": "empty",
            "research_reason": reason or "valid_empty_selected",
            "tiered_models_used": successful,
        }
    selected = []
    for rec in merged.values():
        avg_conf = rec["confidence_sum"] / max(1, rec["votes"])
        conf = min(0.99, avg_conf + (0.06 * (rec["votes"] - 1)))
        selected.append({
            "ticker": rec["ticker"],
            "reason": " | ".join(rec["reasons"][:3]) or f"Selected by {', '.join(rec['sources'])}",
            "confidence": round(conf, 3),
            "tiered_votes": rec["votes"],
            "tiered_sources": rec["sources"],
            "dual_agree": rec["votes"] >= 2,
        })
    selected.sort(key=lambda x: (x.get("tiered_votes", 0), x.get("confidence", 0)), reverse=True)
    log.info("[LLM/TieredResearch] Used %s; merged picks: %s", successful, [x["ticker"] for x in selected[:5]])
    return {
        "selected": selected[:5],
        "tiered_models_used": successful,
        "research_status": "ok",
        "research_reason": reason,
    }


def _tiered_research_once(provider: str, model: str, prompt: str, agent_tag: str):
    """Call one model. Returns ``(parsed, error)``. Success is not recorded here."""
    try:
        text = _call_provider(
            provider, model, prompt,
            max_tokens=_max_tokens_for(provider, "research"),
            agent_tag=agent_tag,
            mark_success=False,
        )
    except Exception as exc:
        return None, exc
    return _parse_json(text), None


def _tiered_research(prompt: str, agent_tag: str = "research") -> dict:
    """Tiered research: keep walking providers until N valid parses, or the list ends.

    HTTP 200 with prose, a fence, or an empty object is not a success and does not
    stop the walk. A schema-valid ``selected: []`` is one valid parse (empty, not
    failed) after a single strict retry on that same model.
    """
    choices = _tiered_choices("research", prompt)
    if not choices:
        log.error("[LLM/TieredResearch] No configured/healthy tiered research models available.")
        return _failed_research("no_models_available", tiered_models_used=[])

    target = _tiered_fanout("research")
    merged: dict[str, dict] = {}
    successful = []
    failure_kinds = []
    cooled_providers: set[str] = set()

    for alias, provider, model in choices:
        if len(successful) >= target:
            break
        if provider in cooled_providers:
            log.info("[LLM/TieredResearch] Skipping %s — provider %s is backing off.", alias, provider)
            continue
        if _model_is_suppressed(provider, model):
            _log_suppressed_skip(provider, model)
            continue

        parsed, err = _tiered_research_once(provider, model, prompt, f"{agent_tag}_{alias}")
        if err is not None:
            log.warning("[LLM/TieredResearch] %s (%s/%s) failed: %s", alias, provider, model, err)
            _classify_cooldown(provider, err, model=model)
            if _is_rate_limited_error(err):
                cooled_providers.add(provider)
                failure_kinds.append("rate_limited")
                log.warning(
                    "[LLM/TieredResearch] %s returned 429 — backing off %s and trying the next tier.",
                    alias, provider,
                )
            elif _is_model_not_found_error(err):
                failure_kinds.append("model_not_found")
            else:
                failure_kinds.append("error")
            continue

        if _research_payload_is_valid(parsed) and _selected_picks(parsed):
            _mark_provider_success(provider)
            successful.append(alias)
            _merge_research_picks(merged, _selected_picks(parsed), alias)
            continue

        if _research_payload_is_valid(parsed):
            log.warning("[LLM/TieredResearch] %s returned no selected picks — one strict JSON retry.", alias)
            parsed2, err2 = _tiered_research_once(
                provider, model, _strict_research_prompt(prompt), f"{agent_tag}_{alias}_strict",
            )
            if err2 is not None:
                log.warning("[LLM/TieredResearch] %s strict retry failed: %s", alias, err2)
                _classify_cooldown(provider, err2, model=model)
                if _is_rate_limited_error(err2):
                    cooled_providers.add(provider)
                    failure_kinds.append("rate_limited")
            elif _research_payload_is_valid(parsed2) and _selected_picks(parsed2):
                _mark_provider_success(provider)
                successful.append(alias)
                _merge_research_picks(merged, _selected_picks(parsed2), alias)
                continue
            # First body was a real empty selection. That is a valid parse, not a failure.
            _mark_provider_success(provider)
            successful.append(alias)
            log.warning("[LLM/TieredResearch] %s returned a valid empty selected list.", alias)
            continue

        log.warning(
            "[LLM/TieredResearch] %s returned an unusable body (prose, fence-only, or empty object) — trying next model.",
            alias,
        )
        failure_kinds.append("unusable")

    if successful:
        return _research_result_from_merge(merged, successful)

    if failure_kinds and all(kind == "rate_limited" for kind in failure_kinds):
        reason = "rate_limited"
    elif "unusable" in failure_kinds:
        reason = "invalid_parse"
    elif "model_not_found" in failure_kinds and not any(k in failure_kinds for k in ("error", "rate_limited", "unusable")):
        reason = "model_not_found"
    else:
        reason = "research_failed"
    log.error("[LLM/TieredResearch] Research failed (%s). Not recording an empty pick set.", reason)
    return _failed_research(reason, tiered_models_used=[], tiered_failures=failure_kinds)


_ANALYSIS_ACTIONS = {"BUY", "SELL", "HOLD", "SKIP"}

# One retry when a model answers but does not produce a decision. HTTP errors
# are not retried here; the walk moves to the next configured model.
_STRICT_ANALYSIS_JSON_INSTRUCTION = (
    "JSON only. No prose and no markdown. "
    "decision must be one of BUY, SELL, HOLD, or SKIP. Include confidence and rationale."
)


def _strict_analysis_prompt(prompt: str) -> str:
    return (prompt or "").rstrip() + "\n\n" + _STRICT_ANALYSIS_JSON_INSTRUCTION


def _analysis_action(parsed) -> str:
    if not isinstance(parsed, dict):
        return ""
    raw = parsed.get("action")
    if raw in (None, ""):
        raw = parsed.get("decision")
    return str(raw or "").upper().strip()


def _analysis_payload_is_valid(parsed) -> bool:
    """Schema-valid analysis object. Prose, ``{}``, and a research ``selected`` list are not."""
    return _analysis_action(parsed) in _ANALYSIS_ACTIONS


def _normalize_analysis_decision(parsed: dict) -> dict:
    out = dict(parsed)
    action = _analysis_action(out)
    out["action"] = action
    out["decision"] = action
    return out


def _failed_analysis(reason: str, **extra) -> dict:
    rationale = extra.pop("rationale", None) or (
        "All tiered analysis models failed or returned invalid JSON"
    )
    out = {
        "action": "SKIP",
        "decision": "SKIP",
        "shares": 0,
        "rationale": rationale,
        "tiered_status": "error",
        "analysis_status": "failed",
        "analysis_reason": reason,
    }
    out.update(extra)
    return out


def _analysis_failure_kind(err: Exception) -> str:
    if _is_rate_limited_error(err):
        return "rate_limited"
    if _is_model_not_found_error(err):
        return "model_not_found"
    return "error"


def _tiered_analysis_once(provider: str, model: str, prompt: str, agent_tag: str):
    """Call one model. Returns ``(parsed, error)``. Success is not recorded here."""
    try:
        text = _call_provider(
            provider, model, prompt,
            max_tokens=_max_tokens_for(provider, "analysis"),
            agent_tag=agent_tag,
            mark_success=False,
        )
    except Exception as exc:
        return None, exc
    return _parse_json(text), None


def _accept_analysis_vote(parsed, alias: str, provider: str, decisions: list, successful: list) -> bool:
    if not _analysis_payload_is_valid(parsed):
        return False
    _mark_provider_success(provider)
    row = _normalize_analysis_decision(parsed)
    row["tiered_source"] = alias
    decisions.append(row)
    successful.append(alias)
    return True


def _tiered_analysis(prompt: str, agent_tag: str = "analysis") -> Optional[dict]:
    """Tiered analysis: walk providers until N schema-valid decisions, then vote.

    HTTP 200 with prose, a fence, an empty object, or JSON that has no
    BUY/SELL/HOLD/SKIP is not a vote and does not stop the walk. One strict
    retry is attempted on that same model before the next tier. When nothing
    valid remains, the result is an analysis failure (``tiered_status=error``),
    not a model opinion to SKIP.
    """
    choices = _tiered_choices("analysis", prompt)
    if not choices:
        log.error("[LLM/TieredAnalysis] No configured/healthy tiered analysis models available.")
        return _failed_analysis(
            "no_models_available",
            rationale="No configured/healthy tiered analysis models available",
            tiered_models_used=[],
        )

    target = _tiered_fanout("analysis")
    decisions = []
    successful = []
    failure_kinds = []
    cooled_providers: set[str] = set()

    for alias, provider, model in choices:
        if len(successful) >= target:
            break
        if provider in cooled_providers:
            log.info("[LLM/TieredAnalysis] Skipping %s — provider %s is backing off.", alias, provider)
            continue
        if _model_is_suppressed(provider, model):
            _log_suppressed_skip(provider, model)
            continue

        parsed, err = _tiered_analysis_once(provider, model, prompt, f"{agent_tag}_{alias}")
        if err is not None:
            log.warning("[LLM/TieredAnalysis] %s (%s/%s) failed: %s", alias, provider, model, err)
            _classify_cooldown(provider, err, model=model)
            kind = _analysis_failure_kind(err)
            failure_kinds.append(kind)
            if kind == "rate_limited":
                cooled_providers.add(provider)
                log.warning(
                    "[LLM/TieredAnalysis] %s returned 429 — backing off %s and trying the next tier.",
                    alias, provider,
                )
            continue

        if _accept_analysis_vote(parsed, alias, provider, decisions, successful):
            continue

        log.warning(
            "[LLM/TieredAnalysis] %s returned an unusable body (prose, fence-only, empty object, or no decision) — one strict JSON retry.",
            alias,
        )
        parsed2, err2 = _tiered_analysis_once(
            provider, model, _strict_analysis_prompt(prompt), f"{agent_tag}_{alias}_strict",
        )
        if err2 is not None:
            log.warning("[LLM/TieredAnalysis] %s strict retry failed: %s", alias, err2)
            _classify_cooldown(provider, err2, model=model)
            kind = _analysis_failure_kind(err2)
            failure_kinds.append(kind)
            if kind == "rate_limited":
                cooled_providers.add(provider)
            continue
        if _accept_analysis_vote(parsed2, alias, provider, decisions, successful):
            continue
        failure_kinds.append("unusable")

    if not decisions:
        if failure_kinds and all(kind == "rate_limited" for kind in failure_kinds):
            reason = "rate_limited"
        elif failure_kinds and all(kind == "unusable" for kind in failure_kinds):
            reason = "invalid_parse"
        elif "model_not_found" in failure_kinds and not any(
            k in failure_kinds for k in ("error", "rate_limited", "unusable")
        ):
            reason = "model_not_found"
        else:
            reason = "analysis_failed"
        log.error("[LLM/TieredAnalysis] Analysis failed (%s). Not recording a SKIP vote.", reason)
        return _failed_analysis(reason, tiered_models_used=[], tiered_failures=failure_kinds)

    buy_decisions = [
        d for d in decisions
        if str(d.get("action") or d.get("decision", "SKIP")).upper() == "BUY"
    ]
    min_buy_votes = int(os.getenv("LLM_TIERED_MIN_BUY_VOTES", "1"))
    if len(successful) >= 2 and os.getenv("LLM_TIERED_REQUIRE_CONSENSUS", "0").lower() in ("1", "true", "yes"):
        min_buy_votes = max(min_buy_votes, 2)

    if len(buy_decisions) >= min_buy_votes:
        chosen = max(buy_decisions, key=lambda d: float(d.get("confidence", 0) or 0))
        chosen = dict(chosen)
        chosen["action"] = "BUY"
        chosen.pop("shares", None)
        chosen.pop("notional_usd", None)
        chosen.pop("qty", None)
        chosen["analysis_status"] = "ok"
        chosen["tiered_status"] = "buy_votes"
        chosen["tiered_buy_votes"] = len(buy_decisions)
        chosen["tiered_total_votes"] = len(decisions)
        chosen["tiered_models_used"] = successful
        rationale_parts = [
            f"{d.get('tiered_source')}: {d.get('action', d.get('decision', 'SKIP'))} - {d.get('rationale', '')}"
            for d in decisions
        ]
        chosen["rationale"] = "Tiered multi-model vote: " + " | ".join(rationale_parts)[:800]
        from agreement_engine import enrich_with_agreement
        return enrich_with_agreement(chosen, decisions)

    return {
        "action": "SKIP",
        "shares": 0,
        "rationale": "Tiered multi-model vote did not meet BUY threshold: " + " | ".join(
            f"{d.get('tiered_source')}: {d.get('action', 'SKIP')} - {d.get('rationale', '')}" for d in decisions
        )[:800],
        "analysis_status": "ok",
        "tiered_status": "no_buy_consensus",
        "tiered_buy_votes": len(buy_decisions),
        "tiered_total_votes": len(decisions),
        "tiered_models_used": successful,
    }

# ── Model selection ───────────────────────────────────────────────────────────
def _effective_mode() -> str:
    pct = budget_pct()
    if pct >= 1.0:
        log.warning("[LLM] Daily token budget EXHAUSTED — skipping AI calls.")
        return "budget_exhausted"
    if pct >= 0.80:
        log.warning(f"[LLM] Budget {pct*100:.0f}% used — auto-downgrading to economy.")
        return "economy"
    return _configured_llm_mode()


def _available(candidates: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return _ready_models(candidates)


def _pick_model(phase: str, mode: str) -> tuple[str, str] | None:
    """Return one (provider, model) for single-model modes."""
    alias = {
        "deepspeak_chat": "deepseek_chat",
        "deepspeak_reasoner": "deepseek_reasoner",
        "chatgpt_mini": "gpt4o_mini",
        "chatgpt": "gpt4o",
    }
    mode = alias.get(mode, mode)

    mapping = {
        "claude_haiku": ("claude", CLAUDE_HAIKU),
        "claude_sonnet": ("claude", CLAUDE_SONNET),
        "claude_opus": ("claude", CLAUDE_OPUS),
        "gpt4o_mini": ("openai", GPT4O_MINI),
        "gpt4o": ("openai", GPT4O),
        "gemini_flash": ("gemini", GEMINI_FLASH),
        "gemini_pro": ("gemini", GEMINI_PRO),
        "mistral_small": ("mistral", MISTRAL_SMALL),
        "mistral_large": ("mistral", MISTRAL_LARGE),
        "deepseek_chat": ("deepseek", DEEPSEEK_CHAT),
        "deepseek_reasoner": ("deepseek", DEEPSEEK_REASONER),
        "groq_llama": ("groq", GROQ_LLAMA),
        "groq_qwen": ("groq", GROQ_QWEN),
        "nvidia_llama": ("nvidia", NVIDIA_LLAMA),
        "nvidia_qwen": ("nvidia", NVIDIA_QWEN),
        "nvidia_deepseek": ("nvidia", NVIDIA_DEEPSEEK),
        "nim_llama": ("nvidia", NVIDIA_LLAMA),
        "nim_qwen": ("nvidia", NVIDIA_QWEN),
        "nim_deepseek": ("nvidia", NVIDIA_DEEPSEEK),
    }
    if mode in mapping:
        p, m = mapping[mode]
        if not PROVIDER_KEYS.get(p):
            raise RuntimeError(f"{p.upper()} API key not configured for LLM_MODE={mode}")
        return p, m

    if mode == "economy":
        choices = _provider_sorted([("groq", GROQ_QWEN), ("nvidia", NVIDIA_LLAMA), ("gemini", GEMINI_FLASH), ("mistral", MISTRAL_SMALL),
                                    ("openai", GPT4O_MINI), ("deepseek", DEEPSEEK_CHAT), ("claude", CLAUDE_HAIKU)], phase)
        return choices[0] if choices else None

    if mode in ("tiered", "adaptive", "auto"):
        # Auto-adaptive mode: use authenticated providers, score by recent health, and avoid weak providers for huge prompts.
        if phase == "research":
            choices = _provider_sorted([("groq", GROQ_QWEN), ("nvidia", NVIDIA_LLAMA), ("gemini", GEMINI_FLASH), ("mistral", MISTRAL_SMALL),
                                        ("deepseek", DEEPSEEK_CHAT), ("openai", GPT4O_MINI), ("claude", CLAUDE_HAIKU)], phase)
        else:
            choices = _provider_sorted([("gemini", GEMINI_FLASH), ("nvidia", NVIDIA_LLAMA), ("groq", GROQ_LLAMA), ("mistral", MISTRAL_SMALL),
                                        ("deepseek", DEEPSEEK_CHAT), ("openai", GPT4O_MINI), ("claude", CLAUDE_HAIKU)], phase)
        return choices[0] if choices else None

    # Unknown mode: safe fallback to first configured provider.
    choices = _available([("claude", CLAUDE_SONNET), ("openai", GPT4O_MINI), ("gemini", GEMINI_FLASH),
                          ("nvidia", NVIDIA_LLAMA), ("mistral", MISTRAL_SMALL), ("deepseek", DEEPSEEK_CHAT), ("groq", GROQ_QWEN)])
    return choices[0] if choices else None


# ── Provider call wrappers ────────────────────────────────────────────────────
def _call_provider(provider: str, model: str, prompt: str, max_tokens: int = 600, agent_tag: str = "",
                   mark_success: bool = True) -> str:
    phase = "analysis" if "analysis" in (agent_tag or "") else "research"
    prompt = _json_system_prefix(phase) + _compact_prompt(prompt, provider, phase)
    if max_tokens is None or max_tokens <= 0:
        max_tokens = _max_tokens_for(provider, phase)

    def _finish(out: str) -> str:
        # Research callers pass mark_success=False and record success only after a valid parse.
        if mark_success:
            _mark_provider_success(provider)
        return out

    if provider == "claude":
        return _finish(_call_claude(model, prompt, max_tokens, agent_tag))
    if provider == "openai":
        return _finish(_call_openai(model, prompt, max_tokens, agent_tag))
    if provider == "gemini":
        return _finish(_call_gemini(model, prompt, max_tokens, agent_tag))
    if provider == "mistral":
        return _finish(_call_chat_endpoint("mistral", model, prompt, max_tokens, agent_tag,
                                           "https://api.mistral.ai/v1/chat/completions", MISTRAL_API_KEY))
    if provider == "deepseek":
        return _finish(_call_chat_endpoint("deepseek", model, prompt, max_tokens, agent_tag,
                                           "https://api.deepseek.com/chat/completions", DEEPSEEK_API_KEY))
    if provider == "groq":
        return _finish(_call_chat_endpoint("groq", model, prompt, max_tokens, agent_tag,
                                           "https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY))
    if provider == "nvidia":
        return _finish(_call_chat_endpoint("nvidia", model, prompt, max_tokens, agent_tag,
                                           f"{NVIDIA_BASE_URL}/chat/completions", NVIDIA_API_KEY))
    raise RuntimeError(f"Unknown LLM provider: {provider}")


def _call_claude(model: str, prompt: str, max_tokens: int = 600, agent_tag: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    response = _anthropic().messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text
    _record_usage(model, response.usage.input_tokens, response.usage.output_tokens, agent_tag)
    return text


def _call_openai(model: str, prompt: str, max_tokens: int = 600, agent_tag: str = "") -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    response = _openai().chat.completions.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content or ""
    usage = response.usage
    _record_usage(model, getattr(usage, "prompt_tokens", _estimate_tokens(prompt)),
                  getattr(usage, "completion_tokens", _estimate_tokens(text)), agent_tag)
    return text


def _call_chat_endpoint(provider: str, model: str, prompt: str, max_tokens: int, agent_tag: str,
                        url: str, api_key: str) -> str:
    if not api_key:
        raise RuntimeError(f"{provider.upper()}_API_KEY not set")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                      json=payload, timeout=45)
    # A 429 is a backoff, not a prompt-format problem. Do not immediately POST again.
    if r.status_code == 429:
        raise RuntimeError(f"{provider} HTTP 429: {r.text[:300]}")
    if r.status_code >= 400 and "response_format" in payload and ("response_format" in r.text.lower() or "json_object" in r.text.lower()):
        payload.pop("response_format", None)
        r = requests.post(url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          json=payload, timeout=45)
    if r.status_code >= 400:
        raise RuntimeError(f"{provider} HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage") or {}
    _record_usage(model, int(usage.get("prompt_tokens", _estimate_tokens(prompt))),
                  int(usage.get("completion_tokens", _estimate_tokens(text))), agent_tag)
    return text


def _call_gemini(model: str, prompt: str, max_tokens: int = 600, agent_tag: str = "") -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
    }
    r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=45)
    if r.status_code >= 400:
        raise RuntimeError(f"gemini HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    text = ""
    candidates = data.get("candidates") or []
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
    usage = data.get("usageMetadata") or {}
    _record_usage(model, int(usage.get("promptTokenCount", _estimate_tokens(prompt))),
                  int(usage.get("candidatesTokenCount", _estimate_tokens(text))), agent_tag)
    return text


def _fallback_models(phase: str, exclude_provider: str = "") -> list[tuple[str, str]]:
    """All configured models for the phase, excluding the provider that just failed."""
    if phase == "research":
        pool = [("groq", GROQ_QWEN), ("nvidia", NVIDIA_LLAMA), ("mistral", MISTRAL_SMALL), ("gemini", GEMINI_FLASH),
                ("deepseek", DEEPSEEK_CHAT), ("openai", GPT4O_MINI), ("claude", CLAUDE_HAIKU)]
    else:
        pool = [("deepseek", DEEPSEEK_REASONER), ("gemini", GEMINI_PRO), ("nvidia", NVIDIA_LLAMA), ("mistral", MISTRAL_LARGE),
                ("groq", GROQ_LLAMA), ("openai", GPT4O), ("claude", CLAUDE_SONNET)]
    return [(p, m) for p, m in _provider_sorted(pool, phase) if p != exclude_provider]


# ── Public API ────────────────────────────────────────────────────────────────
def _accept_research_parse(provider: str, parsed):
    """Record provider success only after a schema-valid research object."""
    if not _research_payload_is_valid(parsed):
        return None
    _mark_provider_success(provider)
    return _annotate_research_status(parsed)


def query_research(prompt: str, agent_tag: str = "research") -> dict:
    mode = _effective_mode()
    if mode == "budget_exhausted":
        return _failed_research("budget_exhausted")
    if mode == "dual":
        return _dual_research(prompt, agent_tag)
    if _is_proactive_tiered(mode):
        return _tiered_research(prompt, agent_tag)
    try:
        picked = _pick_model("research", mode)
    except Exception as exc:
        log.error("[LLM] No research model available: %s", exc)
        return _failed_research("no_models_available")
    if not picked:
        return _failed_research("no_models_available")
    provider, model = picked

    def _try(prov: str, mod: str, tag: str):
        if _model_is_suppressed(prov, mod) or not _provider_ready(prov):
            if _model_is_suppressed(prov, mod):
                _log_suppressed_skip(prov, mod)
            else:
                log.info("[LLM] Skipping %s/%s — provider is backing off.", prov, mod)
            return None
        try:
            text = _call_provider(
                prov, mod, prompt,
                max_tokens=_max_tokens_for(prov, "research"),
                agent_tag=tag,
                mark_success=False,
            )
        except Exception as exc:
            log.warning("[LLM] Research model %s/%s failed: %s", prov, mod, exc)
            _classify_cooldown(prov, exc, model=mod)
            if _is_rate_limited_error(exc):
                log.warning("[LLM] %s rate-limited — backing off and trying the next tier.", prov)
            return None
        accepted = _accept_research_parse(prov, _parse_json(text))
        if accepted:
            return accepted
        log.warning(
            "[LLM] %s/%s returned an unusable research body (prose, fence-only, or empty object) — trying next tier.",
            prov, mod,
        )
        return None

    accepted = _try(provider, model, agent_tag)
    if accepted:
        return accepted
    for fp, fm in _fallback_models("research", provider):
        accepted = _try(fp, fm, f"{agent_tag}_{fp}_fallback")
        if accepted:
            return accepted
    log.error("[LLM] All research models failed; this is a research failure, not an empty pick set.")
    return _failed_research("research_failed")


_FALLBACK_CHAIN = [("anthropic",None),("openai",None),("mistral",None),("deepseek",None)]

def _call_with_fallback(prompt, max_tokens, agent_tag, primary_provider, primary_model):
    """Call primary model; on parse failure or error, retry with cheapest fallback."""
    try:
        text   = _call_provider(primary_provider, primary_model, prompt, max_tokens=max_tokens, agent_tag=agent_tag)
        result = _parse_json(text)
        if result is not None: return result
        log.warning(f"[LLM] {primary_model} returned unparseable output — trying fallback")
    except Exception as e:
        log.warning(f"[LLM] {primary_model} failed ({e}) — trying fallback")
    for fb_prov, _ in _FALLBACK_CHAIN:
        if fb_prov == primary_provider or not _key_available(fb_prov): continue
        try:
            fb_model = _pick_model("research", fb_prov)
            if not fb_model: continue
            prov2, mod2 = fb_model
            text = _call_provider(prov2, mod2, prompt, max_tokens=max_tokens, agent_tag=f"{agent_tag}_fb")
            result = _parse_json(text)
            if result is not None:
                log.info(f"[LLM] Fallback {mod2} succeeded")
                return result
        except Exception as e2:
            log.warning(f"[LLM] Fallback {fb_prov} also failed: {e2}")
    log.error(f"[LLM] All fallbacks failed for {agent_tag}")
    return None

def _try_analysis_model(provider: str, model: str, prompt: str, agent_tag: str):
    """Return a schema-valid decision, or None. Success is recorded only then."""
    if _model_is_suppressed(provider, model) or not _provider_ready(provider):
        if _model_is_suppressed(provider, model):
            _log_suppressed_skip(provider, model)
        else:
            log.info("[LLM] Skipping %s/%s — provider is backing off.", provider, model)
        return None
    parsed, err = _tiered_analysis_once(provider, model, prompt, agent_tag)
    if err is not None:
        log.warning("[LLM] Analysis model %s/%s failed: %s", provider, model, err)
        _classify_cooldown(provider, err, model=model)
        return None
    if _analysis_payload_is_valid(parsed):
        _mark_provider_success(provider)
        return _normalize_analysis_decision(parsed)
    log.warning(
        "[LLM] %s/%s returned an unusable analysis body — one strict JSON retry.",
        provider, model,
    )
    parsed2, err2 = _tiered_analysis_once(
        provider, model, _strict_analysis_prompt(prompt), f"{agent_tag}_strict",
    )
    if err2 is not None:
        log.warning("[LLM] %s/%s strict retry failed: %s", provider, model, err2)
        _classify_cooldown(provider, err2, model=model)
        return None
    if _analysis_payload_is_valid(parsed2):
        _mark_provider_success(provider)
        return _normalize_analysis_decision(parsed2)
    return None


def query_analysis(prompt: str, agent_tag: str = "analysis") -> Optional[dict]:
    mode = _effective_mode()
    if mode == "budget_exhausted":
        return None
    if mode == "dual":
        return _dual_analysis(prompt, agent_tag)
    if _is_proactive_tiered(mode):
        return _tiered_analysis(prompt, agent_tag)
    try:
        picked = _pick_model("analysis", mode)
    except Exception as exc:
        log.error("[LLM] No analysis model available: %s", exc)
        return _failed_analysis("no_models_available", rationale="No configured/healthy analysis models available")
    if not picked:
        log.error("[LLM] No API keys configured for any supported LLM provider.")
        return _failed_analysis("no_models_available", rationale="No configured/healthy analysis models available")
    provider, model = picked
    accepted = _try_analysis_model(provider, model, prompt, agent_tag)
    if accepted:
        return accepted
    for fp, fm in _fallback_models("analysis", provider):
        accepted = _try_analysis_model(fp, fm, prompt, f"{agent_tag}_{fp}_fallback")
        if accepted:
            return accepted
    log.error("[LLM] All analysis models failed; this is an analysis failure, not a SKIP vote.")
    return _failed_analysis(
        "analysis_failed",
        rationale="All configured LLM providers failed or are cooling down",
    )


# ── Dual-consult logic ────────────────────────────────────────────────────────
def _dual_pair() -> list[tuple[str, str]]:
    pair = _available([("claude", CLAUDE_SONNET), ("openai", GPT4O)])
    if len(pair) < 2:
        # If the original conservative pair is not configured, use the first two strong configured models.
        pair = _available([("gemini", GEMINI_PRO), ("nvidia", NVIDIA_LLAMA), ("mistral", MISTRAL_LARGE),
                           ("deepseek", DEEPSEEK_REASONER), ("groq", GROQ_LLAMA),
                           ("claude", CLAUDE_SONNET), ("openai", GPT4O)])[:2]
    return pair


def _dual_research(prompt: str, agent_tag: str) -> dict:
    results = {}
    valid = 0
    failure_kinds = []
    for provider, model in _dual_pair():
        if _model_is_suppressed(provider, model) or not _provider_ready(provider):
            if _model_is_suppressed(provider, model):
                _log_suppressed_skip(provider, model)
            continue
        try:
            text = _call_provider(
                provider, model, prompt, max_tokens=600,
                agent_tag=f"{agent_tag}_{provider}", mark_success=False,
            )
            parsed = _parse_json(text)
            if not _research_payload_is_valid(parsed):
                log.warning("[LLM/DualResearch] %s returned an unusable body — trying next model.", provider)
                failure_kinds.append("unusable")
                continue
            _mark_provider_success(provider)
            valid += 1
            for pick in parsed["selected"]:
                if not isinstance(pick, dict):
                    continue
                ticker = pick.get("ticker", "")
                if not ticker:
                    continue
                if ticker in results:
                    results[ticker]["confidence"] = (results[ticker]["confidence"] + pick.get("confidence", 0.5)) / 2
                    results[ticker]["reason"] += f" | {provider} agrees: {pick.get('reason','')}"
                    results[ticker]["dual_agree"] = True
                else:
                    results[ticker] = {"ticker": ticker, "reason": pick.get("reason", ""),
                                       "confidence": pick.get("confidence", 0.5),
                                       "source": provider, "dual_agree": False}
        except Exception as e:
            log.warning(f"[LLM/DualResearch] {provider} failed: {e}")
            _classify_cooldown(provider, e, model=model)
            failure_kinds.append("rate_limited" if _is_rate_limited_error(e) else "error")
    if valid == 0:
        if failure_kinds and all(kind == "rate_limited" for kind in failure_kinds):
            reason = "rate_limited"
        elif failure_kinds and all(kind == "unusable" for kind in failure_kinds):
            reason = "invalid_parse"
        else:
            reason = "research_failed"
        return _failed_research(reason)
    picks = sorted(results.values(), key=lambda x: (x["dual_agree"], x["confidence"]), reverse=True)
    return _annotate_research_status({"selected": picks[:5]})


def _dual_analysis(prompt: str, agent_tag: str) -> dict:
    decisions = {}
    for provider, model in _dual_pair():
        if _model_is_suppressed(provider, model) or not _provider_ready(provider):
            if _model_is_suppressed(provider, model):
                _log_suppressed_skip(provider, model)
            continue
        try:
            text = _call_provider(
                provider, model, prompt, max_tokens=400,
                agent_tag=f"{agent_tag}_{provider}", mark_success=False,
            )
        except Exception as e:
            log.warning(f"[LLM/DualAnalysis] {provider} failed: {e}")
            _classify_cooldown(provider, e, model=model)
            continue
        parsed = _parse_json(text)
        if not _analysis_payload_is_valid(parsed):
            log.warning("[LLM/DualAnalysis] %s returned an unusable body — trying next model.", provider)
            continue
        _mark_provider_success(provider)
        decisions[provider] = _normalize_analysis_decision(parsed)
    if not decisions:
        failed = _failed_analysis("analysis_failed", rationale="All models failed")
        failed["dual_status"] = "error"
        return failed
    if len(decisions) == 1:
        result = dict(list(decisions.values())[0])
        result.pop("shares", None)
        result.pop("notional_usd", None)
        result["dual_status"] = "single_response"
        return result
    providers = list(decisions.keys())
    actions = {
        p: str(decisions[p].get("action") or decisions[p].get("decision", "SKIP")).upper()
        for p in providers
    }
    if all(a == "BUY" for a in actions.values()):
        primary = providers[0]
        result = dict(decisions[primary])
        result.pop("shares", None)
        result.pop("notional_usd", None)
        result.pop("qty", None)
        result["action"] = "BUY"
        result["rationale"] = "DUAL AGREE ✓ | " + " | ".join(
            f"{p}: {decisions[p].get('rationale','')}" for p in providers)
        result["dual_status"] = "both_buy"
        from agreement_engine import enrich_with_agreement
        return enrich_with_agreement(result, list(decisions.values()))
    return {"action": "SKIP", "shares": 0,
            "rationale": "Dual conflict — " + " ".join(f"{p}:{a}" for p, a in actions.items()),
            "dual_status": "conflict", **{f"{p}_says": a for p, a in actions.items()}}


# ── JSON parser / repair ─────────────────────────────────────────────────────
# Gemini often prefixes research JSON with prose ("Here is the JSON requested")
# and/or a ```json fence. json.loads rejects that whole string, so bucket
# research returns no picks. Strip those wrappers before parsing.
_PREAMBLE_LINE = re.compile(
    r"^(?:sure[,!]?\s+|okay[,!]?\s+|ok[,!]?\s+)?"
    r"here(?:['’]s| is)(?: the| your)? json(?: requested)?\b",
    re.IGNORECASE,
)
_FENCE_BLOCK = re.compile(
    r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_FENCE_INLINE = re.compile(
    r"```[ \t]*(?:json)?[ \t]+(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_LLM_PAYLOAD_KEYS = ("selected", "action", "decision")


def _strip_code_fences(text: str) -> str:
    """Return the inside of a ``` / ```json fence, or text with edge fences removed."""
    clean = (text or "").strip()
    match = _FENCE_BLOCK.search(clean)
    if match:
        return match.group(1).strip()
    match = _FENCE_INLINE.search(clean)
    if match:
        return match.group(1).strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(lines[1:])
    if clean.endswith("```"):
        clean = "\n".join(clean.splitlines()[:-1])
    return clean.strip()


def _later_lines_have_json(lines: list) -> bool:
    for line in lines:
        stripped = line.strip()
        if "{" in stripped or "[" in stripped or stripped.startswith("```"):
            return True
    return False


def _strip_llm_preamble(text: str) -> str:
    """Drop a leading 'Here is the JSON requested' line so json.loads sees JSON."""
    s = (text or "").replace("\ufeff", "").strip()
    if not s:
        return s
    lines = s.splitlines()
    while lines:
        line = lines[0].strip()
        if not line:
            lines.pop(0)
            continue
        match = _PREAMBLE_LINE.match(line)
        if not match:
            break
        tail = line[match.end():].lstrip(" \t:.-")
        tail_has_payload = bool(tail) and (tail[0] in "{[" or "```" in tail)
        if tail_has_payload and not _later_lines_have_json(lines[1:]):
            lines[0] = tail
            break
        lines.pop(0)
    return "\n".join(lines).strip()


def _prepare_llm_json_text(text: str) -> str:
    """Strip common Gemini preambles and markdown fences before json.loads."""
    return _strip_llm_preamble(_strip_code_fences(_strip_llm_preamble(text or "")))


def _extract_balanced_json(text: str) -> str:
    """Extract the first balanced JSON object or array from a messy LLM response."""
    s = _prepare_llm_json_text(text)
    start_positions = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not start_positions:
        return s
    start = min(start_positions)
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(s[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i+1]
    # No balanced close. Return from first brace so caller can attempt truncation repair.
    return s[start:]


def _repair_json_text(clean: str) -> str:
    fixed = re.sub(r"(?<!\\)'([^']*)'", r'"\1"', clean or "")
    return re.sub(r",\s*([}\]])", r"\1", fixed)


def _scan_top_level_json(text: str) -> list:
    """Decode successive top-level JSON values. Do not walk into a broken value."""
    decoder = json.JSONDecoder()
    found = []
    idx = 0
    s = text or ""
    while True:
        while idx < len(s) and s[idx] not in "{[":
            idx += 1
        if idx >= len(s):
            break
        try:
            obj, end = decoder.raw_decode(s, idx)
        except json.JSONDecodeError:
            break
        found.append(obj)
        idx = end
    return found


def _prefer_llm_payload(found: list):
    """Prefer the last object that looks like a research or analysis reply."""
    if not found:
        return None
    preferred = [
        value for value in found
        if isinstance(value, dict) and any(key in value for key in _LLM_PAYLOAD_KEYS)
    ]
    if preferred:
        return preferred[-1]
    last = found[-1]
    if isinstance(last, (dict, list)):
        return last
    return None


def _coerce_llm_payload(value):
    """Callers expect an object. A one-item array unwraps; ticker arrays become selected."""
    if isinstance(value, dict) or value is None:
        return value
    if not isinstance(value, list):
        return None
    dicts = [item for item in value if isinstance(item, dict)]
    if len(dicts) == 1:
        return dicts[0]
    if dicts and all("ticker" in item for item in dicts) and not any(
        "action" in item or "decision" in item for item in dicts
    ):
        return {"selected": dicts}
    return None


def _salvage_research_json(text: str) -> Optional[dict]:
    """Best-effort salvage for truncated research JSON: pull ticker symbols and reasons."""
    raw = text or ""
    tickers = []
    for m in re.finditer(r'"ticker"\s*:\s*"([A-Z]{1,6})"', raw):
        t = m.group(1).upper()
        if t not in tickers:
            tickers.append(t)
    if not tickers:
        return None
    picks = []
    for t in tickers[:5]:
        idx = raw.find(t)
        window = raw[idx:idx+260] if idx >= 0 else ""
        rm = re.search(r'"reason"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)', window)
        reason = rm.group(1) if rm else "Recovered from truncated LLM JSON"
        picks.append({"ticker": t, "reason": reason[:220], "confidence": 0.55, "json_repaired": True})
    return {"selected": picks, "json_repaired": True}


def _payload_from_fragment(fragment: str):
    """json.loads a prepared fragment, or the last research/analysis object in it."""
    fragment = (fragment or "").strip()
    if not fragment:
        return None
    try:
        loaded = json.loads(fragment)
    except json.JSONDecodeError:
        loaded = None
    else:
        coerced = _coerce_llm_payload(loaded)
        if isinstance(coerced, dict):
            return coerced
    return _coerce_llm_payload(_prefer_llm_payload(_scan_top_level_json(fragment)))


def _parsed_payload_is_usable(parsed) -> bool:
    """Non-empty research picks, or an analysis decision. Empty selected is not usable."""
    if not isinstance(parsed, dict):
        return False
    if _selected_picks(parsed):
        return True
    return bool(parsed.get("action") or parsed.get("decision"))


def _should_salvage_research(text: str, parsed) -> bool:
    """Salvage after a hard failure, or when selected is empty but ticker fields remain.

    A finished analysis object (action/decision) is left alone. Clean research JSON
    with a non-empty selected array is left alone.
    """
    if _parsed_payload_is_usable(parsed):
        return False
    if isinstance(parsed, dict):
        return bool(re.search(r'"ticker"\s*:\s*"[A-Z]{1,6}"', text or ""))
    return True


_ANALYSIS_ACTION_RE = re.compile(
    r'"(?:decision|action)"\s*:\s*"(BUY|SELL|HOLD|SKIP)"',
    re.IGNORECASE,
)


def _salvage_analysis_json(text: str) -> Optional[dict]:
    """Pull a decision out of truncated analysis JSON. Research tickers are not invented."""
    raw = text or ""
    match = _ANALYSIS_ACTION_RE.search(raw)
    if not match:
        return None
    action = match.group(1).upper()
    out = {
        "action": action,
        "decision": action,
        "json_repaired": True,
        "rationale": "Recovered from truncated analysis JSON",
    }
    confidence = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', raw)
    if confidence:
        try:
            out["confidence"] = float(confidence.group(1))
        except ValueError:
            pass
    rationale = re.search(r'"rationale"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)', raw)
    if rationale and rationale.group(1).strip():
        out["rationale"] = rationale.group(1)[:500]
    return out


def _should_salvage_analysis(text: str, parsed) -> bool:
    """Salvage a cut-off decision. A finished analysis or research payload is left alone."""
    if _analysis_payload_is_valid(parsed) or _parsed_payload_is_usable(parsed):
        return False
    return bool(_ANALYSIS_ACTION_RE.search(text or ""))


def _parse_json(text: str):
    """Parse an LLM reply. Clean JSON is unchanged; preambles and fences are stripped first.

    If loads, balanced extract, and single-quote/trailing-comma repair all fail,
    analysis salvage pulls a BUY/SELL/HOLD/SKIP out of the raw text when one is
    present, and `_salvage_research_json` pulls ticker/reason pairs otherwise.
    The same salvage runs when parse succeeds but the object is not a usable
    research or analysis payload.
    """
    if not text:
        return None
    parsed = _payload_from_fragment(_prepare_llm_json_text(text))
    if not isinstance(parsed, dict):
        repaired = _repair_json_text(_extract_balanced_json(text))
        try:
            parsed = _payload_from_fragment(repaired)
        except Exception as e:
            log.error("[LLM] JSON parse failed: %s | text[:150]: %r", e, (text or "")[:150])
            parsed = None
    if _parsed_payload_is_usable(parsed):
        return parsed
    if _should_salvage_analysis(text, parsed):
        salvaged = _salvage_analysis_json(text)
        if salvaged and _analysis_payload_is_valid(salvaged):
            log.warning("[LLM] Salvaged analysis decision %s from malformed JSON", salvaged.get("action"))
            return salvaged
    if _should_salvage_research(text, parsed):
        salvaged = _salvage_research_json(text)
        if salvaged and salvaged.get("selected"):
            log.warning(
                "[LLM] Salvaged %d research ticker(s) from malformed JSON",
                len(salvaged["selected"]),
            )
            return salvaged
    if isinstance(parsed, dict):
        return parsed
    log.error("[LLM] JSON parse failed: text[:150]: %r", (text or "")[:150])
    return None


# ── Status summary ────────────────────────────────────────────────────────────
def usage_summary() -> dict:
    u = _load_usage()
    u["provider_health"] = _load_health()
    return u


def active_mode() -> str:
    return _effective_mode()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    print(f"\n🤖 LLM Router test — mode={LLM_MODE}\n")
    for p in ["claude", "openai", "gemini", "mistral", "deepseek", "groq", "nvidia"]:
        print(f"  {p:8} key: {'✓ set' if PROVIDER_KEYS[p] else '✗ not set'}")
    print(f"  Daily budget: {DAILY_TOKEN_BUDGET:,} tokens")
    print(f"  Budget used:  {budget_pct()*100:.1f}%\n")
    test_prompt = 'You are a stock analyst. Pick the best stock from: AAPL, MSFT, NVDA. Respond ONLY with valid JSON: {"selected": [{"ticker": "AAPL", "reason": "test", "confidence": 0.8}]}'
    print(json.dumps(query_research(test_prompt, agent_tag="test"), indent=2))
