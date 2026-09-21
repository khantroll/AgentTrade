"""
trade_log.py — Persist Alpaca fill activities for the Reports trade log.

Syncs on each trading cycle; optional one-shot backfill via backfill_trades.py.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from typing import Optional

from app_paths import resolve_app_dir

try:
    import requests
except ImportError:
    requests = None

TRADE_LOG_FILE = "trade_log.jsonl"
ORDER_META_FILE = "order_meta.jsonl"
PUBLIC_SNAPSHOT = "trade_log.json"
MAX_LINES = 3000
MAX_FETCH_PAGES = 10


def _app_dir() -> str:
    return resolve_app_dir()


def _log_path() -> str:
    return os.path.join(_app_dir(), TRADE_LOG_FILE)


def _meta_path() -> str:
    return os.path.join(_app_dir(), ORDER_META_FILE)


def _load_config_env() -> None:
    try:
        from config_server import apply_config_to_env
        apply_config_to_env()
    except Exception:
        try:
            from dotenv import load_dotenv
            load_dotenv(override=False)
        except ImportError:
            pass


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load_bucket_tags() -> dict:
    path = os.path.join(_app_dir(), "bucket_tags.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def append_order_meta(orders: list, llm_mode: str = None, decisions: list = None) -> None:
    """Record order_id → bucket/rationale/LLM context for fill enrichment."""
    if not orders:
        return

    dec_by_ticker = {}
    for d in decisions or []:
        if str(d.get("action", "")).upper() != "BUY":
            continue
        t = d.get("ticker") or d.get("symbol")
        if t:
            dec_by_ticker[t] = d

    path = _meta_path()
    try:
        with open(path, "a", encoding="utf-8") as f:
            for o in orders:
                oid = o.get("order_id")
                if not oid:
                    continue
                ticker = o.get("ticker")
                dec = dec_by_ticker.get(ticker) or {}
                row = {
                    "order_id": oid,
                    "ticker": ticker,
                    "bucket": o.get("bucket") or dec.get("bucket"),
                    "rationale": o.get("rationale") or dec.get("rationale"),
                    "llm_mode": llm_mode or dec.get("llm_mode"),
                    "dual_status": o.get("dual_status") or dec.get("dual_status"),
                    "tiered_source": o.get("tiered_source") or dec.get("tiered_source"),
                    "stop_loss_price": o.get("stop_loss_price") or dec.get("stop_loss_price"),
                    "take_profit_price": o.get("take_profit_price") or dec.get("take_profit_price"),
                    "ts": datetime.now().isoformat(),
                }
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _load_order_meta() -> dict:
    path = _meta_path()
    meta = {}
    if not os.path.exists(path):
        return meta
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                oid = row.get("order_id")
                if oid:
                    meta[oid] = row
    except OSError:
        pass
    return meta


def _alpaca_headers() -> tuple:
    _load_config_env()
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY required")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
    return base, {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def fetch_alpaca_fills(after_date=None, page_size: int = 100) -> list:
    """Paginated Alpaca FILL activities."""
    if requests is None:
        raise RuntimeError("requests is not installed")

    base, headers = _alpaca_headers()
    fills = []
    page_token = None

    for _ in range(MAX_FETCH_PAGES):
        params = {"page_size": page_size, "direction": "desc"}
        if after_date:
            params["after"] = after_date
        if page_token:
            params["page_token"] = page_token

        r = requests.get(
            f"{base}/v2/account/activities/FILL",
            params=params,
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        fills.extend(batch)
        page_token = r.headers.get("x-next-page-token") or r.headers.get("X-Next-Page-Token")
        if not page_token:
            break

    return fills


def _fill_dedupe_key(row: dict) -> str:
    """Stable key for merge/dedupe across Alpaca fill sources."""
    return "|".join([
        str(row.get("order_id") or ""),
        str(row.get("time") or row.get("date") or ""),
        str(row.get("symbol") or ""),
        str(row.get("side") or ""),
        str(row.get("qty") or ""),
    ])


def _latest_fill_at(trades: list) -> Optional[str]:
    if not trades:
        return None
    times = [t.get("time") for t in trades if t.get("time")]
    return max(times) if times else None


def normalize_fill(activity: dict, tags: dict, meta: dict) -> dict:
    sym = activity.get("symbol") or activity.get("symbol_id") or ""
    qty = _float(activity.get("qty"))
    price = _float(activity.get("price"))
    oid = activity.get("order_id") or activity.get("id") or ""
    ts = activity.get("transaction_time") or activity.get("date") or ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        day = dt.date().isoformat()
        ts_out = dt.isoformat()
    except ValueError:
        day = str(ts)[:10] if ts else ""
        ts_out = str(ts)

    m = meta.get(oid) or {}
    bucket = m.get("bucket") or tags.get(sym) or ""

    return {
        "id": activity.get("id") or f"{sym}_{ts}_{oid}",
        "time": ts_out,
        "date": day,
        "symbol": sym,
        "side": (activity.get("side") or "").lower(),
        "qty": round(qty, 6),
        "price": round(price, 4),
        "notional": round(qty * price, 2),
        "order_id": oid,
        "bucket": bucket,
        "rationale": m.get("rationale") or "",
        "llm_mode": m.get("llm_mode") or "",
        "dual_status": m.get("dual_status") or "",
        "tiered_source": m.get("tiered_source") or "",
        "source": "alpaca_fill",
    }


def load_trades(max_days: int = 365, limit: int = 2000) -> list:
    path = _log_path()
    if not os.path.exists(path):
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).date()
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    d = datetime.fromisoformat(str(row.get("date", ""))).date()
                except ValueError:
                    d = None
                if d and d < cutoff:
                    continue
                out.append(row)
    except OSError:
        return []

    out.sort(key=lambda r: r.get("time") or "", reverse=True)
    if len(out) > limit:
        out = out[:limit]
    return out


def _write_trades(rows: list) -> None:
    path = _log_path()
    root = _app_dir()
    fd, tmp = tempfile.mkstemp(prefix="trade_log_", suffix=".jsonl", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _trim_log(rows: list) -> list:
    if len(rows) <= MAX_LINES:
        return rows
    rows.sort(key=lambda r: r.get("time") or "")
    return rows[-MAX_LINES:]


def sync_trade_log(state=None, days: int = 90) -> dict:
    """Fetch Alpaca fills and merge into trade_log.jsonl."""
    try:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        raw = fetch_alpaca_fills(after_date=after)
        tags = _load_bucket_tags()
        meta = _load_order_meta()

        if state:
            for o in state.get("last_orders") or []:
                oid = o.get("order_id")
                if oid:
                    meta[oid] = {
                        "order_id": oid,
                        "ticker": o.get("ticker"),
                        "bucket": o.get("bucket"),
                        "rationale": o.get("rationale"),
                        "llm_mode": state.get("llm_mode"),
                        "dual_status": o.get("dual_status"),
                        "tiered_source": o.get("tiered_source"),
                    }

        existing_by_key = {}
        for r in load_trades(max_days=max(days, 365), limit=MAX_LINES):
            existing_by_key[_fill_dedupe_key(r)] = r

        added = 0
        for act in raw:
            row = normalize_fill(act, tags, meta)
            key = _fill_dedupe_key(row)
            if key not in existing_by_key:
                existing_by_key[key] = row
                added += 1
            else:
                old = existing_by_key[key]
                if row.get("bucket") and not old.get("bucket"):
                    old["bucket"] = row["bucket"]
                if row.get("rationale") and not old.get("rationale"):
                    old["rationale"] = row["rationale"]

        merged = _trim_log(list(existing_by_key.values()))
        merged.sort(key=lambda r: r.get("time") or "", reverse=True)
        _write_trades(merged)
        latest = _latest_fill_at(merged)
        return {
            "ok": True,
            "added": added,
            "total": len(merged),
            "latest_fill_at": latest,
            "message": f"Synced {added} new fills",
        }
    except Exception as e:
        return {"ok": False, "message": str(e), "added": 0}


def summarize_trades(trades: list) -> dict:
    buys = sells = 0
    volume = 0.0
    by_bucket: dict = {}
    symbols = set()

    for t in trades:
        side = (t.get("side") or "").lower()
        if side == "buy":
            buys += 1
        elif side == "sell":
            sells += 1
        notional = _float(t.get("notional"))
        volume += notional
        sym = t.get("symbol")
        if sym:
            symbols.add(sym)
        b = t.get("bucket") or "Unassigned"
        by_bucket.setdefault(b, {"count": 0, "volume": 0.0, "buys": 0, "sells": 0})
        by_bucket[b]["count"] += 1
        by_bucket[b]["volume"] += notional
        if side == "buy":
            by_bucket[b]["buys"] += 1
        elif side == "sell":
            by_bucket[b]["sells"] += 1

    return {
        "total": len(trades),
        "buys": buys,
        "sells": sells,
        "volume": round(volume, 2),
        "symbols": len(symbols),
        "by_bucket": by_bucket,
    }


def get_trades(max_days: int = 90) -> dict:
    trades = load_trades(max_days=max_days)
    payload = {
        "ok": True,
        "max_days": max_days,
        "trades": trades,
        "summary": summarize_trades(trades),
        "count": len(trades),
        "latest_fill_at": _latest_fill_at(trades),
    }
    try:
        from pnl_attribution import compute_attribution, _load_agent_state
        all_trades = load_trades(max_days=max(max_days, 365), limit=3000)
        payload["attribution"] = compute_attribution(
            all_trades, max_days=max_days, state=_load_agent_state()
        )
    except Exception:
        payload["attribution"] = {"ok": False, "by_bucket": [], "closed_trades": [], "totals": {}}
    return payload


def publish_trade_log(public_dir: str, max_days: int = 365) -> None:
    if not public_dir:
        return
    payload = get_trades(max_days=max_days)
    targets = [os.path.join(public_dir, PUBLIC_SNAPSHOT)]
    app_copy = os.path.join(_app_dir(), PUBLIC_SNAPSHOT)
    if app_copy not in targets:
        targets.append(app_copy)

    for dest in targets:
        tmp = None
        try:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            parent = os.path.dirname(dest) or "."
            fd, tmp = tempfile.mkstemp(prefix="trade_log_", suffix=".json", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, dest)
            os.chmod(dest, 0o644)
        except OSError:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
