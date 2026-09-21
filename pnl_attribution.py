"""
pnl_attribution.py — Realized and unrealized P/L by bucket.

Uses FIFO matching on trade_log fills for realized P/L; open positions from
SQLite (Alpaca snapshots) for unrealized P/L. agent_state.json is a
non-authoritative fallback only.
"""

import json
import os
from collections import deque
from datetime import datetime, timedelta, timezone

from app_paths import resolve_app_dir


def _app_dir() -> str:
    return resolve_app_dir()


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_time(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _load_json_state_fallback() -> dict:
    """NON-AUTHORITATIVE dashboard projection cache."""
    path = os.path.join(_app_dir(), "agent_state.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_agent_state() -> dict:
    """SQLite ledger + bucket_tags.json first; JSON file is non-authoritative fallback."""
    state: dict = {}
    try:
        from agenttrade import db as ledger
        if ledger.db_available():
            ledger.init_db()
            state = ledger.load_application_state()
    except Exception:
        state = {}
    json_state = _load_json_state_fallback()
    if not state.get("positions") and json_state.get("positions"):
        # NON-AUTHORITATIVE fallback: projection cache only
        merged = dict(json_state)
        if state.get("bucket_tags"):
            merged["bucket_tags"] = state["bucket_tags"]
        elif not merged.get("bucket_tags"):
            try:
                from buckets import BucketManager
                merged["bucket_tags"] = BucketManager()._load_tags() or {}
            except Exception:
                pass
        return merged
    if json_state and not state.get("token_usage"):
        state["token_usage"] = json_state.get("token_usage") or {}
    if not state.get("bucket_tags"):
        state["bucket_tags"] = json_state.get("bucket_tags") or {}
    return state


def fifo_realized(trades: list, max_days: int = 90) -> tuple:
    """
    FIFO lot matching per symbol. Returns (closed_trades, by_bucket_realized).
    Only closed trades with sell date inside max_days are counted in period stats.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).date()
    chronological = sorted(trades, key=lambda t: t.get("time") or "")

    # symbol -> deque of lots {qty, price, time, bucket, rationale}
    lots: dict = {}
    closed = []

    for t in chronological:
        sym = t.get("symbol") or ""
        if not sym:
            continue
        side = (t.get("side") or "").lower()
        qty = _float(t.get("qty"))
        price = _float(t.get("price"))
        if qty <= 0:
            continue

        if side == "buy":
            lots.setdefault(sym, deque()).append({
                "qty": qty,
                "price": price,
                "time": t.get("time"),
                "bucket": t.get("bucket") or "Unassigned",
                "rationale": t.get("rationale") or "",
                "llm_mode": t.get("llm_mode") or "",
                "dual_status": t.get("dual_status") or "",
                "tiered_source": t.get("tiered_source") or "",
            })
        elif side == "sell":
            remaining = qty
            sell_time = t.get("time")
            sell_bucket = t.get("bucket") or ""
            while remaining > 1e-9 and lots.get(sym):
                lot = lots[sym][0]
                take = min(remaining, lot["qty"])
                pl = (price - lot["price"]) * take
                bucket = lot["bucket"] or sell_bucket or "Unassigned"
                sell_dt = _parse_time(sell_time)
                buy_dt = _parse_time(lot["time"])
                hold_days = max(0, (sell_dt.date() - buy_dt.date()).days)
                row = {
                    "symbol": sym,
                    "bucket": bucket,
                    "qty": round(take, 6),
                    "buy_price": round(lot["price"], 4),
                    "sell_price": round(price, 4),
                    "realized_pl": round(pl, 2),
                    "buy_time": lot["time"],
                    "sell_time": sell_time,
                    "hold_days": hold_days,
                    "rationale": lot["rationale"] or t.get("rationale") or "",
                    "llm_mode": lot.get("llm_mode") or "",
                    "dual_status": lot.get("dual_status") or "",
                    "tiered_source": lot.get("tiered_source") or "",
                    "in_period": sell_dt.date() >= cutoff if sell_time else True,
                }
                closed.append(row)
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    lots[sym].popleft()

    by_bucket: dict = {}
    for row in closed:
        if not row.get("in_period"):
            continue
        b = row["bucket"] or "Unassigned"
        by_bucket.setdefault(b, {"realized_pl": 0.0, "wins": 0, "losses": 0, "closed_trades": 0})
        by_bucket[b]["realized_pl"] += row["realized_pl"]
        by_bucket[b]["closed_trades"] += 1
        if row["realized_pl"] >= 0:
            by_bucket[b]["wins"] += 1
        else:
            by_bucket[b]["losses"] += 1

    for b, stats in by_bucket.items():
        ct = stats["closed_trades"]
        stats["realized_pl"] = round(stats["realized_pl"], 2)
        stats["win_rate"] = round(stats["wins"] / ct * 100, 1) if ct else None

    closed.sort(key=lambda r: r.get("sell_time") or "", reverse=True)
    return closed, by_bucket


def _aggregate_realized_by_symbol(closed: list) -> dict:
    by_sym: dict = {}
    for row in closed:
        if not row.get("in_period"):
            continue
        sym = row.get("symbol") or ""
        if not sym:
            continue
        by_sym.setdefault(sym, {
            "realized_pl": 0.0,
            "wins": 0,
            "losses": 0,
            "closed_trades": 0,
            "bucket": row.get("bucket") or "Unassigned",
        })
        s = by_sym[sym]
        s["realized_pl"] += row["realized_pl"]
        s["closed_trades"] += 1
        if row["realized_pl"] >= 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        if row.get("bucket"):
            s["bucket"] = row["bucket"]
    for sym, s in by_sym.items():
        ct = s["closed_trades"]
        s["realized_pl"] = round(s["realized_pl"], 2)
        s["win_rate"] = round(s["wins"] / ct * 100, 1) if ct else None
    return by_sym


def unrealized_by_symbol(state: dict = None) -> dict:
    """Open position P/L and market data per symbol."""
    state = state or _load_agent_state()
    tags = state.get("bucket_tags") or {}
    by_sym: dict = {}

    for p in state.get("positions") or []:
        sym = p.get("symbol") or p.get("ticker") or ""
        if not sym:
            continue
        bucket = tags.get(sym) or p.get("bucket") or p.get("bucket_name") or "Unassigned"
        by_sym[sym] = {
            "unrealized_pl": round(_float(p.get("unrealized_pl")), 2),
            "market_value": round(_float(p.get("market_value")), 2),
            "qty": _float(p.get("qty")),
            "avg_entry": round(_float(p.get("avg_entry_price") or p.get("cost_basis")), 4) or None,
            "current_price": round(_float(p.get("current_price") or p.get("market_value") / max(_float(p.get("qty")), 1)), 4) or None,
            "bucket": bucket,
            "open": True,
        }
    return by_sym


def symbol_attribution(closed: list, state: dict = None) -> list:
    """Merge realized (period) and unrealized P/L per ticker."""
    realized = _aggregate_realized_by_symbol(closed)
    unreal = unrealized_by_symbol(state)
    symbols = sorted(set(list(realized.keys()) + list(unreal.keys())))

    rows = []
    for sym in symbols:
        r = realized.get(sym, {})
        u = unreal.get(sym, {})
        real_pl = _float(r.get("realized_pl"))
        unreal_pl = _float(u.get("unrealized_pl"))
        wins = int(r.get("wins") or 0)
        losses = int(r.get("losses") or 0)
        closed_n = int(r.get("closed_trades") or 0)
        is_open = bool(u.get("open"))
        bucket = u.get("bucket") or r.get("bucket") or "Unassigned"

        if is_open and closed_n:
            status = "mixed"
        elif is_open:
            status = "open"
        elif closed_n:
            status = "closed"
        else:
            status = "—"

        rows.append({
            "symbol": sym,
            "bucket": bucket,
            "realized_pl": round(real_pl, 2),
            "unrealized_pl": round(unreal_pl, 2),
            "total_pl": round(real_pl + unreal_pl, 2),
            "closed_trades": closed_n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / closed_n * 100, 1) if closed_n else None,
            "status": status,
            "qty": u.get("qty"),
            "market_value": u.get("market_value"),
            "avg_entry": u.get("avg_entry"),
            "current_price": u.get("current_price"),
        })

    rows.sort(key=lambda x: x["total_pl"], reverse=True)
    return rows


def unrealized_by_bucket(state: dict = None) -> dict:
    """Sum unrealized P/L from open positions grouped by bucket."""
    state = state or _load_agent_state()
    tags = state.get("bucket_tags") or {}
    by_bucket: dict = {}
    open_count: dict = {}

    for p in state.get("positions") or []:
        sym = p.get("symbol") or p.get("ticker") or ""
        bucket = tags.get(sym) or p.get("bucket") or p.get("bucket_name") or "Unassigned"
        pl = _float(p.get("unrealized_pl"))
        mv = _float(p.get("market_value"))
        by_bucket.setdefault(bucket, {"unrealized_pl": 0.0, "market_value": 0.0})
        open_count[bucket] = open_count.get(bucket, 0) + 1
        by_bucket[bucket]["unrealized_pl"] += pl
        by_bucket[bucket]["market_value"] += mv

    for b in by_bucket:
        by_bucket[b]["unrealized_pl"] = round(by_bucket[b]["unrealized_pl"], 2)
        by_bucket[b]["market_value"] = round(by_bucket[b]["market_value"], 2)
        by_bucket[b]["open_positions"] = open_count.get(b, 0)

    return by_bucket


def compute_attribution(trades: list, max_days: int = 90, state: dict = None) -> dict:
    closed, realized_by_bucket = fifo_realized(trades, max_days=max_days)
    unreal = unrealized_by_bucket(state)

    all_buckets = sorted(set(list(realized_by_bucket.keys()) + list(unreal.keys())))
    by_bucket = []
    totals = {
        "realized_pl": 0.0,
        "unrealized_pl": 0.0,
        "total_pl": 0.0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "open_positions": 0,
    }

    for name in all_buckets:
        r = realized_by_bucket.get(name, {})
        u = unreal.get(name, {})
        real_pl = _float(r.get("realized_pl"))
        unreal_pl = _float(u.get("unrealized_pl"))
        wins = int(r.get("wins") or 0)
        losses = int(r.get("losses") or 0)
        closed_n = int(r.get("closed_trades") or 0)
        open_n = int(u.get("open_positions") or 0)
        total_pl = real_pl + unreal_pl

        by_bucket.append({
            "bucket": name,
            "realized_pl": round(real_pl, 2),
            "unrealized_pl": round(unreal_pl, 2),
            "total_pl": round(total_pl, 2),
            "closed_trades": closed_n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / closed_n * 100, 1) if closed_n else None,
            "open_positions": open_n,
            "market_value": u.get("market_value"),
        })

        totals["realized_pl"] += real_pl
        totals["unrealized_pl"] += unreal_pl
        totals["total_pl"] += total_pl
        totals["closed_trades"] += closed_n
        totals["wins"] += wins
        totals["losses"] += losses
        totals["open_positions"] += open_n

    totals["realized_pl"] = round(totals["realized_pl"], 2)
    totals["unrealized_pl"] = round(totals["unrealized_pl"], 2)
    totals["total_pl"] = round(totals["total_pl"], 2)
    ct = totals["closed_trades"]
    totals["win_rate"] = round(totals["wins"] / ct * 100, 1) if ct else None

    period_closed = [c for c in closed if c.get("in_period")]
    by_symbol = symbol_attribution(closed, state=state)

    if state is None:
        state = _load_agent_state()
    llm_block = {}
    try:
        from llm_attribution import compute_llm_attribution, llm_route_label
        llm_block = compute_llm_attribution(
            closed, state=state, token_usage=state.get("token_usage")
        )
        for row in period_closed[:50]:
            if not row.get("llm_route"):
                row["llm_route"] = llm_route_label(
                    row.get("llm_mode"), row.get("dual_status"),
                    row.get("tiered_source"), row.get("rationale"),
                )
    except Exception:
        llm_block = {"by_llm_mode": [], "by_route": [], "by_model": []}

    theme_block = {}
    try:
        from rationale_attribution import compute_rationale_attribution, annotate_closed_trades
        annotate_closed_trades(period_closed)
        theme_block = compute_rationale_attribution(closed, state=state)
    except Exception:
        theme_block = {"by_theme": [], "by_tag": []}

    return {
        "ok": True,
        "max_days": max_days,
        "by_bucket": by_bucket,
        "by_symbol": by_symbol,
        "closed_trades": period_closed[:50],
        "totals": totals,
        **llm_block,
        **theme_block,
    }


def get_attribution(max_days: int = 90) -> dict:
    from trade_log import load_trades
    trades = load_trades(max_days=max(max_days, 365), limit=3000)
    # FIFO needs full history for open lots; filter period on closed output
    return compute_attribution(trades, max_days=max_days)
