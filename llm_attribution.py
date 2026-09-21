"""
llm_attribution.py — P/L grouped by LLM mode, routing path, and model source.
"""

from pnl_attribution import _float, _load_agent_state


def llm_route_label(
    llm_mode: str = "",
    dual_status: str = "",
    tiered_source: str = "",
    rationale: str = "",
) -> str:
    """Human-readable routing label for a buy decision."""
    ds = (dual_status or "").lower()
    if ds == "both_buy":
        return "Dual agree"
    if ds == "conflict":
        return "Dual conflict"
    if ds == "single_response":
        return "Single model"
    if tiered_source:
        return f"Tiered: {tiered_source}"
    rat = rationale or ""
    if "Tiered multi-model" in rat or "Tiered" in rat[:40]:
        return "Tiered vote"
    if "DUAL AGREE" in rat:
        return "Dual agree"
    mode = (llm_mode or "").strip()
    if mode == "economy":
        return "Economy routing"
    if mode == "tiered":
        return "Tiered"
    if mode:
        return mode.replace("_", " ")
    return "Unknown"


def _load_meta_by_symbol() -> dict:
    """Latest order meta row per ticker (for open-position LLM attribution)."""
    from trade_log import _meta_path
    import json
    import os

    path = _meta_path()
    by_sym = {}
    if not os.path.exists(path):
        return by_sym
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
                sym = row.get("ticker")
                if sym:
                    by_sym[sym] = row
    except OSError:
        pass
    return by_sym


def _sum_rows(rows: list) -> dict:
    g = {
        "realized_pl": 0.0,
        "unrealized_pl": 0.0,
        "wins": 0,
        "losses": 0,
        "closed_trades": 0,
        "open_positions": 0,
        "symbols": 0,
    }
    sym_set = set()
    for row in rows:
        g["realized_pl"] += _float(row.get("realized_pl"))
        g["unrealized_pl"] += _float(row.get("unrealized_pl"))
        g["wins"] += int(row.get("wins") or 0)
        g["losses"] += int(row.get("losses") or 0)
        g["closed_trades"] += int(row.get("closed_trades") or 0)
        g["open_positions"] += int(row.get("open_positions") or 0)
        sym = row.get("symbol")
        if sym:
            sym_set.add(sym)
    ct = g["closed_trades"]
    total = g["realized_pl"] + g["unrealized_pl"]
    return {
        "realized_pl": round(g["realized_pl"], 2),
        "unrealized_pl": round(g["unrealized_pl"], 2),
        "total_pl": round(total, 2),
        "wins": g["wins"],
        "losses": g["losses"],
        "closed_trades": ct,
        "win_rate": round(g["wins"] / ct * 100, 1) if ct else None,
        "open_positions": g["open_positions"],
        "symbols": len(sym_set),
    }


def _group_aggregate(rows: list, key_fn, label_key: str) -> list:
    groups: dict = {}
    for row in rows:
        key = key_fn(row) or "Unknown"
        groups.setdefault(key, []).append(row)
    out = [{label_key: key, **_sum_rows(group)} for key, group in groups.items()]
    out.sort(key=lambda x: x["total_pl"], reverse=True)
    return out


def llm_attribution_from_closed(closed: list, state: dict = None) -> tuple:
    """
    Build LLM-attributed rows from FIFO closed trades + open positions.
    Returns (by_mode, by_route, by_model, symbol_rows).
    """
    state = state or _load_agent_state()
    meta_by_sym = _load_meta_by_symbol()
    tags = state.get("bucket_tags") or {}

    sym_realized: dict = {}
    for row in closed:
        if not row.get("in_period"):
            continue
        sym = row.get("symbol") or ""
        if not sym:
            continue
        ctx = {
            "llm_mode": row.get("llm_mode") or "",
            "dual_status": row.get("dual_status") or "",
            "tiered_source": row.get("tiered_source") or "",
            "rationale": row.get("rationale") or "",
        }
        ctx["llm_route"] = llm_route_label(**ctx)
        sym_realized.setdefault(sym, {**ctx, "realized_pl": 0.0, "wins": 0, "losses": 0, "closed_trades": 0})
        s = sym_realized[sym]
        pl = _float(row.get("realized_pl"))
        s["realized_pl"] += pl
        s["closed_trades"] += 1
        if pl >= 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        for k in ctx:
            if ctx[k] and not s.get(k):
                s[k] = ctx[k]

    sym_rows: dict = {}
    for p in state.get("positions") or []:
        sym = p.get("symbol") or p.get("ticker") or ""
        if not sym:
            continue
        meta = meta_by_sym.get(sym) or {}
        base = sym_realized.get(sym, {})
        ctx = {
            "llm_mode": base.get("llm_mode") or meta.get("llm_mode") or state.get("llm_mode") or "",
            "dual_status": base.get("dual_status") or meta.get("dual_status") or "",
            "tiered_source": base.get("tiered_source") or meta.get("tiered_source") or "",
            "rationale": base.get("rationale") or meta.get("rationale") or "",
        }
        ctx["llm_route"] = llm_route_label(**ctx)
        real_pl = round(_float(base.get("realized_pl")), 2)
        unreal_pl = round(_float(p.get("unrealized_pl")), 2)
        sym_rows[sym] = {
            "symbol": sym,
            **ctx,
            "bucket": tags.get(sym) or meta.get("bucket") or "",
            "realized_pl": real_pl,
            "unrealized_pl": unreal_pl,
            "total_pl": round(real_pl + unreal_pl, 2),
            "wins": int(base.get("wins") or 0),
            "losses": int(base.get("losses") or 0),
            "closed_trades": int(base.get("closed_trades") or 0),
            "open_positions": 1,
        }

    for sym, base in sym_realized.items():
        if sym in sym_rows:
            continue
        ctx = {
            "llm_mode": base.get("llm_mode") or "",
            "dual_status": base.get("dual_status") or "",
            "tiered_source": base.get("tiered_source") or "",
            "rationale": base.get("rationale") or "",
        }
        ctx["llm_route"] = llm_route_label(**ctx)
        real_pl = round(_float(base.get("realized_pl")), 2)
        sym_rows[sym] = {
            "symbol": sym,
            **ctx,
            "bucket": tags.get(sym) or "",
            "realized_pl": real_pl,
            "unrealized_pl": 0.0,
            "total_pl": real_pl,
            "wins": int(base.get("wins") or 0),
            "losses": int(base.get("losses") or 0),
            "closed_trades": int(base.get("closed_trades") or 0),
            "open_positions": 0,
        }

    flat = list(sym_rows.values())
    by_mode = _group_aggregate(
        flat,
        lambda r: (r.get("llm_mode") or "Unknown").replace("_", " "),
        "llm_mode",
    )
    by_route = _group_aggregate(flat, lambda r: r.get("llm_route") or "Unknown", "llm_route")
    by_model = _group_aggregate(
        flat,
        lambda r: (
            r.get("tiered_source")
            or (r.get("dual_status") if r.get("dual_status") not in ("single", "single_response", "") else None)
            or (r.get("llm_mode") or "Unknown")
        ).replace("_", " "),
        "model",
    )
    symbol_list = sorted(flat, key=lambda x: x.get("total_pl", 0), reverse=True)
    return by_mode, by_route, by_model, symbol_list


def compute_llm_attribution(closed: list, state: dict = None, token_usage: dict = None) -> dict:
    by_mode, by_route, by_model, _ = llm_attribution_from_closed(closed, state=state)
    payload = {
        "by_llm_mode": by_mode,
        "by_route": by_route,
        "by_model": by_model,
    }
    if token_usage:
        payload["token_cost_by_model"] = token_usage.get("by_model") or {}
        payload["token_cost_usd"] = token_usage.get("total_cost_usd")
    return payload
