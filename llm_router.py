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
GROQ_LLAMA    = os.getenv("GROQ_LLAMA_MODEL",    "llama-3.3-70b-versatile")
GROQ_QWEN     = os.getenv("GROQ_QWEN_MODEL",     "llama-3.1-8b-instant")  # low-TPM friendly default
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

def _provider_sorted(candidates: Iterable[tuple[str, str]], phase: str, prompt: str = "") -> list[tuple[str, str]]:
    input_tokens = _estimate_tokens(prompt)
    ready = [(p, m) for p, m in candidates if _provider_ready(p)]
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

def _classify_cooldown(provider: str, err: Exception):
    msg = str(err).lower()
    if "401" in msg or "authentication" in msg or "invalid x-api-key" in msg or "invalid api key" in msg:
        _cooldown_provider(provider, 24*3600, "auth/key error")
    elif "insufficient balance" in msg or "payment" in msg or "billing" in msg:
        _cooldown_provider(provider, 24*3600, "billing/balance error")
    elif "429" in msg or "rate" in msg or "capacity" in msg or "tokens per minute" in msg or "request too large" in msg:
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


def _tiered_choices(phase: str, prompt: str) -> list[tuple[str, str, str]]:
    """Return [(alias, provider, model)] for configured, healthy tiered participants."""
    candidates = []
    for alias in _tiered_aliases(phase):
        provider, model = MODEL_ALIASES[alias]
        if _provider_ready(provider):
            candidates.append((alias, provider, model))
    if LLM_AUTO_ADAPT:
        candidates = sorted(candidates, key=lambda apm: _score_provider(apm[1], phase, _estimate_tokens(prompt)), reverse=True)
    fanout_default = "3" if phase == "research" else "3"
    fanout = int(os.getenv("LLM_TIERED_FANOUT", os.getenv("LLM_TIERED_RESEARCH_FANOUT" if phase == "research" else "LLM_TIERED_ANALYSIS_FANOUT", fanout_default)))
    return candidates[:max(1, fanout)]


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


def _tiered_research(prompt: str, agent_tag: str = "research") -> Optional[dict]:
    """Proactive tiered research: several authenticated models screen independently, then we merge votes."""
    choices = _tiered_choices("research", prompt)
    if not choices:
        log.error("[LLM/TieredResearch] No configured/healthy tiered research models available.")
        return None

    merged: dict[str, dict] = {}
    successful = []
    for alias, provider, model in choices:
        try:
            text = _call_provider(provider, model, prompt, max_tokens=_max_tokens_for(provider, "research"), agent_tag=f"{agent_tag}_{alias}")
            parsed = _parse_json(text) or {}
            picks = _selected_picks(parsed)
            if not picks:
                log.warning(
                    "[LLM/TieredResearch] %s returned no selected picks — one strict JSON retry.",
                    alias,
                )
                text = _call_provider(
                    provider, model, _strict_research_prompt(prompt),
                    max_tokens=_max_tokens_for(provider, "research"),
                    agent_tag=f"{agent_tag}_{alias}_strict",
                )
                parsed = _parse_json(text) or {}
                picks = _selected_picks(parsed)
            if not picks:
                log.warning("[LLM/TieredResearch] %s returned no selected picks.", alias)
                continue
            successful.append(alias)
            for pick in picks:
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
        except Exception as e:
            log.warning(f"[LLM/TieredResearch] {alias} ({provider}/{model}) failed: {e}")
            _classify_cooldown(provider, e)

    if not merged:
        log.error("[LLM/TieredResearch] All tiered research models failed or returned no picks.")
        return None

    selected = []
    for rec in merged.values():
        avg_conf = rec["confidence_sum"] / max(1, rec["votes"])
        # Vote bonus lets consensus rise above one-model picks without hiding confidence.
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
    log.info(f"[LLM/TieredResearch] Used {successful}; merged picks: {[x['ticker'] for x in selected[:5]]}")
    return {"selected": selected[:5], "tiered_models_used": successful}


def _tiered_analysis(prompt: str, agent_tag: str = "analysis") -> Optional[dict]:
    """Proactive tiered analysis: several models decide independently, then voting/aggregation returns one action."""
    choices = _tiered_choices("analysis", prompt)
    if not choices:
        log.error("[LLM/TieredAnalysis] No configured/healthy tiered analysis models available.")
        return {"action": "SKIP", "shares": 0, "rationale": "No configured/healthy tiered analysis models available"}

    decisions = []
    successful = []
    for alias, provider, model in choices:
        try:
            text = _call_provider(provider, model, prompt, max_tokens=_max_tokens_for(provider, "analysis"), agent_tag=f"{agent_tag}_{alias}")
            parsed = _parse_json(text)
            if not parsed:
                continue
            parsed["tiered_source"] = alias
            decisions.append(parsed)
            successful.append(alias)
        except Exception as e:
            log.warning(f"[LLM/TieredAnalysis] {alias} ({provider}/{model}) failed: {e}")
            _classify_cooldown(provider, e)

    if not decisions:
        return {"action": "SKIP", "shares": 0, "rationale": "All tiered analysis models failed or returned invalid JSON", "tiered_status": "error"}

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
    return [(p, m) for p, m in candidates if _provider_ready(p)]


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
def _call_provider(provider: str, model: str, prompt: str, max_tokens: int = 600, agent_tag: str = "") -> str:
    phase = "analysis" if "analysis" in (agent_tag or "") else "research"
    prompt = _json_system_prefix(phase) + _compact_prompt(prompt, provider, phase)
    if max_tokens is None or max_tokens <= 0:
        max_tokens = _max_tokens_for(provider, phase)
    if provider == "claude":
        out = _call_claude(model, prompt, max_tokens, agent_tag); _mark_provider_success(provider); return out
    if provider == "openai":
        out = _call_openai(model, prompt, max_tokens, agent_tag); _mark_provider_success(provider); return out
    if provider == "gemini":
        out = _call_gemini(model, prompt, max_tokens, agent_tag); _mark_provider_success(provider); return out
    if provider == "mistral":
        out = _call_chat_endpoint("mistral", model, prompt, max_tokens, agent_tag,
                                   "https://api.mistral.ai/v1/chat/completions", MISTRAL_API_KEY); _mark_provider_success(provider); return out
    if provider == "deepseek":
        out = _call_chat_endpoint("deepseek", model, prompt, max_tokens, agent_tag,
                                   "https://api.deepseek.com/chat/completions", DEEPSEEK_API_KEY); _mark_provider_success(provider); return out
    if provider == "groq":
        out = _call_chat_endpoint("groq", model, prompt, max_tokens, agent_tag,
                                   "https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY); _mark_provider_success(provider); return out
    if provider == "nvidia":
        out = _call_chat_endpoint("nvidia", model, prompt, max_tokens, agent_tag,
                                   f"{NVIDIA_BASE_URL}/chat/completions", NVIDIA_API_KEY); _mark_provider_success(provider); return out
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
def query_research(prompt: str, agent_tag: str = "research") -> Optional[dict]:
    mode = _effective_mode()
    if mode == "budget_exhausted":
        return None
    if mode == "dual":
        return _dual_research(prompt, agent_tag)
    if _is_proactive_tiered(mode):
        return _tiered_research(prompt, agent_tag)
    picked = _pick_model("research", mode)
    if not picked:
        raise RuntimeError("No API keys configured for any supported LLM provider.")
    provider, model = picked
    try:
        text = _call_provider(provider, model, prompt, max_tokens=_max_tokens_for(provider, "research"), agent_tag=agent_tag)
        return _parse_json(text)
    except Exception as e:
        log.warning(f"[LLM] Primary research model {provider}/{model} failed: {e}")
        _classify_cooldown(provider, e)
        for fp, fm in _fallback_models("research", provider):
            try:
                text = _call_provider(fp, fm, prompt, max_tokens=_max_tokens_for(fp, "research"), agent_tag=f"{agent_tag}_{fp}_fallback")
                return _parse_json(text)
            except Exception as fe:
                log.warning(f"[LLM] Fallback research model {fp}/{fm} failed: {fe}")
                _classify_cooldown(fp, fe)
        log.error("[LLM] All research models failed; returning no picks instead of crashing cycle.")
        return None


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

def query_analysis(prompt: str, agent_tag: str = "analysis") -> Optional[dict]:
    mode = _effective_mode()
    if mode == "budget_exhausted":
        return None
    if mode == "dual":
        return _dual_analysis(prompt, agent_tag)
    if _is_proactive_tiered(mode):
        return _tiered_analysis(prompt, agent_tag)
    picked = _pick_model("analysis", mode)
    if not picked:
        raise RuntimeError("No API keys configured for any supported LLM provider.")
    provider, model = picked
    try:
        text = _call_provider(provider, model, prompt, max_tokens=_max_tokens_for(provider, "analysis"), agent_tag=agent_tag)
        return _parse_json(text)
    except Exception as e:
        log.warning(f"[LLM] Primary analysis model {provider}/{model} failed: {e}")
        _classify_cooldown(provider, e)
        for fp, fm in _fallback_models("analysis", provider):
            try:
                text = _call_provider(fp, fm, prompt, max_tokens=_max_tokens_for(fp, "analysis"), agent_tag=f"{agent_tag}_{fp}_fallback")
                return _parse_json(text)
            except Exception as fe:
                log.warning(f"[LLM] Fallback analysis model {fp}/{fm} failed: {fe}")
                _classify_cooldown(fp, fe)
        log.error("[LLM] All analysis models failed; returning SKIP instead of crashing cycle.")
        return {"action": "SKIP", "shares": 0, "rationale": "All configured LLM providers failed or are cooling down"}


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
    for provider, model in _dual_pair():
        try:
            text = _call_provider(provider, model, prompt, max_tokens=600, agent_tag=f"{agent_tag}_{provider}")
            parsed = _parse_json(text)
            if parsed and "selected" in parsed:
                for pick in parsed["selected"]:
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
            time.sleep(1)
    picks = sorted(results.values(), key=lambda x: (x["dual_agree"], x["confidence"]), reverse=True)
    return {"selected": picks[:5]}


def _dual_analysis(prompt: str, agent_tag: str) -> dict:
    decisions = {}
    for provider, model in _dual_pair():
        try:
            text = _call_provider(provider, model, prompt, max_tokens=400, agent_tag=f"{agent_tag}_{provider}")
            parsed = _parse_json(text)
            if parsed:
                decisions[provider] = parsed
        except Exception as e:
            log.warning(f"[LLM/DualAnalysis] {provider} failed: {e}")
    if not decisions:
        return {"action": "SKIP", "shares": 0, "rationale": "All models failed", "dual_status": "error"}
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


def _parse_json(text: str):
    """Parse an LLM reply. Clean JSON is unchanged; preambles and fences are stripped first.

    If loads, balanced extract, and single-quote/trailing-comma repair all fail,
    `_salvage_research_json` pulls ticker/reason pairs out of the raw text.
    The same salvage runs when parse succeeds but `selected` is missing or empty
    and the text still looks like broken research JSON.
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
