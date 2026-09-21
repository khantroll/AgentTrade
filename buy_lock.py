"""Post-sell buy lock — scoped cooldowns after sells (symbol > bucket > asset-class)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import agent_config as cfg

log = logging.getLogger(__name__)

_FILL_ID_CAP = 300
_LOCK_CAP = 200


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("+00:00", ""))
    except (TypeError, ValueError):
        return None


def infer_asset_class(symbol: Optional[str]) -> str:
    """Infer asset class from ticker format (crypto pairs use /USD etc.)."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return "us_equity"
    if "/" in sym or sym.endswith("USD") and len(sym) > 3:
        return "crypto"
    return "us_equity"


def _lookup_bucket(symbol: Optional[str]) -> Optional[str]:
    if not symbol:
        return None
    try:
        from buckets import BucketManager
        tags = BucketManager()._load_tags() or {}
        return tags.get(symbol) or tags.get(str(symbol).upper())
    except Exception:
        return None


_LOCK_PERSIST_KEYS = (
    "last_sell_at",
    "last_sell_symbols",
    "symbol_locks",
    "daily_sells_today",
    "daily_sells_date",
    "daily_sells_placed_today",
    "daily_sells_placed_date",
    "emergency_rebalance_at",
    "processed_fill_ids",
)


def load_prior_state() -> dict:
    """Load buy-lock continuity from SQLite; JSON is non-authoritative fallback."""
    sqlite_state: dict = {}
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            sqlite_state = ledger.get_buy_lock_state() or {}
    except Exception:
        sqlite_state = {}

    json_state: dict = {}
    try:
        with open(cfg.STATE_FILE, encoding="utf-8") as f:
            json_state = json.load(f)
    except (OSError, json.JSONDecodeError):
        json_state = {}

    out: dict = {}
    for key in _LOCK_PERSIST_KEYS:
        if json_state.get(key) not in (None, "", [], {}):
            out[key] = json_state[key]
    for key in _LOCK_PERSIST_KEYS:
        if key in sqlite_state and sqlite_state[key] not in (None,):
            out[key] = sqlite_state[key]
    return out


def _today_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _fill_id(fill: dict) -> str:
    return str(
        fill.get("id")
        or fill.get("order_id")
        or f"{fill.get('submitted_at')}|{fill.get('ticker')}|{fill.get('side')}|{fill.get('shares')}"
    )


def _end_of_day(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time())


def _reset_daily_sell_counters(ctx: dict, now: datetime) -> dict:
    today = _today_key(now)
    if ctx.get("daily_sells_date") != today:
        ctx["daily_sells_today"] = 0
        ctx["daily_sells_date"] = today
    if ctx.get("daily_sells_placed_date") != today:
        ctx["daily_sells_placed_today"] = 0
        ctx["daily_sells_placed_date"] = today
    if ctx.get("emergency_rebalance_at"):
        er = _parse_iso(ctx["emergency_rebalance_at"])
        if er and er.date() != now.date():
            ctx.pop("emergency_rebalance_at", None)
    return ctx


def _prune_expired_locks(ctx: dict, now: datetime) -> list[dict]:
    """Return active locks; drop expired entries from ctx."""
    locks = list(ctx.get("symbol_locks") or [])
    active: list[dict] = []
    for lock in locks:
        unlock = _parse_iso(lock.get("unlock_at"))
        if unlock and now < unlock:
            active.append(lock)
    ctx["symbol_locks"] = active[-_LOCK_CAP:]
    return active


def _upsert_symbol_lock(
    ctx: dict,
    *,
    symbol: str,
    asset_class: str,
    unlock_at: datetime,
    reason: str,
    scope: str = "symbol",
    bucket: Optional[str] = None,
    locked_at: Optional[datetime] = None,
) -> dict:
    """Add or extend a scoped lock for one symbol."""
    ctx = dict(ctx or {})
    now = locked_at or datetime.now()
    sym = str(symbol or "").upper().strip()
    if not sym:
        return ctx

    locks = list(ctx.get("symbol_locks") or [])
    unlock_iso = unlock_at.isoformat()
    locked_iso = now.isoformat()
    ac = asset_class or infer_asset_class(sym)
    bucket = bucket or _lookup_bucket(sym)

    merged = False
    for lock in locks:
        if lock.get("symbol") != sym:
            continue
        existing_unlock = _parse_iso(lock.get("unlock_at"))
        if existing_unlock and unlock_at > existing_unlock:
            lock["unlock_at"] = unlock_iso
        lock["reason"] = reason
        lock["scope"] = scope
        lock["asset_class"] = ac
        if bucket:
            lock["bucket"] = bucket
        merged = True
        break

    if not merged:
        entry: dict[str, Any] = {
            "symbol": sym,
            "asset_class": ac,
            "scope": scope,
            "reason": reason,
            "locked_at": locked_iso,
            "unlock_at": unlock_iso,
        }
        if bucket:
            entry["bucket"] = bucket
        locks.append(entry)

    ctx["symbol_locks"] = locks[-_LOCK_CAP:]
    return ctx


def _add_sell_locks_from_symbols(
    ctx: dict,
    symbols: list[str],
    *,
    now: datetime,
    reason: str,
    unlock_at: datetime,
    scope: str = "symbol",
    buckets: Optional[dict[str, str]] = None,
) -> dict:
    for sym in symbols:
        if not sym:
            continue
        ac = infer_asset_class(sym)
        bucket = (buckets or {}).get(sym) or _lookup_bucket(sym)
        ctx = _upsert_symbol_lock(
            ctx,
            symbol=sym,
            asset_class=ac,
            unlock_at=unlock_at,
            reason=reason,
            scope=scope,
            bucket=bucket,
            locked_at=now,
        )
    return ctx


def sync_sell_fills(ctx: dict, fills: list, now: Optional[datetime] = None) -> dict:
    """Detect new Alpaca sell fills and add per-symbol buy locks."""
    now = now or datetime.now()
    ctx = dict(ctx or {})
    ctx = _reset_daily_sell_counters(ctx, now)

    seen = set(ctx.get("processed_fill_ids") or [])
    daily_sells = int(ctx.get("daily_sells_today") or 0)
    last_sell_at = ctx.get("last_sell_at")
    last_sell_symbols: list[str] = list(ctx.get("last_sell_symbols") or [])
    cooldown = timedelta(hours=cfg.POST_SELL_COOLDOWN_HOURS)

    for fill in fills or []:
        if str(fill.get("side", "")).lower() != "sell":
            continue
        fid = _fill_id(fill)
        if fid in seen:
            continue

        seen.add(fid)
        daily_sells += 1
        sym = fill.get("ticker") or fill.get("symbol") or ""
        sym_u = str(sym).upper()
        if sym_u and sym_u not in last_sell_symbols:
            last_sell_symbols.append(sym_u)

        fill_time = _parse_iso(fill.get("submitted_at") or fill.get("filled_at")) or now
        fill_iso = fill_time.isoformat()
        if not last_sell_at or fill_iso > last_sell_at:
            last_sell_at = fill_iso

        ac = fill.get("asset_class") or infer_asset_class(sym_u)
        unlock_at = fill_time + cooldown
        ctx = _upsert_symbol_lock(
            ctx,
            symbol=sym_u,
            asset_class=ac,
            unlock_at=unlock_at,
            reason="recent_sell",
            scope="symbol",
            locked_at=fill_time,
        )

        log.info(
            "[BuyLock] Sell fill detected: %s (%s) qty=%s — symbol lock until %s",
            sym_u,
            ac,
            fill.get("shares") or fill.get("qty"),
            unlock_at.isoformat(),
        )

    ctx["processed_fill_ids"] = list(seen)[-_FILL_ID_CAP:]
    ctx["daily_sells_today"] = daily_sells
    ctx["daily_sells_date"] = _today_key(now)
    ctx["last_sell_at"] = last_sell_at
    ctx["last_sell_symbols"] = last_sell_symbols
    return ctx


def record_sells_placed(ctx: dict, orders: list, now: Optional[datetime] = None) -> dict:
    """Track sell orders placed this cycle and add per-symbol locks."""
    now = now or datetime.now()
    ctx = dict(ctx or {})
    ctx = _reset_daily_sell_counters(ctx, now)

    placed = [
        o for o in (orders or [])
        if str(o.get("side", "")).lower() == "sell" and o.get("status") == "placed"
    ]
    if not placed:
        return ctx

    ctx["daily_sells_placed_today"] = int(ctx.get("daily_sells_placed_today") or 0) + len(placed)
    ctx["daily_sells_placed_date"] = _today_key(now)

    symbols: list[str] = []
    buckets: dict[str, str] = {}
    for o in placed:
        sym = o.get("ticker") or o.get("symbol") or ""
        sym_u = str(sym).upper()
        if sym_u:
            symbols.append(sym_u)
            if o.get("bucket"):
                buckets[sym_u] = o["bucket"]

    unlock_at = now + timedelta(hours=cfg.POST_SELL_COOLDOWN_HOURS)
    ctx = _add_sell_locks_from_symbols(
        ctx, symbols, now=now, reason="recent_sell", unlock_at=unlock_at, buckets=buckets,
    )

    last_sell_symbols = list(ctx.get("last_sell_symbols") or [])
    for sym in symbols:
        if sym not in last_sell_symbols:
            last_sell_symbols.append(sym)
    ctx["last_sell_symbols"] = last_sell_symbols
    ctx["last_sell_at"] = now.isoformat()
    return ctx


def record_emergency_rebalance(ctx: dict, hard_orders: list, now: Optional[datetime] = None) -> dict:
    """Lock only the symbols sold during emergency rebalance (not all asset classes)."""
    if not cfg.EMERGENCY_REBALANCE_BUY_LOCK:
        return ctx

    sells = [
        o for o in (hard_orders or [])
        if str(o.get("side", "")).lower() == "sell" and o.get("status") == "placed"
    ]
    if not sells:
        return ctx

    now = now or datetime.now()
    ctx = dict(ctx or {})
    ctx["emergency_rebalance_at"] = now.isoformat()
    ctx = record_sells_placed(ctx, sells, now)

    unlock_eod = _end_of_day(now)
    symbols = [str(o.get("ticker") or o.get("symbol") or "").upper() for o in sells]
    ctx = _add_sell_locks_from_symbols(
        ctx,
        [s for s in symbols if s],
        now=now,
        reason="emergency_rebalance",
        unlock_at=unlock_eod,
        scope="symbol",
    )
    log.info(
        "[BuyLock] Emergency rebalance — symbol locks for %s until %s",
        ", ".join(s for s in symbols if s),
        unlock_eod.isoformat(),
    )
    return ctx


def is_buy_locked(
    symbol: Optional[str] = None,
    asset_class: Optional[str] = None,
    bucket: Optional[str] = None,
    buy_lock: Optional[dict] = None,
    ctx: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> tuple[bool, str, Optional[dict]]:
    """
    Check whether a specific buy is blocked by a scoped lock.

    Priority: global (buying_disabled) > symbol > bucket > asset_class.
    Returns (locked, reason_code, matching_lock_or_None).
    """
    now = now or datetime.now()

    if not cfg.BUYING_ENABLED:
        return True, "buying_disabled", {"scope": "global", "reason": "buying_disabled"}

    if cfg.ALLOW_SAME_DAY_REBUY:
        return False, "", None

    active: list[dict] = []
    if buy_lock and buy_lock.get("locks"):
        for lock in buy_lock["locks"]:
            unlock = _parse_iso(lock.get("unlock_at"))
            if unlock and now < unlock:
                active.append(lock)
    elif ctx:
        active = _prune_expired_locks(dict(ctx), now)
    elif buy_lock:
        active = list(buy_lock.get("locks") or [])

    sym = str(symbol or "").upper().strip() if symbol else ""
    ac = asset_class or (infer_asset_class(sym) if sym else None)
    bucket_name = bucket

    # Symbol-level lock
    if sym:
        for lock in active:
            if lock.get("symbol") == sym:
                return True, lock.get("reason") or "recent_sell", lock

    # Bucket-level lock (only locks with scope=bucket and matching bucket, no symbol)
    if bucket_name:
        for lock in active:
            if lock.get("scope") == "bucket" and lock.get("bucket") == bucket_name and not lock.get("symbol"):
                return True, lock.get("reason") or "bucket_lock", lock

    # Asset-class-level lock (only explicit scope=asset_class entries)
    if ac:
        for lock in active:
            if (
                lock.get("scope") == "asset_class"
                and lock.get("asset_class") == ac
                and not lock.get("symbol")
            ):
                return True, lock.get("reason") or "asset_class_lock", lock

    return False, "", None


def _build_lock_message(active: list[dict], scope: str) -> str:
    """Human-readable banner message for the active lock set."""
    if scope == "global":
        return "GLOBAL BUY LOCK: Buying disabled (BUYING_ENABLED=false)"

    if not active:
        return ""

    symbols = sorted({l["symbol"] for l in active if l.get("symbol")})
    buckets = sorted({l["bucket"] for l in active if l.get("bucket") and l.get("scope") == "bucket"})
    asset_classes = sorted({
        l["asset_class"] for l in active
        if l.get("asset_class") and l.get("scope") == "asset_class" and not l.get("symbol")
    })

    parts: list[str] = []
    if scope == "symbol" or symbols:
        sym_str = ", ".join(symbols[:12])
        if len(symbols) > 12:
            sym_str += f" (+{len(symbols) - 12} more)"
        parts.append(f"SYMBOL LOCK: {sym_str}")
    if buckets:
        parts.append(f"BUCKET LOCK: {', '.join(buckets)}")
    if asset_classes:
        ac_labels = {"us_equity": "equity", "crypto": "crypto"}
        parts.append(
            "ASSET-CLASS LOCK: "
            + ", ".join(ac_labels.get(a, a) for a in asset_classes)
        )

    if not parts:
        parts.append("BUY LOCK ACTIVE: Recent sells")
    return " — ".join(parts)


def _migrate_legacy_locks(ctx: dict, now: datetime) -> dict:
    """Rebuild per-symbol locks from legacy global-lock state (pre-refactor deploys)."""
    if ctx.get("symbol_locks"):
        return ctx
    if cfg.ALLOW_SAME_DAY_REBUY:
        return ctx

    last_sell_at = _parse_iso(ctx.get("last_sell_at"))
    if not last_sell_at:
        return ctx

    cooldown = timedelta(hours=cfg.POST_SELL_COOLDOWN_HOURS)
    if now >= last_sell_at + cooldown:
        return ctx

    symbols = list(ctx.get("last_sell_symbols") or [])
    if not symbols and int(ctx.get("daily_sells_today") or 0) > 0:
        return ctx
    if not symbols:
        return ctx

    unlock_at = last_sell_at + cooldown
    return _add_sell_locks_from_symbols(
        ctx,
        symbols,
        now=last_sell_at,
        reason="recent_sell",
        unlock_at=unlock_at,
    )


def evaluate_buy_lock(ctx: dict, now: Optional[datetime] = None) -> dict:
    """
    Return buy-lock status for dashboard and order gates.

    Locks are scoped per symbol (default). Global lock only when BUYING_ENABLED=false.
    """
    now = now or datetime.now()
    ctx = _reset_daily_sell_counters(dict(ctx or {}), now)
    ctx = _migrate_legacy_locks(ctx, now)
    active = _prune_expired_locks(ctx, now)

    symbols = sorted({l["symbol"] for l in active if l.get("symbol")})
    buckets = sorted({
        l["bucket"] for l in active
        if l.get("bucket") and l.get("scope") == "bucket"
    })
    asset_classes = sorted({
        l["asset_class"] for l in active
        if l.get("scope") == "asset_class" and not l.get("symbol")
    })

    # Group locks by asset class for dashboard
    locks_by_asset_class: dict[str, list] = {}
    for lock in active:
        ac = lock.get("asset_class") or "us_equity"
        locks_by_asset_class.setdefault(ac, []).append(lock)

    base: dict[str, Any] = {
        "active": False,
        "scope": None,
        "reason": None,
        "message": None,
        "unlock_at": None,
        "locks": active,
        "locked_symbols": symbols,
        "locked_buckets": buckets,
        "locked_asset_classes": asset_classes,
        "locks_by_asset_class": locks_by_asset_class,
        "last_sell_at": ctx.get("last_sell_at"),
        "last_sell_symbols": list(ctx.get("last_sell_symbols") or []),
        "daily_sells_today": int(ctx.get("daily_sells_today") or 0),
        "buying_enabled": cfg.BUYING_ENABLED,
        "selling_enabled": cfg.SELLING_ENABLED,
        "allow_same_day_rebuy": cfg.ALLOW_SAME_DAY_REBUY,
    }

    if not cfg.BUYING_ENABLED:
        return {
            **base,
            "active": True,
            "scope": "global",
            "reason": "buying_disabled",
            "message": _build_lock_message([], "global"),
        }

    if cfg.ALLOW_SAME_DAY_REBUY or not active:
        return base

    # Determine primary scope for banner (most specific active lock type)
    has_symbol = any(l.get("symbol") for l in active)
    has_bucket = any(l.get("scope") == "bucket" for l in active)
    has_ac = any(l.get("scope") == "asset_class" for l in active)
    if has_symbol:
        scope = "symbol"
    elif has_bucket:
        scope = "bucket"
    elif has_ac:
        scope = "asset_class"
    else:
        scope = "symbol"

    # Soonest unlock among active locks (for compact banner)
    unlock_times = [_parse_iso(l.get("unlock_at")) for l in active]
    unlock_times = [u for u in unlock_times if u]
    soonest = min(unlock_times) if unlock_times else None

    reasons = sorted({l.get("reason") or "recent_sell" for l in active})
    reason = reasons[0] if len(reasons) == 1 else "recent_sell"

    return {
        **base,
        "active": True,
        "scope": scope,
        "reason": reason,
        "reasons": reasons,
        "message": _build_lock_message(active, scope),
        "unlock_at": soonest.isoformat() if soonest else None,
    }


def apply_lock_fields_to_state(state: dict, ctx: dict, buy_lock: dict) -> dict:
    """Merge buy-lock tracking fields into cycle state payload."""
    state = dict(state)
    for key in (
        "last_sell_at",
        "last_sell_symbols",
        "symbol_locks",
        "daily_sells_today",
        "daily_sells_date",
        "daily_sells_placed_today",
        "daily_sells_placed_date",
        "emergency_rebalance_at",
        "processed_fill_ids",
    ):
        if key in ctx:
            state[key] = ctx[key]
    state["buy_lock"] = buy_lock
    try:
        from agenttrade import db as ledger
        ledger.set_buy_lock_state(ctx)
    except Exception as e:
        log.debug("[BuyLock] Could not persist lock state to SQLite: %s", e)
    return state
