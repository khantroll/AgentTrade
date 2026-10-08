"""Post-sell buy lock — scoped cooldowns after sells (symbol > bucket > asset-class)."""

from __future__ import annotations

import json
import logging
import os
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


def _symbol_forms(symbol: str) -> set[str]:
    """``SOL``, ``SOLUSD``, and ``SOL/USD`` are one pair for cooldown matching."""
    try:
        from order_utils import _CRYPTO_USD_BASES, crypto_symbol_forms
        forms = crypto_symbol_forms(symbol)
    except Exception:
        _CRYPTO_USD_BASES = frozenset()
        forms = []
    found = {str(item).upper() for item in forms if item}
    text = "".join(str(symbol or "").upper().split())
    if text:
        found.add(text)
    bare = text.replace("/", "")
    if bare.endswith("USD") and len(bare) > 3:
        found.add(bare[:-3])
    elif text in _CRYPTO_USD_BASES:
        found.add(f"{text}USD")
        found.add(f"{text}/USD")
    return {item for item in found if item}


def symbols_match(left: str, right: str) -> bool:
    return bool(_symbol_forms(left) & _symbol_forms(right))


def sell_fill_is_agenttrade(fill: dict) -> bool:
    """True when this sell was submitted by AgentTrade, including a manual close.

    ``agenttrade-`` and ``agenttrade-manual-`` are ours. A CryptoAgent or
    operator order on the shared account is not, and must not start a cooldown.
    """
    cid = str(fill.get("client_order_id") or "").strip()
    if not cid:
        raw = fill.get("raw_json")
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        if isinstance(raw, dict):
            cid = str(raw.get("client_order_id") or "").strip()
    if cid.startswith("agenttrade-"):
        return True
    oid = str(fill.get("order_id") or fill.get("alpaca_order_id") or "").strip()
    if not oid:
        return False
    try:
        from agenttrade.db import get_connection

        with get_connection() as conn:
            row = conn.execute(
                "SELECT client_order_id FROM orders WHERE alpaca_order_id=? LIMIT 1",
                (oid,),
            ).fetchone()
    except Exception:
        return False
    return bool(row and str(row["client_order_id"] or "").startswith("agenttrade-"))


_LOCK_MATCH_WINDOW = timedelta(hours=36)


def _client_id_kind(client_order_id: str) -> Optional[str]:
    """``ours``, ``foreign``, or None when the id is missing."""
    text = str(client_order_id or "").strip()
    if not text:
        return None
    if text.startswith("agenttrade-"):
        return "ours"
    return "foreign"


def _parse_moment(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        from trading_day import parse_cycle_timestamp
        return parse_cycle_timestamp(value)
    except Exception:
        return None


def _order_near_lock(order: dict, lock_time: Optional[datetime]) -> bool:
    if lock_time is None:
        return True
    moments = []
    for key in ("filled_at", "submitted_at", "created_at", "updated_at", "time", "locked_at"):
        moment = _parse_moment(order.get(key))
        if moment is not None:
            moments.append(moment)
    if not moments:
        return True
    window = _LOCK_MATCH_WINDOW.total_seconds()
    return any(abs((moment - lock_time).total_seconds()) <= window for moment in moments)


def fetch_closed_orders() -> Optional[list]:
    """Recent closed Alpaca orders, or None when the broker cannot be read.

    None means "don't know". An empty list means the broker had no rows.
    A missing sqlite order must not be treated as a foreign sell.
    """
    if not (os.getenv("ALPACA_API_KEY") or getattr(cfg, "ALPACA_API_KEY", None)):
        return None
    try:
        from alpaca_client import get_open_orders
        data = get_open_orders(status="closed", limit=500)
    except Exception as exc:
        log.info("[BuyLock] Closed orders unavailable (%s); keeping unresolved locks", exc)
        return None
    return data if isinstance(data, list) else None


def _sqlite_order_signals(symbol: str) -> set[str]:
    signals: set[str] = set()
    try:
        from agenttrade.db import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT symbol, client_order_id FROM orders
                WHERE client_order_id IS NOT NULL AND client_order_id != ''
                """
            ).fetchall()
    except Exception:
        return signals
    for row in rows:
        if not symbols_match(row["symbol"] or "", symbol):
            continue
        kind = _client_id_kind(row["client_order_id"])
        if kind:
            signals.add(kind)
    return signals


def _jsonl_rows(path: str) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _trade_log_signals(symbol: str, lock_time: Optional[datetime]) -> set[str]:
    """Client ids in trade_log.jsonl, plus order_meta.jsonl rows we wrote."""
    signals: set[str] = set()
    root = getattr(cfg, "APP_DIR", ".")
    for row in _jsonl_rows(os.path.join(root, "trade_log.jsonl")):
        sym = row.get("symbol") or row.get("ticker") or ""
        if not symbols_match(sym, symbol) or not _order_near_lock(row, lock_time):
            continue
        kind = _client_id_kind(row.get("client_order_id"))
        if kind:
            signals.add(kind)
    for row in _jsonl_rows(os.path.join(root, "order_meta.jsonl")):
        sym = row.get("ticker") or row.get("symbol") or ""
        if symbols_match(sym, symbol):
            signals.add("ours")
    return signals


def _closed_order_signals(symbol: str, lock_time: Optional[datetime], closed_orders: Optional[list]) -> set[str]:
    if closed_orders is None:
        return set()
    signals: set[str] = set()
    for order in closed_orders:
        if not isinstance(order, dict):
            continue
        sym = order.get("symbol") or order.get("ticker") or ""
        if str(order.get("side") or "sell").lower() not in ("sell", ""):
            continue
        if not symbols_match(sym, symbol) or not _order_near_lock(order, lock_time):
            continue
        kind = _client_id_kind(order.get("client_order_id"))
        if kind:
            signals.add(kind)
    return signals


def classify_lock(lock: dict, closed_orders: Optional[list] = None) -> str:
    """``ours``, ``foreign``, or ``unknown``.

    Unknown keeps the lock. Only a positive non-AgentTrade client id drops it.
    Sqlite is one source. A sell that never landed in ``orders`` can still be
    ours via the lock record, trade_log.jsonl, or the Alpaca order.
    """
    lock = lock or {}
    symbol = str(lock.get("symbol") or "")
    if not symbol:
        return "unknown"
    signals: set[str] = set()
    own_id = _client_id_kind(lock.get("client_order_id"))
    if own_id:
        signals.add(own_id)
    lock_time = _parse_moment(lock.get("locked_at") or lock.get("last_sell_at"))
    signals |= _sqlite_order_signals(symbol)
    signals |= _trade_log_signals(symbol, lock_time)
    signals |= _closed_order_signals(symbol, lock_time, closed_orders)
    if "ours" in signals:
        return "ours"
    if "foreign" in signals:
        return "foreign"
    return "unknown"


def symbol_has_agenttrade_order(symbol: str) -> bool:
    """True when this symbol must stay locked.

    Missing evidence is not a foreign sell. Callers that only want to drop a
    lock should use ``classify_lock``.
    """
    kind = classify_lock({"symbol": symbol})
    return kind != "foreign"


def drop_foreign_symbol_locks(ctx: dict, closed_orders: Optional[list] = None) -> dict:
    """Drop a symbol lock only when a source shows a non-AgentTrade client id.

    SOL and the manual LINK close are absent from sqlite. Their Alpaca client
    ids still start with ``agenttrade-``, so they stay. PEPE does not.
    """
    ctx = dict(ctx or {})
    if closed_orders is None:
        closed_orders = fetch_closed_orders()
    kept = []
    dropped = []
    for lock in list(ctx.get("symbol_locks") or []):
        sym = str(lock.get("symbol") or "")
        if sym and classify_lock(lock, closed_orders) == "foreign":
            dropped.append(sym)
            continue
        kept.append(lock)
    if dropped:
        log.info("[BuyLock] Cleared foreign sell lock(s): %s", ", ".join(dropped))
    ctx["symbol_locks"] = kept
    dropped_forms: set[str] = set()
    for sym in dropped:
        dropped_forms |= _symbol_forms(sym)
    ctx["last_sell_symbols"] = [
        sym for sym in (ctx.get("last_sell_symbols") or [])
        if not (_symbol_forms(sym) & dropped_forms)
    ]
    return ctx


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
    return drop_foreign_symbol_locks(out)


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
    client_order_id: Optional[str] = None,
    order_id: Optional[str] = None,
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
        if not symbols_match(lock.get("symbol") or "", sym):
            continue
        existing_unlock = _parse_iso(lock.get("unlock_at"))
        if existing_unlock and unlock_at > existing_unlock:
            lock["unlock_at"] = unlock_iso
        lock["reason"] = reason
        lock["scope"] = scope
        lock["asset_class"] = ac
        if bucket:
            lock["bucket"] = bucket
        if client_order_id:
            lock["client_order_id"] = client_order_id
        if order_id:
            lock["order_id"] = order_id
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
        if client_order_id:
            entry["client_order_id"] = client_order_id
        if order_id:
            entry["order_id"] = order_id
        locks.append(entry)

    ctx["symbol_locks"] = locks[-_LOCK_CAP:]
    return ctx


def record_exit_order(order: dict, strategy_name: str = "") -> None:
    """Persist an AgentTrade exit so a later load can see its client id."""
    if not isinstance(order, dict):
        return
    if str(order.get("side") or "").lower() != "sell":
        return
    if str(order.get("status") or "placed") not in ("placed", "filled", "accepted", "new"):
        return
    order_id = order.get("order_id") or order.get("id") or order.get("alpaca_order_id")
    client_order_id = order.get("client_order_id")
    if not order_id and not client_order_id:
        return
    try:
        from agenttrade.db import record_submitted_order

        record_submitted_order(
            order.get("cycle_run_id"),
            {
                "order_id": order_id,
                "symbol": order.get("ticker") or order.get("symbol"),
                "side": "sell",
                "qty": order.get("shares") or order.get("qty"),
                "status": order.get("status") or "placed",
                "client_order_id": client_order_id,
                "submitted_at": order.get("submitted_at") or order.get("filled_at"),
                "type": order.get("type") or order.get("order_type"),
                "time_in_force": order.get("time_in_force"),
            },
            strategy_name=strategy_name or order.get("bucket") or order.get("source") or "exit",
        )
    except Exception as exc:
        log.info("[BuyLock] Could not record exit %s: %s", order.get("ticker") or order.get("symbol"), exc)


def _client_id_on_fill(fill: dict, closed_orders: Optional[list]) -> str:
    cid = str(fill.get("client_order_id") or "").strip()
    if cid:
        return cid
    raw = fill.get("raw_json")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if isinstance(raw, dict):
        cid = str(raw.get("client_order_id") or "").strip()
        if cid:
            return cid
    oid = str(fill.get("order_id") or fill.get("alpaca_order_id") or "").strip()
    if not oid or not closed_orders:
        return ""
    for order in closed_orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("id") or order.get("order_id") or "") == oid:
            return str(order.get("client_order_id") or "").strip()
    return ""


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
    closed_orders = None
    if any(
        str(fill.get("side", "")).lower() == "sell" and not fill.get("client_order_id")
        for fill in (fills or [])
    ):
        closed_orders = fetch_closed_orders()

    for fill in fills or []:
        if str(fill.get("side", "")).lower() != "sell":
            continue
        fid = _fill_id(fill)
        if fid in seen:
            continue
        seen.add(fid)
        client_order_id = _client_id_on_fill(fill, closed_orders)
        judged = dict(fill)
        if client_order_id:
            judged["client_order_id"] = client_order_id
        if not sell_fill_is_agenttrade(judged):
            log.info(
                "[BuyLock] Ignoring foreign sell fill %s — no AgentTrade cooldown",
                fill.get("ticker") or fill.get("symbol") or fid,
            )
            continue

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
        order_id = fill.get("order_id") or fill.get("alpaca_order_id")
        ctx = _upsert_symbol_lock(
            ctx,
            symbol=sym_u,
            asset_class=ac,
            unlock_at=unlock_at,
            reason="recent_sell",
            scope="symbol",
            locked_at=fill_time,
            client_order_id=client_order_id or None,
            order_id=str(order_id) if order_id else None,
        )
        record_exit_order({
            "ticker": sym_u,
            "side": "sell",
            "status": "filled",
            "order_id": order_id,
            "client_order_id": client_order_id,
            "submitted_at": fill_iso,
            "shares": fill.get("shares") or fill.get("qty"),
            "source": "sell_fill",
        })

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
    unlock_at = now + timedelta(hours=cfg.POST_SELL_COOLDOWN_HOURS)
    for o in placed:
        sym = o.get("ticker") or o.get("symbol") or ""
        sym_u = str(sym).upper()
        if not sym_u:
            continue
        symbols.append(sym_u)
        if o.get("bucket"):
            buckets[sym_u] = o["bucket"]
        ctx = _upsert_symbol_lock(
            ctx,
            symbol=sym_u,
            asset_class=infer_asset_class(sym_u),
            unlock_at=unlock_at,
            reason="recent_sell",
            scope="symbol",
            bucket=o.get("bucket") or _lookup_bucket(sym_u),
            locked_at=now,
            client_order_id=o.get("client_order_id"),
            order_id=str(o.get("order_id") or "") or None,
        )
        record_exit_order(o)

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
            if symbols_match(lock.get("symbol") or "", sym):
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
