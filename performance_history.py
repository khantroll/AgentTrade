"""
performance_history.py — Append-only cycle snapshots for dashboard reporting.

Each trading cycle appends one JSON line. The dashboard reads aggregated history
via GET /history (config_server) or static performance_history.json.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from app_paths import resolve_app_dir

try:
    import requests
except ImportError:
    requests = None

HISTORY_FILE = "performance_history.jsonl"
PUBLIC_SNAPSHOT = "performance_history.json"
MAX_LINES = 5000


def _app_dir() -> str:
    return resolve_app_dir()


def _history_path() -> str:
    return os.path.join(_app_dir(), HISTORY_FILE)


def _load_config_env() -> None:
    """Load API keys from config.json / .env before Alpaca calls."""
    try:
        from config_server import apply_config_to_env
        apply_config_to_env()
    except Exception:
        try:
            from dotenv import load_dotenv
            load_dotenv(override=False)
        except ImportError:
            pass


def _alpaca_period(days: int) -> str:
    if days <= 7:
        return "1W"
    if days <= 30:
        return "1M"
    if days <= 90:
        return "3M"
    if days <= 365:
        return "1A"
    return "all"


def fetch_alpaca_portfolio_history(days: int = 90) -> dict:
    """Daily equity series from Alpaca portfolio history API."""
    if requests is None:
        raise RuntimeError("requests is not installed")

    _load_config_env()
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY required")

    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }
    r = requests.get(
        f"{base}/v2/account/portfolio/history",
        params={"period": _alpaca_period(days), "timeframe": "1D"},
        headers=headers,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def snapshots_from_alpaca(data: dict, days: int = 90) -> list:
    """Convert Alpaca portfolio/history arrays into cycle-compatible rows."""
    _load_config_env()
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    mode = "PAPER" if paper else "LIVE"

    ts_list = data.get("timestamp") or []
    equities = data.get("equity") or []
    pls = data.get("profit_loss") or []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    rows = []
    for i, ts in enumerate(ts_list):
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        day = dt.date()
        if day < cutoff:
            continue
        equity = _float(equities[i] if i < len(equities) else 0)
        pl = pls[i] if i < len(pls) else None
        rows.append({
            "ts": dt.isoformat(),
            "date": day.isoformat(),
            "portfolio_value": round(equity, 2),
            "cash": None,
            "last_equity": None,
            "daily_pl": round(_float(pl), 2) if pl is not None else None,
            "unrealized_pl": None,
            "positions_count": None,
            "daily_trades": None,
            "orders_placed": None,
            "candidates": None,
            "decisions": None,
            "buys": None,
            "blocked": None,
            "llm_mode": None,
            "mode": mode,
            "tokens": None,
            "token_cost_usd": None,
            "token_calls": None,
            "buckets": {},
            "source": "alpaca_backfill",
        })
    return rows


def _is_backfill_row(row: dict) -> bool:
    return row.get("source") == "alpaca_backfill"


def _dates_with_real_cycles(cycles: list) -> set:
    dates = set()
    for row in cycles:
        if not _is_backfill_row(row):
            dates.add(row.get("date"))
    return {d for d in dates if d}


def merge_backfill(existing: list, backfill: list) -> list:
    """
    Merge Alpaca daily rows into history.
    Real cycle snapshots always win for a given date; backfill fills gaps only.
    Re-running replaces previous alpaca_backfill rows.
    """
    real = [r for r in existing if not _is_backfill_row(r)]
    real_dates = _dates_with_real_cycles(real)

    kept_backfill = []
    for row in backfill:
        day = row.get("date")
        if day and day not in real_dates:
            kept_backfill.append(row)

    merged = real + kept_backfill
    merged.sort(key=lambda r: (r.get("date") or "", r.get("ts") or ""))
    return merged


def write_cycles(cycles: list) -> None:
    """Rewrite performance_history.jsonl atomically."""
    path = _history_path()
    root = _app_dir()
    fd, tmp = tempfile.mkstemp(prefix="perf_hist_", suffix=".jsonl", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in cycles:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def backfill_from_alpaca(days: int = 90) -> dict:
    """Fetch Alpaca daily equity and merge into performance_history.jsonl."""
    try:
        raw = fetch_alpaca_portfolio_history(days=days)
        backfill_rows = snapshots_from_alpaca(raw, days=days)
        if not backfill_rows:
            return {"ok": False, "message": "Alpaca returned no portfolio history", "added": 0}

        existing = load_cycles(max_days=max(days, 365), limit=MAX_LINES)
        before = len(existing)
        real_dates_before = _dates_with_real_cycles(existing)
        merged = merge_backfill(existing, backfill_rows)
        write_cycles(merged)

        new_dates = {r.get("date") for r in backfill_rows if r.get("date") not in real_dates_before}
        return {
            "ok": True,
            "added": len(new_dates),
            "total": len(merged),
            "before": before,
            "message": f"Imported {len(new_dates)} days from Alpaca",
        }
    except Exception as e:
        return {"ok": False, "message": str(e), "added": 0}


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bucket_summary(rebalance: dict, positions: list = None, bucket_tags: dict = None) -> dict:
    pl_by_bucket: dict = {}
    count_by_bucket: dict = {}
    if positions:
        for p in positions:
            sym = p.get("symbol") or p.get("ticker") or ""
            bucket = (bucket_tags or {}).get(sym) or p.get("bucket") or p.get("bucket_name") or "Unassigned"
            pl_by_bucket[bucket] = pl_by_bucket.get(bucket, 0.0) + _float(p.get("unrealized_pl"))
            count_by_bucket[bucket] = count_by_bucket.get(bucket, 0) + 1

    out = {}
    for name, r in (rebalance or {}).items():
        pos_list = r.get("positions") or []
        out[name] = {
            "current_pct": r.get("current_pct"),
            "target_pct": r.get("target_pct"),
            "value": round(_float(r.get("current_$")), 2),
            "drift_$": round(_float(r.get("drift_$")), 2),
            "action": r.get("action"),
            "positions_count": len(pos_list) if pos_list else count_by_bucket.get(name, 0),
            "unrealized_pl": round(pl_by_bucket.get(name, 0.0), 2),
        }
    return out


def build_cycle_snapshot(account: dict, positions: list, state: dict) -> dict:
    """Compact record for one trading cycle — safe to store long-term."""
    equity = _float(account.get("equity") or account.get("portfolio_value"))
    last_equity = _float(account.get("last_equity"))
    cash = _float(account.get("cash") or state.get("cash"))
    unrealized = sum(_float(p.get("unrealized_pl")) for p in (positions or []))

    tok = state.get("token_usage") or {}
    decisions = state.get("decisions") or []
    buys = sum(1 for d in decisions if str(d.get("action", "")).upper() == "BUY")

    ts = state.get("last_run") or datetime.now().isoformat()
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now()

    return {
        "ts": ts,
        "date": dt.date().isoformat(),
        "portfolio_value": round(equity, 2),
        "cash": round(cash, 2),
        "last_equity": round(last_equity, 2) if last_equity else None,
        "daily_pl": round(equity - last_equity, 2) if last_equity else None,
        "unrealized_pl": round(unrealized, 2),
        "positions_count": len(positions or []),
        "daily_trades": int(state.get("daily_trades") or 0),
        "orders_placed": len(state.get("last_orders") or []),
        "candidates": len(state.get("trade_candidates") or []),
        "decisions": len(decisions),
        "buys": buys,
        "blocked": len(state.get("blocked_ideas") or []),
        "llm_mode": state.get("llm_mode"),
        "mode": state.get("mode", "PAPER"),
        "tokens": int(tok.get("total_tokens") or 0),
        "token_cost_usd": round(_float(tok.get("total_cost_usd")), 4),
        "token_calls": int(tok.get("calls") or 0),
        "buckets": _bucket_summary(
            state.get("rebalance") or {},
            positions=positions,
            bucket_tags=state.get("bucket_tags") or {},
        ),
    }


def append_cycle_snapshot(account: dict, positions: list, state: dict) -> dict:
    """Append one cycle line to JSONL; trim if file grows too large."""
    snap = build_cycle_snapshot(account, positions, state)
    path = _history_path()

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, separators=(",", ":")) + "\n")

    _trim_history(path)
    return snap


def _trim_history(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_LINES:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-MAX_LINES:])
    except OSError:
        pass


def load_cycles(max_days: int = 365, limit: int = 2000) -> list:
    path = _history_path()
    if not os.path.exists(path):
        return []

    cutoff = (datetime.now() - timedelta(days=max_days)).date()
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

    if len(out) > limit:
        out = out[-limit:]
    return out


def rollup_daily(cycles: list) -> list:
    """One point per calendar day — prefers real cycle snapshots over Alpaca backfill."""
    by_date: dict = {}
    for row in cycles:
        day = row.get("date")
        if not day:
            continue
        prev = by_date.get(day)
        if prev is None:
            by_date[day] = row
            continue
        # Real cycle beats backfill; otherwise latest timestamp wins
        if _is_backfill_row(prev) and not _is_backfill_row(row):
            by_date[day] = row
        elif not _is_backfill_row(prev) and _is_backfill_row(row):
            pass
        else:
            by_date[day] = row

    daily = []
    prev_pv = None
    for day in sorted(by_date.keys()):
        row = by_date[day]
        pv = _float(row.get("portfolio_value"))
        daily.append({
            "date": day,
            "portfolio_value": pv,
            "daily_pl": row.get("daily_pl"),
            "change_from_prev": round(pv - prev_pv, 2) if prev_pv is not None else None,
            "cash": row.get("cash"),
            "positions_count": row.get("positions_count"),
            "daily_trades": row.get("daily_trades"),
            "tokens": row.get("tokens"),
            "token_cost_usd": row.get("token_cost_usd"),
            "llm_mode": row.get("llm_mode"),
            "candidates": row.get("candidates"),
            "orders_placed": row.get("orders_placed"),
            "blocked": row.get("blocked"),
        })
        prev_pv = pv
    return daily


def rollup_bucket_daily(cycles: list) -> dict:
    """Per-bucket time series — last snapshot each calendar day."""
    series: dict = {}
    for row in cycles:
        day = row.get("date")
        if not day:
            continue
        for name, b in (row.get("buckets") or {}).items():
            series.setdefault(name, {})[day] = {
                "date": day,
                "value": b.get("value"),
                "current_pct": b.get("current_pct"),
                "target_pct": b.get("target_pct"),
                "drift_$": b.get("drift_$"),
                "unrealized_pl": b.get("unrealized_pl"),
                "action": b.get("action"),
                "positions_count": b.get("positions_count"),
            }
    return {
        name: [days[d] for d in sorted(days.keys())]
        for name, days in series.items()
    }


def bucket_period_summary(cycles: list) -> list:
    """Latest bucket stats plus value change over the loaded cycle window."""
    if not cycles:
        return []
    bucket_daily = rollup_bucket_daily(cycles)
    rows = []
    for name in sorted(bucket_daily.keys()):
        pts = bucket_daily[name]
        if not pts:
            continue
        first, last = pts[0], pts[-1]
        v0 = _float(first.get("value"))
        v1 = _float(last.get("value"))
        rows.append({
            "name": name,
            "target_pct": last.get("target_pct"),
            "current_pct": last.get("current_pct"),
            "value": last.get("value"),
            "drift_$": last.get("drift_$"),
            "unrealized_pl": last.get("unrealized_pl"),
            "action": last.get("action"),
            "positions_count": last.get("positions_count"),
            "value_change": round(v1 - v0, 2) if pts else None,
            "first_date": first.get("date"),
            "last_date": last.get("date"),
        })
    return rows


def get_history(max_days: int = 90) -> dict:
    cycles = load_cycles(max_days=max_days)
    daily = rollup_daily(cycles)
    bucket_daily = rollup_bucket_daily(cycles)
    period_summary = {}
    try:
        from period_summary import compute_period_summary
        period_summary = compute_period_summary(max_days=max_days)
    except Exception:
        period_summary = {"ok": False}
    return {
        "ok": True,
        "max_days": max_days,
        "cycles": cycles,
        "daily": daily,
        "bucket_daily": bucket_daily,
        "bucket_summary": bucket_period_summary(cycles),
        "period_summary": period_summary,
        "count": len(cycles),
    }


def publish_history(public_dir: str, max_days: int = 365) -> None:
    """Write performance_history.json beside agent_state for static dashboard fetch."""
    if not public_dir:
        return
    payload = get_history(max_days=max_days)
    dest = os.path.join(public_dir, PUBLIC_SNAPSHOT)
    tmp = None
    try:
        os.makedirs(public_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="perf_hist_", suffix=".json", dir=public_dir)
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


def seed_from_agent_state_if_empty() -> bool:
    """If no history yet, seed one point from SQLite (JSON is non-authoritative fallback)."""
    path = _history_path()
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return False

    account = None
    positions = []
    extra = {}
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            row = ledger.get_latest_account_snapshot()
            if row and (row.get("equity") or row.get("portfolio_value")):
                account = ledger.account_row_as_snapshot(row)
                positions = ledger.positions_as_dashboard()
                extra = {"source": "sqlite_seed"}
    except Exception:
        account = None

    if not account:
        # NON-AUTHORITATIVE fallback: agent_state.json projection cache
        state_path = os.path.join(_app_dir(), "agent_state.json")
        if not os.path.exists(state_path):
            return False
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        if not state.get("portfolio_value"):
            return False
        account = {
            "equity": state.get("portfolio_value"),
            "portfolio_value": state.get("portfolio_value"),
            "cash": state.get("cash"),
            "last_equity": None,
        }
        positions = state.get("positions") or []
        extra = state

    if not account:
        return False
    append_cycle_snapshot(account, positions, extra)
    return True
