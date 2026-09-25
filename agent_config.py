"""
agent_config.py — Shared runtime config, paths, and cycle counters.

Loaded once at startup; refreshed via refresh_config() at each cycle.
"""

import json
import logging
import os
from datetime import datetime

from dotenv import load_dotenv

from buckets import BucketManager
from config_server import apply_config_to_env

load_dotenv(override=False)
apply_config_to_env()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(APP_DIR, "agent_state.json")
PUBLIC_DASHBOARD_DIR = os.getenv("PUBLIC_DASHBOARD_DIR", "/var/www/my_webapp__3/www")
PUBLIC_STATE_FILE = os.path.join(PUBLIC_DASHBOARD_DIR, "agent_state.json")

# Dashboard projection cap. Nested escape bloat once wrote a ~1.4GB agent_state.json.
# Refuse that publish instead of replacing the file. SQLite is not involved here
# (a multi-GB sqlite OOM is a separate host/ops follow-up).
# Override with AGENT_STATE_MAX_BYTES. Default is 8 MiB, not gigabytes.
DEFAULT_AGENT_STATE_MAX_BYTES = 8 * 1024 * 1024

# Research funnel: screener → research picks N → confidence filter → analysis on top M
RESEARCH_TOP_N = 5
MAX_ANALYSIS_CANDIDATES = 2

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"

MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "5"))

STRATEGY_AGGRESSION = os.getenv("STRATEGY_AGGRESSION", "balanced").lower()
MAX_BUYS_PER_BUCKET = {"conservative": 1, "balanced": 1, "aggressive": 2}.get(STRATEGY_AGGRESSION, 1)
MIN_CONFIDENCE_FOR_ANALYSIS = {"conservative": 0.70, "balanced": 0.55, "aggressive": 0.45}.get(
    STRATEGY_AGGRESSION, 0.55
)
RESERVE_CASH_PCT = float(os.getenv("RESERVE_CASH_PCT", "0.10"))
ALLOW_MARGIN = os.getenv("ALLOW_MARGIN", "false").lower() == "true"
MAX_BUCKET_OVERWEIGHT = {"conservative": 0.05, "balanced": 0.10, "aggressive": 0.20}.get(
    STRATEGY_AGGRESSION, 0.10
)
ENABLE_CRYPTO = os.getenv("ENABLE_CRYPTO", "0").lower() in ("1", "true", "yes", "on")
CRYPTO_TRADE_24_7 = os.getenv("CRYPTO_TRADE_24_7", "1").lower() in ("1", "true", "yes", "on")
CRYPTO_MIN_NOTIONAL = float(os.getenv("CRYPTO_MIN_NOTIONAL", "10"))

BUYING_ENABLED = True
SELLING_ENABLED = True
ALLOW_SAME_DAY_REBUY = False
POST_SELL_COOLDOWN_HOURS = 24.0
EMERGENCY_REBALANCE_BUY_LOCK = True
MIN_CASH_RESERVE = 5000.0
ALLOW_NEGATIVE_CASH = False

bucket_manager = BucketManager()

daily_trades = 0


def _load_daily_trades() -> int:
    """Load today's trade count from SQLite. JSON cache is non-authoritative fallback."""
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            return int(ledger.count_fills_today())
    except Exception:
        pass
    # NON-AUTHORITATIVE fallback: dashboard projection cache
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("last_run", "")[:10] == datetime.now().strftime("%Y-%m-%d"):
            return int(state.get("daily_trades", 0))
    except Exception:
        pass
    return 0


def refresh_config() -> None:
    """Reload env/config and strategy constants (call at cycle start).

    Safe for cron (single process per flock). Do not run overlapping cycles
    without run_cycle.sh flock — concurrent refresh_config() can race globals.
    """
    global ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER, ALPACA_BASE_URL
    global MAX_DAILY_TRADES, STRATEGY_AGGRESSION, MAX_BUYS_PER_BUCKET
    global MIN_CONFIDENCE_FOR_ANALYSIS, RESERVE_CASH_PCT, ALLOW_MARGIN
    global MAX_BUCKET_OVERWEIGHT, ENABLE_CRYPTO, CRYPTO_TRADE_24_7, CRYPTO_MIN_NOTIONAL
    global BUYING_ENABLED, SELLING_ENABLED, ALLOW_SAME_DAY_REBUY
    global POST_SELL_COOLDOWN_HOURS, EMERGENCY_REBALANCE_BUY_LOCK
    global MIN_CASH_RESERVE, ALLOW_NEGATIVE_CASH

    apply_config_to_env()

    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    ALPACA_BASE_URL = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"
    MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "5"))
    STRATEGY_AGGRESSION = os.getenv("STRATEGY_AGGRESSION", "balanced").lower()
    MAX_BUYS_PER_BUCKET = {"conservative": 1, "balanced": 1, "aggressive": 2}.get(STRATEGY_AGGRESSION, 1)
    MIN_CONFIDENCE_FOR_ANALYSIS = {"conservative": 0.70, "balanced": 0.55, "aggressive": 0.45}.get(
        STRATEGY_AGGRESSION, 0.55
    )
    RESERVE_CASH_PCT = float(os.getenv("RESERVE_CASH_PCT", "0.10"))
    ALLOW_MARGIN = os.getenv("ALLOW_MARGIN", "false").lower() == "true"
    MAX_BUCKET_OVERWEIGHT = {"conservative": 0.05, "balanced": 0.10, "aggressive": 0.20}.get(
        STRATEGY_AGGRESSION, 0.10
    )
    ENABLE_CRYPTO = os.getenv("ENABLE_CRYPTO", "0").lower() in ("1", "true", "yes", "on")
    CRYPTO_TRADE_24_7 = os.getenv("CRYPTO_TRADE_24_7", "1").lower() in ("1", "true", "yes", "on")
    CRYPTO_MIN_NOTIONAL = float(os.getenv("CRYPTO_MIN_NOTIONAL", "10"))
    BUYING_ENABLED = env_bool("BUYING_ENABLED", "true")
    SELLING_ENABLED = env_bool("SELLING_ENABLED", "true")
    ALLOW_SAME_DAY_REBUY = env_bool("ALLOW_SAME_DAY_REBUY", "false")
    try:
        POST_SELL_COOLDOWN_HOURS = float(os.getenv("POST_SELL_COOLDOWN_HOURS", "24"))
    except ValueError:
        POST_SELL_COOLDOWN_HOURS = 24.0
    EMERGENCY_REBALANCE_BUY_LOCK = env_bool("EMERGENCY_REBALANCE_BUY_LOCK", "true")
    try:
        MIN_CASH_RESERVE = float(os.getenv("MIN_CASH_RESERVE", "5000"))
    except ValueError:
        MIN_CASH_RESERVE = 5000.0
    ALLOW_NEGATIVE_CASH = env_bool("ALLOW_NEGATIVE_CASH", "false")
    sync_crypto_bucket_enabled()


def sync_crypto_bucket_enabled() -> None:
    """Keep Crypto bucket.enabled aligned with runtime ENABLE_CRYPTO."""
    for bucket in bucket_manager.buckets:
        if (
            getattr(bucket, "is_crypto", False)
            or bucket.mode == "crypto"
            or getattr(bucket, "asset_class", "") == "crypto"
        ):
            bucket.enabled = ENABLE_CRYPTO


def init_daily_trades() -> None:
    global daily_trades
    daily_trades = _load_daily_trades()


def increment_daily_trades(count: int = 1) -> None:
    global daily_trades
    daily_trades += count


def reset_daily_counters() -> None:
    global daily_trades
    daily_trades = 0


def env_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).lower() in ("1", "true", "yes", "on")


def agent_state_max_bytes() -> int:
    """Configured max serialized size for agent_state.json. Default 8 MiB."""
    raw = os.getenv("AGENT_STATE_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_AGENT_STATE_MAX_BYTES
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_AGENT_STATE_MAX_BYTES
    if n <= 0:
        return DEFAULT_AGENT_STATE_MAX_BYTES
    return n


def dumps_agent_state(state: dict) -> str | None:
    """Serialize the dashboard projection, or None if it exceeds the size cap.

    Callers must not replace agent_state.json (or the public copy) when this
    returns None. This does not read or write SQLite.
    """
    payload = json.dumps(state, indent=2)
    size = len(payload.encode("utf-8"))
    limit = agent_state_max_bytes()
    if size > limit:
        logging.getLogger("agent_state").error(
            "[State] Refusing agent_state.json write: %s bytes exceeds max %s "
            "(AGENT_STATE_MAX_BYTES, default %s bytes / %s MiB). "
            "Existing projection left unchanged. SQLite history is not modified.",
            f"{size:,}",
            f"{limit:,}",
            f"{DEFAULT_AGENT_STATE_MAX_BYTES:,}",
            DEFAULT_AGENT_STATE_MAX_BYTES // (1024 * 1024),
        )
        return None
    return payload


def hard_rebalance_drift_pct() -> float:
    try:
        return float(os.getenv("HARD_REBALANCE_DRIFT_PCT", "0.10"))
    except ValueError:
        return 0.10


init_daily_trades()
