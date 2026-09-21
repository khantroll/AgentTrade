"""
health_check.py — Pre-cycle readiness checks for dashboard and agent logs.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from app_paths import resolve_app_dir

try:
    import requests
except ImportError:
    requests = None


def _app_dir() -> str:
    return resolve_app_dir()


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_dt(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_ago(dt: Optional[datetime]) -> Optional[float]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _check(status: str, check_id: str, label: str, message: str, **extra) -> dict:
    row = {"id": check_id, "label": label, "status": status, "message": message}
    row.update(extra)
    return row


def _alpaca_check() -> dict:
    try:
        from config_server import apply_config_to_env, test_alpaca
        apply_config_to_env()
    except Exception:
        pass

    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    if not key or not secret:
        return _check("error", "alpaca", "Alpaca API", "API keys not configured — set in Settings")

    result = test_alpaca(key, secret, paper=paper)
    if result.get("ok"):
        return _check("ok", "alpaca", "Alpaca API", result.get("message", "Connected"))
    return _check("error", "alpaca", "Alpaca API", result.get("message", "Connection failed"))


def _market_check() -> dict:
    if requests is None:
        return _check("warn", "market", "Market hours", "requests not installed")

    try:
        from config_server import apply_config_to_env
        apply_config_to_env()
    except Exception:
        pass

    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return _check("info", "market", "Market hours", "Configure Alpaca keys to read clock")

    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
    try:
        r = requests.get(
            f"{base}/v2/clock",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=8,
        )
        r.raise_for_status()
        clock = r.json()
        is_open = bool(clock.get("is_open"))
        nxt = clock.get("next_close") if is_open else clock.get("next_open")
        nxt_short = str(nxt)[:16].replace("T", " ") if nxt else "—"
        crypto_247 = os.getenv("ENABLE_CRYPTO", "true").lower() == "true"
        if is_open:
            msg = f"Market open · closes {nxt_short}"
            return _check("ok", "market", "Market hours", msg, is_open=True)
        msg = f"Market closed · opens {nxt_short}"
        if crypto_247:
            msg += " · crypto buckets may still run"
        return _check("info", "market", "Market hours", msg, is_open=False)
    except Exception as e:
        return _check("warn", "market", "Market hours", str(e))


def _token_budget_check(state: dict) -> dict:
    tok = state.get("token_usage") or {}
    used = int(tok.get("total_tokens") or 0)
    budget = int(tok.get("budget") or os.getenv("DAILY_TOKEN_BUDGET", 200000))
    pct = round(used / budget * 100, 1) if budget else 0
    try:
        from llm_router import budget_exhausted
        exhausted = budget_exhausted()
    except Exception:
        exhausted = used >= budget

    if exhausted:
        return _check(
            "error", "token_budget", "Token budget",
            f"Exhausted — {used:,} / {budget:,} ({pct}%) · cycle will skip",
            pct=pct, used=used, budget=budget,
        )
    if pct >= 80:
        return _check(
            "warn", "token_budget", "Token budget",
            f"High usage — {used:,} / {budget:,} ({pct}%) · may switch to economy",
            pct=pct, used=used, budget=budget,
        )
    return _check(
        "ok", "token_budget", "Token budget",
        f"{used:,} / {budget:,} ({pct}%)",
        pct=pct, used=used, budget=budget,
    )


def _last_cycle_check(state: dict) -> dict:
    last_run = state.get("last_run")
    if not last_run:
        try:
            from agenttrade import db as ledger
            if ledger.db_available():
                ledger.init_db()
                cycle = ledger.get_latest_cycle_run() or {}
                last_run = cycle.get("finished_at") or cycle.get("started_at")
        except Exception:
            last_run = None
    dt = _parse_dt(last_run)
    hours = _hours_ago(dt)
    if not dt:
        return _check("warn", "last_cycle", "Last cycle", "No completed cycle in SQLite ledger")

    age = f"{hours:.1f}h ago" if hours is not None else "unknown"
    msg = f"{dt.strftime('%Y-%m-%d %H:%M')} ({age})"
    if hours is not None and hours > 36:
        return _check("error", "last_cycle", "Last cycle", msg + " · stale", hours=hours)
    if hours is not None and hours > 12:
        return _check("warn", "last_cycle", "Last cycle", msg + " · check cron", hours=hours)
    return _check("ok", "last_cycle", "Last cycle", msg, hours=hours)


def _jsonl_last_line(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    last = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return last


def _jsonl_line_count(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _history_freshness_check() -> dict:
    path = os.path.join(_app_dir(), "performance_history.jsonl")
    row = _jsonl_last_line(path)
    if not row:
        return _check(
            "warn", "history", "Performance history",
            "No snapshots — run a cycle or backfill_history.py",
        )
    ts = row.get("ts") or row.get("date")
    dt = _parse_dt(ts) if "T" in str(ts) else None
    if not dt and row.get("date"):
        try:
            dt = datetime.fromisoformat(str(row["date"])).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    hours = _hours_ago(dt)
    n = _jsonl_line_count(path)
    age = f"{hours:.0f}h ago" if hours is not None else str(ts)
    msg = f"{n} snapshots · latest {age}"
    if hours is not None and hours > 48:
        return _check("warn", "history", "Performance history", msg, count=n, hours=hours)
    return _check("ok", "history", "Performance history", msg, count=n, hours=hours)


def _trade_log_check() -> dict:
    path = os.path.join(_app_dir(), "trade_log.jsonl")
    row = _jsonl_last_line(path)
    if not row:
        return _check(
            "warn", "trade_log", "Trade log",
            "Empty — run backfill_trades.py or wait for next cycle",
        )
    ts = row.get("time") or row.get("date")
    dt = _parse_dt(ts)
    hours = _hours_ago(dt)
    n = _jsonl_line_count(path)
    age = f"{hours:.0f}h ago" if hours is not None else str(ts)[:10]
    msg = f"{n} fills · latest {age}"
    return _check("ok", "trade_log", "Trade log", msg, count=n, hours=hours)


def _llm_keys_check() -> dict:
    try:
        from config_server import apply_config_to_env, load_config
        apply_config_to_env()
        cfg = load_config()
    except Exception:
        cfg = {}

    mode = (cfg.get("LLM_MODE") or os.getenv("LLM_MODE") or "tiered").lower()
    providers = {
        "claude": bool(os.getenv("ANTHROPIC_API_KEY") or cfg.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY") or cfg.get("OPENAI_API_KEY")),
        "gemini": bool(os.getenv("GEMINI_API_KEY") or cfg.get("GEMINI_API_KEY")),
        "mistral": bool(os.getenv("MISTRAL_API_KEY") or cfg.get("MISTRAL_API_KEY")),
        "deepseek": bool(os.getenv("DEEPSEEK_API_KEY") or cfg.get("DEEPSEEK_API_KEY")),
        "groq": bool(os.getenv("GROQ_API_KEY") or cfg.get("GROQ_API_KEY")),
        "nvidia": bool(os.getenv("NVIDIA_API_KEY") or cfg.get("NVIDIA_API_KEY") or os.getenv("NIM_API_KEY")),
    }
    configured = [k for k, v in providers.items() if v]
    if not configured:
        return _check(
            "error", "llm_keys", "LLM keys",
            f"No provider keys configured (mode={mode})",
            mode=mode, providers=configured,
        )
    if len(configured) == 1 and mode == "tiered":
        return _check(
            "warn", "llm_keys", "LLM keys",
            f"Tiered mode with one key ({configured[0]}) — limited voting",
            mode=mode, providers=configured,
        )
    return _check(
        "ok", "llm_keys", "LLM keys",
        f"{len(configured)} providers · mode {mode}",
        mode=mode, providers=configured,
    )


def _llm_provider_health_check() -> dict:
    path = os.path.join(_app_dir(), "llm_health.json")
    data = _load_json(path)
    if not data:
        return _check("ok", "llm_health", "LLM provider health", "No cooldowns recorded")

    now = datetime.now(timezone.utc).timestamp()
    cooling = []
    for prov, rec in data.items():
        if not isinstance(rec, dict):
            continue
        until = float(rec.get("cooldown_until") or 0)
        if until > now:
            mins = int((until - now) / 60)
            cooling.append(f"{prov} ({mins}m)")

    if cooling:
        return _check(
            "warn", "llm_health", "LLM provider health",
            "Cooldown: " + ", ".join(cooling[:4]),
            cooling=cooling,
        )
    return _check("ok", "llm_health", "LLM provider health", "All providers available")


def _agent_state_check() -> dict:
    """SQLite ledger is durable truth; agent_state.json is a disposable projection."""
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            path = ledger.get_db_path()
            cycle = ledger.get_latest_cycle_run()
            msg = f"SQLite ledger at {os.path.basename(path)}"
            if cycle:
                msg += f" · last cycle {cycle.get('status') or 'unknown'}"
            return _check("ok", "agent_state", "Application ledger", msg)
        return _check("warn", "agent_state", "Application ledger", "SQLite ledger not initialized")
    except Exception as e:
        json_path = os.path.join(_app_dir(), "agent_state.json")
        if os.path.exists(json_path):
            return _check(
                "warn", "agent_state", "Application ledger",
                f"SQLite unavailable ({e}); JSON projection cache present (non-authoritative)",
            )
        return _check("error", "agent_state", "Application ledger", str(e))


def _daily_trades_check(state: dict) -> dict:
    daily = int(state.get("daily_trades") or 0)
    limit = 5
    if daily >= limit:
        return _check(
            "warn", "daily_trades", "Daily trade limit",
            f"{daily}/{limit} used · no new orders today",
            daily=daily, limit=limit,
        )
    return _check(
        "ok", "daily_trades", "Daily trade limit",
        f"{daily}/{limit} used",
        daily=daily, limit=limit,
    )


def _hard_rebalance_check(state: dict) -> dict:
    try:
        from config_server import apply_config_to_env
        apply_config_to_env()
    except Exception:
        pass

    enabled = os.getenv("HARD_REBALANCE", "false").lower() in ("1", "true", "yes", "on")
    if not enabled:
        return _check("ok", "hard_rebalance", "Hard rebalance", "Off (soft rebalance only)")

    hb = state.get("hard_rebalance") or {}
    reb = state.get("rebalance") or {}
    trim_buckets = [k for k, r in reb.items() if isinstance(r, dict) and r.get("action") == "TRIM"]
    plans = hb.get("plans") or []
    orders = hb.get("orders") or []

    if plans:
        syms = ", ".join(f"{p.get('symbol')}×{p.get('qty')}" for p in plans[:3])
        msg = f"Enabled · {len(plans)} planned trim(s): {syms}"
        if orders:
            msg += f" · {len(orders)} executed last cycle"
        return _check("warn", "hard_rebalance", "Hard rebalance", msg, plans=len(plans))

    if trim_buckets:
        return _check(
            "ok", "hard_rebalance", "Hard rebalance",
            f"Enabled · {len(trim_buckets)} bucket(s) TRIM but below drift threshold",
            trim_buckets=trim_buckets,
        )
    return _check("ok", "hard_rebalance", "Hard rebalance", "Enabled · no overweight buckets")


def _screener_cache_check() -> dict:
    try:
        from config_server import apply_config_to_env
        apply_config_to_env()
        from screener_cache import stats
        s = stats()
    except Exception as e:
        return _check("warn", "screener_cache", "Screener cache", str(e))

    if not s.get("enabled"):
        return _check("ok", "screener_cache", "Screener cache", "Off — full screener each cycle")

    ttl = s.get("ttl_minutes") or 0
    fresh = int(s.get("fresh_entries") or 0)
    total = int(s.get("entries") or 0)
    if fresh:
        return _check(
            "ok", "screener_cache", "Screener cache",
            f"On · {fresh}/{total} fresh · TTL {ttl:.0f}m",
            ttl_minutes=ttl, fresh=fresh, total=total,
        )
    return _check(
        "ok", "screener_cache", "Screener cache",
        f"On · TTL {ttl:.0f}m · no cached universes yet",
        ttl_minutes=ttl,
    )


def _overall_status(checks: list) -> tuple:
    statuses = [c.get("status") for c in checks]
    if "error" in statuses:
        return "blocked", "Blocked — fix errors before next cycle"
    if "warn" in statuses:
        return "caution", "Caution — review warnings"
    return "ready", "Ready for next cycle"


def run_health_check(app_root: str = None) -> dict:
    if app_root:
        os.environ["TRADING_AGENT_DIR"] = app_root
    root = _app_dir()
    state = {}
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            state = ledger.load_application_state()
    except Exception:
        state = {}
    if not state:
        # NON-AUTHORITATIVE fallback: projection cache
        state_path = os.path.join(root, "agent_state.json")
        state = _load_json(state_path)

    checks = [
        _agent_state_check(),
        _alpaca_check(),
        _market_check(),
        _token_budget_check(state),
        _last_cycle_check(state),
        _llm_keys_check(),
        _llm_provider_health_check(),
        _daily_trades_check(state),
        _hard_rebalance_check(state),
        _screener_cache_check(),
        _history_freshness_check(),
        _trade_log_check(),
    ]

    status, summary = _overall_status(checks)
    return {
        "ok": True,
        "ready": status == "ready",
        "status": status,
        "summary": summary,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


def log_health_summary() -> None:
    """Log compact health summary at cycle start."""
    import logging
    log = logging.getLogger(__name__)
    try:
        h = run_health_check()
        log.info(f"[Health] {h['status'].upper()} — {h['summary']}")
        for c in h.get("checks") or []:
            if c.get("status") in ("error", "warn"):
                log.warning(f"[Health] {c['label']}: {c['message']}")
    except Exception as e:
        log.warning(f"[Health] check failed: {e}")
