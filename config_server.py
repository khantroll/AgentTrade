"""
config_server.py — Local Config API Server
============================================
A tiny Flask server that runs alongside agent.py and lets the dashboard
read and write configuration (API keys, LLM mode, budget, etc.) through
a local HTTP API at http://localhost:5111.

Why a server instead of direct file access?
  - The dashboard is a browser file:// page — browsers block direct
    filesystem writes for security reasons.
  - The server runs on localhost only (never exposed to the internet).
  - Keys are stored in config.json (local only, gitignored).
  - The agent loads config.json at startup, so changes take effect
    on the next cycle without restarting.

Endpoints:
  GET  /config          → returns current config (keys masked)
  POST /config          → save full config
  POST /config/key      → save a single key {"key": "OPENAI_API_KEY", "value": "sk-..."}
  POST /test/anthropic  → test Anthropic key validity
  POST /test/openai     → test OpenAI key validity
  POST /test/alpaca     → test Alpaca key validity
  POST /test/reddit     → test Reddit credentials
  POST /test/newsapi    → test NewsAPI key
  POST /test/quiver     → test Quiver Quantitative key
  GET  /status          → returns agent status (market open, budget, etc.)

Usage:
  pip install flask flask-cors
  python config_server.py          # runs on port 5111
  # OR let agent.py start it automatically as a background thread
"""

import os
import json
import logging
import threading
import re
import unicodedata
import requests as req
from datetime import datetime

log = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
PORT        = 5111


def _resolve_app_root() -> str:
    """Install dir for config_server (never use __file__ inside Flask route handlers)."""
    for candidate in (
        os.getenv("TRADING_AGENT_DIR"),
        os.getenv("APP_DIR"),
        os.getcwd(),
    ):
        if not candidate:
            continue
        root = os.path.abspath(candidate)
        if os.path.isfile(os.path.join(root, "config.json")) or os.path.isfile(
            os.path.join(root, "config_server.py")
        ):
            return root
    mod = globals().get("__file__")
    if mod:
        return os.path.dirname(os.path.abspath(mod))
    return os.getcwd()


APP_ROOT = _resolve_app_root()

# Keys that should be masked in GET responses
SENSITIVE_KEYS = {
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY", "DEEPSPEAK_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "NIM_API_KEY",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    "REDDIT_CLIENT_SECRET", "QUIVER_API_KEY", "NEWS_API_KEY",
}

# Default config structure
DEFAULT_CONFIG = {
    "ALPACA_API_KEY":       "",
    "ALPACA_SECRET_KEY":    "",
    "ALPACA_PAPER":         "true",
    "ANTHROPIC_API_KEY":    "",
    "OPENAI_API_KEY":       "",
    "GEMINI_API_KEY":       "",
    "MISTRAL_API_KEY":      "",
    "DEEPSEEK_API_KEY":     "",
    "DEEPSPEAK_API_KEY":    "",
    "GROQ_API_KEY":         "",
    "NVIDIA_API_KEY":       "",
    "NIM_API_KEY":          "",
    "NVIDIA_BASE_URL":      "https://integrate.api.nvidia.com/v1",
    "NVIDIA_LLAMA_MODEL":   "meta/llama-3.1-70b-instruct",
    "NVIDIA_QWEN_MODEL":    "qwen/qwen3-235b-a22b",
    "NVIDIA_DEEPSEEK_MODEL": "deepseek-ai/deepseek-r1",
    "LLM_MODE":             "tiered",
    "DAILY_TOKEN_BUDGET":   "200000",
    "MAX_DAILY_TRADES":     "5",
    "STRATEGY_AGGRESSION":  "balanced",
    "HARD_REBALANCE":       "false",
    "HARD_REBALANCE_DRIFT_PCT": "0.10",
    "SCREENER_CACHE":       "true",
    "SCREENER_CACHE_TTL_MINUTES": "240",
    "NEWS_API_KEY":         "",
    "QUIVER_API_KEY":       "",
    "REDDIT_CLIENT_ID":     "",
    "REDDIT_CLIENT_SECRET": "",
    "REDDIT_USER_AGENT":    "trading_agent_bot/1.0",

}


# ── Value normalization helpers ───────────────────────────────────────────────

MASK_CHARS = {"•", "*", "●", "○", "·"}


def _looks_masked(value: object) -> bool:
    """True when a browser sent a masked secret value from GET /config."""
    if value is None:
        return False
    s = str(value)
    return any(ch in s for ch in MASK_CHARS)


def sanitize_config_value(key: str, value: object) -> str:
    """
    Normalize config values before saving or testing.

    API keys copied from phones, password managers, email, or web pages can pick up
    BOMs, zero-width spaces, non-breaking spaces, smart quotes, or trailing newlines.
    Some HTTP/auth libraries then throw errors like:
      'latin-1' codec can't encode character ...

    For key/token fields we remove invisible/whitespace formatting characters and
    common wrapper quotes. For ordinary config fields we only trim line endings.
    """
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = s.replace("\ufeff", "")
    # Remove zero-width/invisible format chars and DEL/control chars except \t/\n/\r,
    # then strip all whitespace from token-like fields.
    token_like = key in SENSITIVE_KEYS or key.endswith("_KEY") or key.endswith("_SECRET") or key == "REDDIT_CLIENT_ID"
    if token_like:
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf" and ord(ch) not in range(0, 32) and ord(ch) != 127)
        s = re.sub(r"\s+", "", s)
        s = s.strip('"\'“”‘’`')
    else:
        s = s.strip()
    return s


def _request_value_or_saved(data: dict, request_name: str, config_key: str) -> str:
    """Use the posted value unless it is blank/masked; then fall back to saved config."""
    incoming = data.get(request_name, "")
    if incoming and not _looks_masked(incoming):
        return sanitize_config_value(config_key, incoming)
    return sanitize_config_value(config_key, load_config().get(config_key, ""))


# ── Config file helpers ───────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config.json, merging with defaults for any missing keys."""
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        # Fill in any new keys added since last save
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = v
        return data
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG)
    except Exception as e:
        log.error(f"[Config] Load error: {e}")
        return dict(DEFAULT_CONFIG)


def _atomic_write(path: str, data: dict):
    """Write JSON atomically via temp file + os.replace (Linux atomic)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f: json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def save_config(data: dict) -> bool:
    """Save config to config.json and also sync to .env so agent picks it up."""
    try:
        # Only keep known keys
        clean = {k: sanitize_config_value(k, v) for k, v in data.items() if k in DEFAULT_CONFIG}
        _atomic_write(CONFIG_FILE, clean)

        # Also write .env so agent.py / dotenv picks it up without restart
        _sync_to_env(clean)
        log.info(f"[Config] Saved {len(clean)} keys to {CONFIG_FILE}")
        return True
    except Exception as e:
        log.error(f"[Config] Save error: {e}")
        return False


def _sync_to_env(config: dict):
    """Write config values back to .env file so dotenv picks them up."""
    try:
        lines = []
        if os.path.exists(".env"):
            with open(".env") as f:
                lines = f.readlines()

        # Build a map of existing lines
        env_map = {}
        other_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                other_lines.append(line)
                continue
            k = stripped.split("=", 1)[0].strip()
            env_map[k] = line

        # Update / add config keys
        for k, v in config.items():
            if v:  # only write non-empty values
                env_map[k] = f"{k}={v}\n"

        with open(".env", "w") as f:
            f.writelines(other_lines)
            for line in env_map.values():
                f.write(line)
    except Exception as e:
        log.warning(f"[Config] .env sync warning: {e}")


def mask_config(config: dict) -> dict:
    """Return config with sensitive values masked for the API response."""
    masked = dict(config)
    for k in SENSITIVE_KEYS:
        if k in masked and masked[k]:
            val = str(masked[k])
            if len(val) > 8:
                masked[k] = val[:4] + "*" * (len(val) - 8) + val[-4:]
            else:
                masked[k] = "*" * len(val)
    return masked


def apply_config_to_env() -> dict:
    """Load config.json into os.environ. UI-saved config.json wins over .env."""
    cfg = load_config()
    for k, v in cfg.items():
        if v is not None and str(v).strip() != "":
            os.environ[k] = str(v)
    return cfg


def get_key(key: str) -> str:
    """Get a single config value — used by agent modules."""
    config = load_config()
    return config.get(key, os.getenv(key, ""))


def set_llm_mode(mode: str) -> dict:
    """Persist LLM_MODE to config.json and .env."""
    mode = sanitize_config_value("LLM_MODE", mode)
    if not mode:
        return {"ok": False, "message": "LLM_MODE is required"}
    existing = load_config()
    existing["LLM_MODE"] = mode
    ok = save_config(existing)
    apply_config_to_env()
    return {"ok": ok, "message": "Saved" if ok else "Save failed", "LLM_MODE": mode}


# ── Key testers ───────────────────────────────────────────────────────────────

def test_anthropic(api_key: str) -> dict:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply OK"}],
        )
        return {"ok": True, "message": f"Connected ✓  (model: claude-haiku-4-5-20251001)"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_openai(api_key: str) -> dict:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp   = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=5,
            messages=[{"role": "user", "content": "Reply OK"}],
        )
        return {"ok": True, "message": "Connected ✓  (model: gpt-4o-mini)"}
    except Exception as e:
        return {"ok": False, "message": str(e)}



def test_gemini(api_key: str) -> dict:
    try:
        r = req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": "Reply OK"}]}], "generationConfig": {"maxOutputTokens": 8}},
            timeout=12,
        )
        if r.status_code < 400:
            return {"ok": True, "message": "Connected ✓  (model: gemini-2.5-flash)"}
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:160]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _test_chat_endpoint(name: str, api_key: str, url: str, model: str) -> dict:
    try:
        r = req.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 8},
            timeout=12,
        )
        if r.status_code < 400:
            return {"ok": True, "message": f"Connected ✓  (model: {model})"}
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:160]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_mistral(api_key: str) -> dict:
    return _test_chat_endpoint("mistral", api_key, "https://api.mistral.ai/v1/chat/completions", "mistral-small-latest")


def test_deepseek(api_key: str) -> dict:
    return _test_chat_endpoint("deepseek", api_key, "https://api.deepseek.com/chat/completions", "deepseek-chat")


def test_groq(api_key: str) -> dict:
    return _test_chat_endpoint("groq", api_key, "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile")


def test_nvidia(api_key: str) -> dict:
    base = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    model = os.getenv("NVIDIA_LLAMA_MODEL", "meta/llama-3.1-70b-instruct")
    return _test_chat_endpoint("nvidia", api_key, f"{base}/chat/completions", model)


def test_alpaca(api_key: str, secret_key: str, paper: bool = True) -> dict:
    try:
        base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        r    = req.get(
            f"{base}/v2/account",
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
            timeout=8,
        )
        if r.status_code == 200:
            acct = r.json()
            pv   = float(acct.get("portfolio_value", 0))
            mode = "Paper" if paper else "⚠️  LIVE"
            return {"ok": True, "message": f"Connected ✓  {mode} · Portfolio ${pv:,.2f}"}
        else:
            return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_reddit(client_id: str, client_secret: str, user_agent: str) -> dict:
    try:
        r = req.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": user_agent or "trading_agent_bot/1.0"},
            timeout=8,
        )
        if r.status_code == 200 and "access_token" in r.json():
            return {"ok": True, "message": "Connected ✓  OAuth token obtained"}
        else:
            return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_newsapi(api_key: str) -> dict:
    try:
        r = req.get(
            "https://newsapi.org/v2/top-headlines",
            params={"country": "us", "pageSize": 1, "apiKey": api_key},
            timeout=8,
        )
        data = r.json()
        if data.get("status") == "ok":
            return {"ok": True, "message": f"Connected ✓  ({data.get('totalResults',0)} headlines available)"}
        else:
            return {"ok": False, "message": data.get("message", "Unknown error")}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_quiver(api_key: str) -> dict:
    try:
        r = req.get(
            "https://api.quiverquant.com/beta/live/congresstrading",
            headers={"Authorization": f"Token {api_key}"},
            timeout=8,
        )
        if r.status_code == 200:
            count = len(r.json()) if isinstance(r.json(), list) else "?"
            return {"ok": True, "message": f"Connected ✓  ({count} recent trades available)"}
        else:
            return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ── Flask server ──────────────────────────────────────────────────────────────

def create_app():
    try:
        from flask import Flask, jsonify, request
        from flask_cors import CORS
    except ImportError:
        raise ImportError("Run: pip install flask flask-cors")

    app = Flask(__name__)
    CORS(app, origins=["null", "http://localhost:*", "file://*"])  # allow file:// dashboard

    @app.route("/config", methods=["GET"])
    def get_config():
        return jsonify({"ok": True, "config": mask_config(load_config())})

    @app.route("/config", methods=["POST"])
    def post_config():
        data = request.get_json(force=True) or {}
        # Dedicated mode-only update from dashboard buttons
        if set(data.keys()) <= {"LLM_MODE", "llm_mode", "action"} and (data.get("LLM_MODE") or data.get("llm_mode")):
            mode = data.get("LLM_MODE") or data.get("llm_mode")
            return jsonify(set_llm_mode(mode))

        # Merge with existing — never overwrite with empty strings
        existing = load_config()
        for k, v in data.items():
            if k not in DEFAULT_CONFIG:
                continue
            if _looks_masked(v):
                continue
            v = sanitize_config_value(k, v)
            if v:
                existing[k] = v
        ok = save_config(existing)
        apply_config_to_env()
        return jsonify({"ok": ok, "message": "Saved" if ok else "Save failed", "LLM_MODE": existing.get("LLM_MODE")})

    @app.route("/config/mode", methods=["GET"])
    def get_llm_mode():
        cfg = load_config()
        last_run = None
        try:
            from agenttrade import db as ledger
            ledger.init_db()
            cycle = ledger.get_latest_cycle_run() or {}
            last_run = cycle.get("mode")
            if not last_run:
                artifacts = ledger.get_latest_funnel().get("artifacts") or {}
                last_run = artifacts.get("llm_mode")
        except Exception:
            last_run = None
        if not last_run:
            # NON-AUTHORITATIVE fallback: projection cache
            try:
                state_path = os.path.join(APP_ROOT, "agent_state.json")
                with open(state_path) as f:
                    last_run = json.load(f).get("llm_mode")
            except Exception:
                pass
        return jsonify({
            "ok": True,
            "LLM_MODE": cfg.get("LLM_MODE", "tiered"),
            "last_run_mode": last_run,
        })

    @app.route("/config/mode", methods=["POST"])
    def post_llm_mode():
        data = request.get_json(force=True) or {}
        mode = data.get("LLM_MODE") or data.get("llm_mode")
        if not mode:
            return jsonify({"ok": False, "message": "LLM_MODE required"}), 400
        return jsonify(set_llm_mode(mode))

    @app.route("/config/key", methods=["POST"])
    def post_single_key():
        data    = request.get_json(force=True)
        key     = data.get("key", "")
        value   = data.get("value", "")
        if not key or key not in DEFAULT_CONFIG:
            return jsonify({"ok": False, "message": f"Unknown key: {key}"}), 400
        existing = load_config()
        # Browser fields may contain masked values such as sk-****abcd after reload.
        # Saving those would corrupt the real key, so ignore masked placeholders.
        if _looks_masked(value):
            return jsonify({"ok": True, "message": "Masked value ignored; saved key kept"})
        existing[key] = sanitize_config_value(key, value)
        ok = save_config(existing)
        apply_config_to_env()
        return jsonify({"ok": ok, "message": "Saved" if ok else "Save failed"})

    @app.route("/test/anthropic", methods=["POST"])
    def route_test_anthropic():
        d = request.get_json(force=True)
        return jsonify(test_anthropic(_request_value_or_saved(d, "api_key", "ANTHROPIC_API_KEY")))

    @app.route("/test/openai", methods=["POST"])
    def route_test_openai():
        d = request.get_json(force=True)
        return jsonify(test_openai(_request_value_or_saved(d, "api_key", "OPENAI_API_KEY")))


    @app.route("/test/gemini", methods=["POST"])
    def route_test_gemini():
        d = request.get_json(force=True)
        return jsonify(test_gemini(_request_value_or_saved(d, "api_key", "GEMINI_API_KEY")))

    @app.route("/test/mistral", methods=["POST"])
    def route_test_mistral():
        d = request.get_json(force=True)
        return jsonify(test_mistral(_request_value_or_saved(d, "api_key", "MISTRAL_API_KEY")))

    @app.route("/test/deepseek", methods=["POST"])
    def route_test_deepseek():
        d = request.get_json(force=True)
        return jsonify(test_deepseek(_request_value_or_saved(d, "api_key", "DEEPSEEK_API_KEY")))

    @app.route("/test/groq", methods=["POST"])
    def route_test_groq():
        d = request.get_json(force=True)
        return jsonify(test_groq(_request_value_or_saved(d, "api_key", "GROQ_API_KEY")))

    @app.route("/test/nvidia", methods=["POST"])
    def route_test_nvidia():
        d = request.get_json(force=True)
        return jsonify(test_nvidia(_request_value_or_saved(d, "api_key", "NVIDIA_API_KEY")))

    @app.route("/test/alpaca", methods=["POST"])
    def route_test_alpaca():
        d = request.get_json(force=True)
        return jsonify(test_alpaca(
            _request_value_or_saved(d, "api_key", "ALPACA_API_KEY"),
            _request_value_or_saved(d, "secret_key", "ALPACA_SECRET_KEY"),
            d.get("paper", True)
        ))

    @app.route("/test/reddit", methods=["POST"])
    def route_test_reddit():
        d = request.get_json(force=True)
        return jsonify(test_reddit(
            _request_value_or_saved(d, "client_id", "REDDIT_CLIENT_ID"),
            _request_value_or_saved(d, "client_secret", "REDDIT_CLIENT_SECRET"),
            sanitize_config_value("REDDIT_USER_AGENT", d.get("user_agent", load_config().get("REDDIT_USER_AGENT", "trading_agent_bot/1.0")))
        ))

    @app.route("/test/newsapi", methods=["POST"])
    def route_test_newsapi():
        d = request.get_json(force=True)
        return jsonify(test_newsapi(_request_value_or_saved(d, "api_key", "NEWS_API_KEY")))

    @app.route("/test/quiver", methods=["POST"])
    def route_test_quiver():
        d = request.get_json(force=True)
        return jsonify(test_quiver(_request_value_or_saved(d, "api_key", "QUIVER_API_KEY")))

    @app.route("/state", methods=["GET"])
    def get_state():
        """Dashboard state from SQLite + optional live Alpaca. JSON is projection only."""
        live = request.args.get("live", "").lower() in ("1", "true", "yes")
        try:
            from agenttrade.publish import build_dashboard_state
            if live:
                return jsonify(build_dashboard_state())
            return jsonify(build_dashboard_state(live_snapshot={}))
        except Exception:
            # NON-AUTHORITATIVE fallback: projection cache
            try:
                state_path = os.path.join(APP_ROOT, "agent_state.json")
                with open(state_path) as f:
                    return jsonify(json.load(f))
            except Exception:
                return jsonify({"ok": False, "message": "dashboard state unavailable"}), 404

    @app.route("/account/live", methods=["GET"])
    def get_live_account():
        """Live Alpaca account snapshot merged with SQLite ledger."""
        from agenttrade.publish import build_dashboard_state
        try:
            return jsonify({"ok": True, **build_dashboard_state()})
        except Exception as e:
            log.exception("[Account] GET /account/live failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/ledger", methods=["GET"])
    def get_ledger():
        from agenttrade import db as ledger
        try:
            ledger.init_db()
            return jsonify({"ok": True, **ledger.get_dashboard_ledger()})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/history", methods=["GET"])
    def get_history_route():
        """Cycle + daily rollup for performance reporting tab."""
        from performance_history import get_history, seed_from_agent_state_if_empty, publish_history
        try:
            public_dir = os.getenv(
                "PUBLIC_DASHBOARD_DIR",
                "/var/www/my_webapp__3/www",
            )
            if seed_from_agent_state_if_empty():
                publish_history(public_dir)
            days = int(request.args.get("days", 90))
            days = max(7, min(days, 365))
            payload = get_history(max_days=days)
            publish_history(public_dir, max_days=365)
            return jsonify(payload)
        except Exception as e:
            log.exception("[History] GET /history failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/attribution", methods=["GET"])
    def get_attribution_route():
        from pnl_attribution import get_attribution
        try:
            days = int(request.args.get("days", 90))
            days = max(7, min(days, 365))
            return jsonify(get_attribution(max_days=days))
        except Exception as e:
            log.exception("[Attribution] GET /attribution failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/signal-performance", methods=["GET"])
    def get_signal_performance_route():
        from signal_performance import get_signal_performance_report
        try:
            return jsonify(get_signal_performance_report())
        except Exception as e:
            log.exception("[SignalPerformance] GET /signal-performance failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/validation/performance", methods=["GET"])
    def get_validation_performance():
        from agenttrade.performance import compute_performance_metrics
        try:
            days = int(request.args.get("days", 90))
            return jsonify(compute_performance_metrics(max_days=days))
        except Exception as e:
            return jsonify({"status": "insufficient data", "error": str(e)}), 500

    @app.route("/export/summary", methods=["GET"])
    def export_summary_csv():
        """Performance summary CSV (9 core metrics)."""
        from report_export import performance_summary_csv
        from flask import Response
        try:
            days = int(request.args.get("days", 90))
            days = max(7, min(days, 365))
            body = performance_summary_csv(max_days=days)
            fname = f"agenttrade_performance_{days}d.csv"
            return Response(
                body,
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )
        except Exception as e:
            log.exception("[Export] GET /export/summary failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/export/full", methods=["GET"])
    def export_full_csv():
        """Full report CSV: summary, daily, trades, attribution."""
        from report_export import full_report_csv
        from flask import Response
        try:
            days = int(request.args.get("days", 90))
            days = max(7, min(days, 365))
            body = full_report_csv(max_days=days)
            fname = f"agenttrade_report_{days}d.csv"
            return Response(
                body,
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )
        except Exception as e:
            log.exception("[Export] GET /export/full failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/trades", methods=["GET"])
    def get_trades_route():
        """Fill history for Reports trade log."""
        from trade_log import get_trades, sync_trade_log, publish_trade_log
        try:
            days = int(request.args.get("days", 90))
            days = max(7, min(days, 365))
            sync_days = max(7, min(days, 30))
            sync_trade_log(days=sync_days)
            payload = get_trades(max_days=days)
            public_dir = os.getenv("PUBLIC_DASHBOARD_DIR", "/var/www/my_webapp__3/www")
            publish_trade_log(public_dir)
            return jsonify(payload)
        except Exception as e:
            log.exception("[Trades] GET /trades failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/trades/sync", methods=["POST"])
    def post_trades_sync():
        from trade_log import sync_trade_log, publish_trade_log
        try:
            days = int(request.args.get("days", 90))
            days = max(7, min(days, 365))
            result = sync_trade_log(days=days)
            if result.get("ok"):
                publish_trade_log(os.getenv("PUBLIC_DASHBOARD_DIR", "/var/www/my_webapp__3/www"))
            status = 200 if result.get("ok") else 400
            return jsonify(result), status
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/history/backfill", methods=["POST"])
    def post_history_backfill():
        """Import daily equity from Alpaca into performance_history.jsonl."""
        from performance_history import backfill_from_alpaca, publish_history
        try:
            days = int(request.args.get("days", 90))
            days = max(7, min(days, 365))
            result = backfill_from_alpaca(days=days)
            if result.get("ok"):
                public_dir = os.getenv("PUBLIC_DASHBOARD_DIR", "/var/www/my_webapp__3/www")
                publish_history(public_dir)
            status = 200 if result.get("ok") else 400
            return jsonify(result), status
        except Exception as e:
            log.exception("[History] POST /history/backfill failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/ledger/consecutive-losses", methods=["GET"])
    def get_consecutive_losses():
        """Deterministic consecutive loss detail from completed_trades."""
        from agenttrade import db as ledger
        try:
            ledger.init_db()
            status = ledger.get_consecutive_loss_status(limit=20)
            threshold = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
            return jsonify({
                "ok": True,
                "count": status.get("count", 0),
                "threshold": threshold,
                "paused": ledger.trading_paused(),
                "trades": status.get("trades", []),
                "latest_winner": status.get("latest_winner"),
                "calculated_at": status.get("calculated_at"),
                "source": status.get("source", "completed_trades"),
                "ledger_complete": bool(status.get("ledger_complete")),
                "message": status.get("message", ""),
            })
        except Exception as e:
            log.exception("[Ledger] GET /ledger/consecutive-losses failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/ledger/completed-trades", methods=["GET"])
    def get_completed_trades_route():
        """Recent completed trades from the FIFO lot ledger."""
        from agenttrade import db as ledger
        try:
            ledger.init_db()
            limit = int(request.args.get("limit", 50))
            start = request.args.get("start")
            end = request.args.get("end")
            trades = ledger.get_completed_trades(limit=limit, start_date=start, end_date=end)
            return jsonify({"ok": True, "trades": trades, "count": len(trades)})
        except Exception as e:
            log.exception("[Ledger] GET /ledger/completed-trades failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/ledger/rebuild", methods=["POST"])
    def post_ledger_rebuild():
        """Rebuild the FIFO lot ledger from raw fills."""
        from agenttrade.rebuild_ledger import run_rebuild
        try:
            body = request.get_json(silent=True, force=True) or {}
            dry_run = bool(body.get("dry_run", False))
            fetch_alpaca = bool(body.get("fetch_alpaca", False))
            result = run_rebuild(dry_run=dry_run, fetch_alpaca=fetch_alpaca)
            return jsonify({"ok": not result.get("errors"), **result})
        except Exception as e:
            log.exception("[Ledger] POST /ledger/rebuild failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/health", methods=["GET"])
    def get_health():
        from health_check import run_health_check
        try:
            os.environ.setdefault("TRADING_AGENT_DIR", APP_ROOT)
            return jsonify(run_health_check(app_root=APP_ROOT))
        except Exception as e:
            log.exception("[Health] GET /health failed")
            return jsonify({"ok": False, "message": str(e), "checks": []}), 200

    @app.route("/screener/cache", methods=["GET"])
    def get_screener_cache():
        try:
            apply_config_to_env()
            from screener_cache import stats
            return jsonify({"ok": True, **stats()})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/screener/cache/clear", methods=["POST"])
    def clear_screener_cache():
        try:
            apply_config_to_env()
            from screener_cache import clear
            removed = clear()
            return jsonify({"ok": True, "message": f"Cleared {removed} cached universe(s)", "removed": removed})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/backtest", methods=["GET"])
    def get_backtest():
        """Run rule-based backtest (may take 30–90s for 90d). Query: days, capital, interval."""
        try:
            apply_config_to_env()
            from backtest_engine import default_date_range, run_backtest

            days = int(request.args.get("days", 90))
            days = max(30, min(days, 365))
            capital = float(request.args.get("capital", 10000))
            interval = int(request.args.get("interval", 5))
            start, end = default_date_range(days)
            if request.args.get("start") and request.args.get("end"):
                start = request.args.get("start")
                end = request.args.get("end")

            result = run_backtest(
                start_date=start,
                end_date=end,
                initial_capital=capital,
                cycle_interval=interval,
                universe_limit=min(50, int(request.args.get("universe", 50))),
            )
            # Trim heavy arrays for API response
            if result.get("ok"):
                result["equity_curve"] = (result.get("equity_curve") or [])[-60:]
                result["trades"] = (result.get("trades") or [])[-100:]
            return jsonify(result)
        except Exception as e:
            log.exception("[Backtest] GET /backtest failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    # ── Manual action endpoints ──────────────────────────────────────────────

    @app.route("/action/set-stop", methods=["POST"])
    def post_action_set_stop():
        """
        Place a GTC sell stop order on Alpaca for an existing position,
        then persist the manual stop override in the SQLite system-flags ledger.

        Body: {symbol, stop_price, qty? (defaults to full position)}
        """
        try:
            body = request.get_json(silent=True, force=True) or {}
            symbol     = str(body.get("symbol", "")).strip().upper()
            stop_price = float(body.get("stop_price", 0) or 0)
            if not symbol or stop_price <= 0:
                return jsonify({"ok": False, "message": "symbol and stop_price required"}), 400

            import agent_config as cfg
            from alpaca_client import alpaca_post, get_positions
            cfg.refresh_config()

            # Resolve qty: use caller-supplied qty or full position qty
            qty_str = str(body.get("qty", "")).strip()
            if not qty_str:
                positions = get_positions()
                pos = next((p for p in positions if p.get("symbol") == symbol), None)
                if not pos:
                    return jsonify({"ok": False,
                        "message": f"No open position found for {symbol}"}), 400
                qty_raw = float(pos.get("qty", 0) or pos.get("quantity", 0))
                if qty_raw <= 0:
                    return jsonify({"ok": False,
                        "message": f"Position qty is 0 for {symbol}"}), 400
                # Use integer qty for equities, up to 6 decimal places for crypto
                asset_class = pos.get("asset_class", "us_equity")
                if "crypto" in asset_class.lower():
                    qty_str = str(round(qty_raw, 6))
                else:
                    qty_str = str(int(qty_raw))

            order = alpaca_post("/v2/orders", {
                "symbol":        symbol,
                "qty":           qty_str,
                "side":          "sell",
                "type":          "stop",
                "time_in_force": "gtc",
                "stop_price":    str(round(stop_price, 4)),
            })

            # Durable manual override belongs in SQLite; agent_state.json is projection-only.
            try:
                from agenttrade import db as ledger
                ledger.init_db()
                ledger.set_manual_stop_price(symbol, stop_price)
            except Exception as persist_err:
                log.warning("[SetStop] Could not persist manual stop to SQLite: %s", persist_err)

            return jsonify({
                "ok":         True,
                "message":    f"Stop order placed for {symbol} at ${stop_price:.4f} (qty {qty_str})",
                "stop_price": stop_price,
                "order_id":   order.get("id"),
            })
        except Exception as e:
            log.exception("[Action] set-stop failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/action/calc-stop", methods=["POST"])
    def post_action_calc_stop():
        """
        Calculate a suggested stop price for a symbol using the ATR engine
        (same logic as the buy pipeline).  No orders are placed.

        Body: {symbol}
        Returns: {stop_price, stop_source, atr, entry_price}
        """
        try:
            body   = request.get_json(silent=True, force=True) or {}
            symbol = str(body.get("symbol", "")).strip().upper()
            if not symbol:
                return jsonify({"ok": False, "message": "symbol required"}), 400

            import agent_config as cfg
            from alpaca_client import get_positions
            cfg.refresh_config()

            positions = get_positions()
            pos = next((p for p in positions if p.get("symbol") == symbol), None)
            if not pos:
                return jsonify({"ok": False, "message": f"No open position for {symbol}"}), 400

            price = float(pos.get("current_price") or pos.get("avg_entry_price") or 0)
            entry = float(pos.get("avg_entry_price") or price)
            if price <= 0:
                return jsonify({"ok": False, "message": "Cannot resolve current price"}), 400

            # Use the ATR engine from risk.py
            from agenttrade.risk import compute_stop_price
            from buckets import BucketManager
            bm = BucketManager()
            tags = bm._load_tags()
            bucket_name = tags.get(symbol)
            bucket = None
            for b in bm.buckets:
                if b.name == bucket_name:
                    bucket = b
                    break

            decision = {
                "ticker": symbol, "current_price": price, "price": price,
                "atr": body.get("atr"),  # caller may supply ATR
            }
            stop = compute_stop_price(decision, bucket=bucket)
            stop_source = decision.get("stop_source", "fallback_flat_pct")

            # Fallback if ATR engine returns None
            if not stop:
                fb_pct = getattr(bucket, "stop_loss_pct", 0.07) if bucket else 0.07
                stop = round(entry * (1 - float(fb_pct)), 4)
                stop_source = "fallback_flat_pct"

            return jsonify({
                "ok":          True,
                "symbol":      symbol,
                "stop_price":  round(stop, 4),
                "stop_source": stop_source,
                "atr":         decision.get("atr"),
                "entry_price": round(entry, 4),
                "current_price": round(price, 4),
                "bucket":      bucket_name or "Unassigned",
            })
        except Exception as e:
            log.exception("[Action] calc-stop failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/action/reset-pause", methods=["POST"])
    def post_action_reset_pause():
        try:
            from agenttrade.risk import reset_trading_pause
            from agenttrade import db as ledger
            ledger.init_db()
            body = request.get_json(silent=True, force=True) or {}
            reason = body.get("reason", "Manual reset via dashboard")
            if not ledger.trading_paused():
                return jsonify({"ok": True, "message": "Trading was not paused — no action needed"})
            reset_trading_pause(reason)
            return jsonify({"ok": True, "message": f"Trading pause cleared: {reason}"})
        except Exception as e:
            log.exception("[Action] reset-pause failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/action/reset-halt", methods=["POST"])
    def post_action_reset_halt():
        try:
            body = request.get_json(silent=True, force=True) or {}
            if not body.get("confirm"):
                return jsonify({"ok": False, "message": "confirm=true required"}), 400
            from agenttrade import db as ledger
            ledger.init_db()
            reason = body.get("reason", "Manual halt reset via dashboard")
            ledger.set_system_flag("TRADING_HALTED", "false")
            ledger.set_system_flag("HALT_REASON", "")
            ledger.insert_risk_event(None, "warn", "TRADING_HALT_RESET", reason)
            return jsonify({"ok": True, "message": f"Trading halt cleared: {reason}"})
        except Exception as e:
            log.exception("[Action] reset-halt failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/action/reconcile", methods=["POST"])
    def post_action_reconcile():
        """
        Re-run the reconciliation gate.  If it passes AND a halt/pause is active,
        auto-clear both flags — the halt was caused by a failed reconciliation, so
        a passing reconciliation is the natural resolution.
        """
        try:
            from agenttrade.reconciliation import run_reconciliation_gate
            from agenttrade import db as ledger
            ledger.init_db()
            latest   = ledger.get_latest_cycle_run()
            cycle_id = (latest or {}).get("id") or 0
            result   = run_reconciliation_gate(cycle_run_id=cycle_id, mode="dashboard_action")

            cleared = []
            if result.passed:
                if ledger.trading_halted():
                    ledger.set_system_flag("TRADING_HALTED", "false")
                    ledger.set_system_flag("HALT_REASON", "")
                    ledger.insert_risk_event(
                        cycle_id, "info", "TRADING_HALT_CLEARED",
                        "Reconciliation passed — trading halt auto-cleared",
                    )
                    cleared.append("TRADING_HALTED")
                    log.info("[Action/reconcile] Trading halt cleared after passing reconciliation")
                if ledger.trading_paused():
                    from agenttrade.risk import reset_trading_pause
                    reset_trading_pause("Reconciliation passed — pause auto-cleared")
                    cleared.append("TRADING_PAUSED")
                    log.info("[Action/reconcile] Trading pause cleared after passing reconciliation")

            suffix = ""
            if cleared:
                suffix = f" — auto-cleared: {', '.join(cleared)}"

            return jsonify({
                "ok":      result.passed,
                "message": ("PASSED" + suffix) if result.passed else f"FAILED: {result.message}",
                "passed":  result.passed,
                "status":  result.status,
                "details": result.message,
                "cleared": cleared,
            })
        except Exception as e:
            log.exception("[Action] reconcile failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/action/manual-trade", methods=["POST"])
    def post_action_manual_trade():
        """Place a market buy or sell order. Body: {symbol, side, qty?, notional?, confirm}."""
        try:
            body = request.get_json(silent=True, force=True) or {}
            if not body.get("confirm"):
                return jsonify({"ok": False, "message": "confirm=true required"}), 400
            symbol = str(body.get("symbol", "")).strip().upper()
            side   = str(body.get("side", "")).strip().lower()
            if not symbol or side not in ("buy", "sell"):
                return jsonify({"ok": False, "message": "symbol and side (buy/sell) required"}), 400
            import agent_config as cfg
            from alpaca_client import alpaca_post, close_position
            cfg.refresh_config()
            qty      = body.get("qty")
            notional = body.get("notional")
            # Full close: no qty/notional specified → use Alpaca's liquidate endpoint
            if not qty and not notional and side == "sell":
                result = close_position(symbol)
                return jsonify({"ok": True, "message": f"{symbol} full position closed", "order": result})
            order = {
                "symbol": symbol, "side": side,
                "type": "market", "time_in_force": "day",
            }
            if qty:
                order["qty"] = str(qty)
            elif notional:
                order["notional"] = str(notional)
            result = alpaca_post("/v2/orders", order)
            return jsonify({"ok": True, "message": f"{side.upper()} order placed for {symbol}", "order": result})
        except Exception as e:
            log.exception("[Action] manual-trade failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/action/margin-correction", methods=["POST"])
    def post_action_margin_correction():
        """
        Smart margin correction: rank positions by sell priority using the same
        weighted signal logic as the buy pipeline (inverted for exit scoring),
        then close worst-scoring holds one-by-one until cash >= 0.

        Sell priority (higher score = sell first):
          45% P/L performance  — biggest losers sell first
          40% Signal weakness  — same PIPELINE_WEIGHTS as screener, inverted
          15% Stop proximity   — at/near stop sells first
        """
        try:
            body = request.get_json(silent=True, force=True) or {}
            if not body.get("confirm"):
                return jsonify({"ok": False, "message": "confirm=true required"}), 400

            import agent_config as cfg
            from alpaca_client import get_account, get_positions, close_position
            from margin_correction import rank_positions_for_margin_correction
            cfg.refresh_config()

            account = get_account()
            cash    = float(account.get("cash", 0))
            if cash >= 0:
                return jsonify({"ok": True,
                    "message": f"Cash already positive (${cash:,.2f}) — no correction needed"})

            positions = get_positions()
            if not positions:
                return jsonify({"ok": False, "message": "No positions to liquidate"})

            # Rank using signal-weighted logic (worst holds first)
            ranked  = rank_positions_for_margin_correction(positions)
            sold    = []
            skipped = []
            deficit = abs(cash)

            for pos in ranked:
                if deficit <= 0:
                    break
                sym    = pos.get("symbol", "")
                mv     = float(pos.get("market_value") or 0)
                score  = pos.get("sell_score", 0)
                detail = pos.get("score_detail", {})
                if not sym or mv <= 0:
                    continue
                try:
                    close_position(sym)
                    sold.append({
                        "symbol":          sym,
                        "market_value":    round(mv, 2),
                        "sell_score":      round(score, 4),
                        "pl_pct":          detail.get("pl_pct"),
                        "signal_weakness": detail.get("signal_weakness"),
                        "status":          "closed",
                    })
                    deficit -= mv
                    log.info(
                        "[MarginCorrection] Closed %s (score=%.3f, pl=%.1f%%, mv=$%.0f) — deficit $%.0f remaining",
                        sym, score, detail.get("pl_pct", 0), mv, max(0, deficit),
                    )
                except Exception as sell_err:
                    skipped.append({"symbol": sym, "error": str(sell_err)})
                    log.warning("[MarginCorrection] Could not close %s: %s", sym, sell_err)

            kept = [
                {"symbol": p.get("symbol"), "sell_score": p.get("sell_score"),
                 "pl_pct": p.get("score_detail", {}).get("pl_pct")}
                for p in ranked if p.get("symbol") not in {s["symbol"] for s in sold}
            ]

            return jsonify({
                "ok":                  True,
                "message":             f"Closed {len(sold)} position(s) to recover ~${abs(cash):,.2f} deficit",
                "cash_deficit_before": round(abs(cash), 2),
                "positions_closed":    sold,
                "positions_kept":      kept,
                "skipped_errors":      skipped,
            })
        except Exception as e:
            log.exception("[Action] margin-correction failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route("/status", methods=["GET"])
    def get_status():
        try:
            from agenttrade import db as ledger
            ledger.init_db()
            cycle = ledger.get_latest_cycle_run() or {}
            artifacts = ledger.get_latest_funnel().get("artifacts") or {}
            tok = artifacts.get("token_usage") or {}
            acct = ledger.get_latest_account_snapshot() or {}
            return jsonify({
                "ok":          True,
                "last_run":    artifacts.get("last_run") or cycle.get("finished_at") or cycle.get("started_at"),
                "llm_mode":    artifacts.get("llm_mode") or cycle.get("mode"),
                "portfolio":   acct.get("portfolio_value") or acct.get("equity"),
                "tokens_used": tok.get("total_tokens", 0),
                "cost_today":  tok.get("total_cost_usd", 0),
                "budget":      tok.get("budget", 200000),
            })
        except Exception:
            # NON-AUTHORITATIVE fallback: projection cache
            try:
                with open("agent_state.json") as f:
                    state = json.load(f)
                tok = state.get("token_usage", {})
                return jsonify({
                    "ok":          True,
                    "last_run":    state.get("last_run"),
                    "llm_mode":    state.get("llm_mode"),
                    "portfolio":   state.get("portfolio_value"),
                    "tokens_used": tok.get("total_tokens", 0),
                    "cost_today":  tok.get("total_cost_usd", 0),
                    "budget":      tok.get("budget", 200000),
                })
            except Exception:
                return jsonify({"ok": False, "message": "status unavailable"})

    @app.route("/action/ledger-rebuild", methods=["POST"])
    def action_ledger_rebuild():
        """Rebuild the FIFO lot ledger from raw fills (dashboard action)."""
        from agenttrade.rebuild_ledger import run_rebuild
        try:
            body = request.get_json(silent=True, force=True) or {}
            result = run_rebuild(dry_run=False, fetch_alpaca=False)
            ok = not result.get("errors")
            return jsonify({
                "ok": ok,
                "message": f"Ledger rebuild {'complete' if ok else 'completed with errors'}: "
                           f"{result.get('buy_lots_created',0)} lots, "
                           f"{result.get('completed_trades_created',0)} completed trades",
                **result,
            })
        except Exception as e:
            log.exception("[Ledger] POST /action/ledger-rebuild failed")
            return jsonify({"ok": False, "message": str(e)}), 500

    return app


def start_server(port: int = PORT, debug: bool = False):
    """Start the config server in a background thread."""
    app = create_app()

    def run():
        import logging as lg
        lg.getLogger("werkzeug").setLevel(lg.WARNING)
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    log.info(f"[Config Server] Running at http://127.0.0.1:{port}")
    return t


# ── Entry point (standalone) ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(f"\n🔧 Config server starting on http://127.0.0.1:{PORT}\n")
    print(f"   Open settings.html in your browser to manage API keys.\n")
    app = create_app()
    app.run(host="127.0.0.1", port=PORT, debug=True)
