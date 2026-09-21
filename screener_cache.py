"""
screener_cache.py — File-backed cache for get_universe() results.

Avoids re-running expensive yFinance, Reddit, NewsAPI, TradingView, and
Alpaca screener pipelines on every bucket/cycle when results are still fresh.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional, Tuple

from app_paths import resolve_app_dir

CACHE_FILE = "screener_cache.json"


def _app_dir() -> str:
    return resolve_app_dir()


def _cache_path() -> str:
    return os.path.join(_app_dir(), CACHE_FILE)


def cache_enabled() -> bool:
    return os.getenv("SCREENER_CACHE", "true").lower() in ("1", "true", "yes", "on")


def cache_ttl_seconds() -> int:
    try:
        minutes = float(os.getenv("SCREENER_CACHE_TTL_MINUTES", "240"))
    except ValueError:
        minutes = 240.0
    return max(0, int(minutes * 60))


def make_cache_key(
    bucket_config: dict,
    use_news_filter: bool,
    use_congress_filter: bool,
    use_reddit_filter: bool,
    max_universe: int,
) -> str:
    cfg = bucket_config or {}
    tickers = cfg.get("tickers") or cfg.get("symbols") or []
    payload = {
        "mode": cfg.get("mode", "growth"),
        "min_price": float(cfg.get("min_price", 5.0)),
        "max_price": float(cfg.get("max_price", 500.0)),
        "min_dividend_yield": float(cfg.get("min_dividend_yield", 0.02)),
        "tickers": sorted(str(t).upper() for t in tickers),
        "use_news": bool(use_news_filter),
        "use_congress": bool(use_congress_filter),
        "use_reddit": bool(use_reddit_filter),
        "max_universe": int(max_universe),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _load_store() -> dict:
    path = _cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_store(store: dict) -> None:
    path = _cache_path()
    fd, tmp = tempfile.mkstemp(prefix="screener_cache_", suffix=".json", dir=_app_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def get(key: str) -> Optional[Tuple[list, dict, float]]:
    """Return (universe, sources, age_seconds) if fresh, else None."""
    if not cache_enabled():
        return None
    ttl = cache_ttl_seconds()
    if ttl <= 0:
        return None

    entry = _load_store().get(key)
    if not entry:
        return None

    try:
        cached_at = datetime.fromisoformat(str(entry["cached_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None

    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - cached_at).total_seconds()
    if age > ttl:
        return None

    universe = entry.get("universe")
    sources = entry.get("sources")
    if not isinstance(universe, list) or not isinstance(sources, dict):
        return None

    return universe, sources, age


def set(key: str, universe: list, sources: dict) -> None:
    if not cache_enabled() or cache_ttl_seconds() <= 0:
        return

    store = _load_store()
    store[key] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "universe": universe,
        "sources": {k: v for k, v in sources.items() if k not in ("cached", "cache_age_min", "cache_key")},
        "meta": {
            "mode": sources.get("mode"),
            "universe_size": len(universe),
        },
    }
    _save_store(store)


def clear() -> int:
    """Remove all cached entries. Returns count removed."""
    store = _load_store()
    count = len(store)
    if count:
        _save_store({})
    return count


def stats() -> dict:
    store = _load_store()
    ttl = cache_ttl_seconds()
    now = datetime.now(timezone.utc)
    entries = []
    for key, entry in store.items():
        if not isinstance(entry, dict):
            continue
        try:
            cached_at = datetime.fromisoformat(str(entry["cached_at"]).replace("Z", "+00:00"))
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            age_min = (now - cached_at).total_seconds() / 60
            fresh = age_min * 60 <= ttl if ttl > 0 else False
        except ValueError:
            age_min = None
            fresh = False
        entries.append({
            "key": key[:12],
            "mode": (entry.get("meta") or {}).get("mode"),
            "size": (entry.get("meta") or {}).get("universe_size"),
            "age_min": round(age_min, 1) if age_min is not None else None,
            "fresh": fresh,
        })
    return {
        "enabled": cache_enabled(),
        "ttl_minutes": ttl / 60 if ttl else 0,
        "entries": len(entries),
        "fresh_entries": sum(1 for e in entries if e.get("fresh")),
        "items": entries[:10],
    }
