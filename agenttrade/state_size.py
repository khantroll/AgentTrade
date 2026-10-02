"""Keep dashboard projections under the agent_state.json size cap.

The projection is a cache. SQLite remains the ledger. Large LLM bodies,
OHLCV arrays, Reddit mention dumps, and screener pipeline membership maps
are not required for the dashboard to show the last cycle's decisions,
positions, fills, or analysis path. Those are stripped here so a normal
cycle writes a file under the default 8 MiB cap instead of refusing the
write and leaving the previous SKIP snapshot on disk.
"""

from __future__ import annotations

from typing import Any, Optional

# Nested keys that have blown the projection into the tens or hundreds of MB
# when copied into screener_sources, funnel payloads, and signal raw_json.
_DROP_KEYS = frozenset({
    "ohlcv",
    "raw_json",
    "prompt",
    "raw_response",
    "llm_raw",
    "raw_text",
    "selftext",
    "mentions",
    "reddit_intelligence",
    "pipeline_membership",
    "explainability_json",
    "components_json",
    "pipelines_json",
    "differences_json",
})

_REDDIT_KEEP = frozenset({
    "avg_sentiment",
    "quality_score",
    "mention_count",
    "subreddits",
})

# Short identifiers the dashboard matches on. Still capped so a runaway
# string cannot by itself exceed the projection limit.
_TEXT_KEEP = frozenset({
    "ticker",
    "symbol",
    "order_id",
    "id",
    "analysis_path",
    "action",
    "decision",
    "status",
    "side",
    "bucket",
    "asset_class",
})

_TEXT_LIMIT = 500
_ID_TEXT_LIMIT = 120
_MAX_DEPTH = 8

# Current-cycle facts the dashboard reads. Capped, never deleted to fit.
_ROW_LIST_CAPS = {
    "decisions": 80,
    "trade_candidates": 80,
    "candidates": 80,
    "blocked_ideas": 80,
    "last_orders": 40,
    "recent_fills": 50,
    "recent_trades": 50,
    "positions": 100,
    "open_positions": 100,
    "open_orders": 100,
    "completed_trades": 40,
    "strategy_signals": 20,
    "sentiment_scores": 20,
    "signal_snapshots": 20,
    "signal_attributions": 30,
    "recent_risk_events": 10,
    "risk_events": 10,
    "reddit_trends": 40,
}

# Historical / diagnostic sections. Dropped only if the slim projection
# is still over the cap. Durable cycle facts above are not in this list.
_SHED_KEYS = (
    "signal_snapshots",
    "strategy_signals",
    "sentiment_scores",
    "reddit_trends",
    "hypothetical_trades",
    "signal_scorecards",
    "performance_metrics",
    "validation",
    "source_accuracy_stats",
    "recent_risk_events",
    "risk_events",
    "signal_attributions",
    "position_signal_breakdown",
    "consecutive_loss_detail",
)


def slim_json_value(value: Any, depth: int = 0) -> Any:
    """Return a JSON-ready copy without bulky nested bodies."""
    if depth > _MAX_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _TEXT_LIMIT:
            return value[:_TEXT_LIMIT]
        return value
    if isinstance(value, list):
        return [slim_json_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return _slim_dict(value, depth)
    return str(value)[:_TEXT_LIMIT]


def slim_persisted_json(value: Any) -> Any:
    """Slim a value before it is stored in SQLite and later reloaded."""
    return slim_json_value(value)


def _slim_dict(row: dict, depth: int) -> dict:
    out: dict = {}
    for key, raw in row.items():
        name = str(key)
        if name in _DROP_KEYS:
            continue
        if name == "signal_attribution" and isinstance(raw, dict):
            _copy_attribution_summary(out, raw)
            continue
        if name == "reddit_detail" and isinstance(raw, dict):
            detail = {
                k: slim_json_value(v, depth + 1)
                for k, v in raw.items()
                if k in _REDDIT_KEEP
            }
            if detail:
                out[name] = detail
            continue
        if name == "attribution" and isinstance(raw, dict):
            out[name] = _slim_attribution_map(raw, depth)
            continue
        if isinstance(raw, str):
            limit = _ID_TEXT_LIMIT if name in _TEXT_KEEP else _TEXT_LIMIT
            out[name] = raw if len(raw) <= limit else raw[:limit]
            continue
        out[name] = slim_json_value(raw, depth + 1)
    return out


def _copy_attribution_summary(out: dict, raw: dict) -> None:
    strength = raw.get("signal_strength")
    if strength is None:
        strength = raw.get("total_score")
    if out.get("signal_strength") is None and strength is not None:
        out["signal_strength"] = strength
    if out.get("total_score") is None and strength is not None:
        out["total_score"] = strength
    mix = raw.get("signal_mix") or raw.get("components")
    if not out.get("signal_mix") and isinstance(mix, dict):
        out["signal_mix"] = slim_json_value(mix, 1)


def _slim_attribution_map(raw: dict, depth: int) -> dict:
    slim = {}
    for index, (sym, row) in enumerate(raw.items()):
        if index >= 40:
            break
        if isinstance(row, dict):
            slim[str(sym)] = _slim_dict(row, depth + 1)
        else:
            slim[str(sym)] = slim_json_value(row, depth + 1)
    return slim


def compact_dashboard_state(state: dict) -> dict:
    """Slim a dashboard projection. Does not mutate the caller's dict."""
    if not isinstance(state, dict):
        return {}
    slim = _slim_dict(state, 0)
    for key, cap in _ROW_LIST_CAPS.items():
        rows = slim.get(key)
        if isinstance(rows, list) and len(rows) > cap:
            slim[key] = rows[:cap]
    usage = slim.get("token_usage")
    if isinstance(usage, dict):
        calls = usage.get("calls_log")
        if isinstance(calls, list) and len(calls) > 20:
            usage["calls_log"] = calls[-20:]
    sources = slim.get("screener_sources")
    if isinstance(sources, dict):
        slim["screener_sources"] = _slim_screener_sources(sources)
    return slim


def _slim_screener_sources(sources: dict) -> dict:
    """Keep pipeline counts and a short attribution summary per bucket."""
    out = {}
    for bucket, info in sources.items():
        if not isinstance(info, dict):
            out[bucket] = slim_json_value(info, 1)
            continue
        kept = {}
        for key, value in info.items():
            if key in _DROP_KEYS or key == "attribution":
                continue
            if isinstance(value, (int, float, bool)) or value is None:
                kept[key] = value
            elif isinstance(value, str):
                kept[key] = value[:_TEXT_LIMIT]
        attr = info.get("attribution")
        if isinstance(attr, dict) and attr:
            kept["attribution"] = _slim_attribution_map(attr, 1)
        out[bucket] = kept
    return out


def shed_optional_sections(state: dict) -> tuple[dict, list[str]]:
    """Drop diagnostic sections. Decisions, positions, fills, and orders stay."""
    shed = dict(state)
    dropped = []
    for key in _SHED_KEYS:
        if key in shed:
            shed.pop(key, None)
            dropped.append(key)
    sources = shed.get("screener_sources")
    if isinstance(sources, dict):
        trimmed = {}
        removed_attr = False
        for bucket, info in sources.items():
            if not isinstance(info, dict):
                trimmed[bucket] = info
                continue
            copy = dict(info)
            if "attribution" in copy:
                copy.pop("attribution", None)
                removed_attr = True
            trimmed[bucket] = copy
        shed["screener_sources"] = trimmed
        if removed_attr:
            dropped.append("screener_sources.attribution")
    return shed, dropped


def encode_projection(state: dict, *, pretty: bool) -> str:
    import json
    if pretty:
        return json.dumps(state, indent=2)
    return json.dumps(state, separators=(",", ":"))


def projection_utf8_size(payload: str) -> int:
    return len(payload.encode("utf-8"))


# Broker order facts worth keeping. Everything else (dashboard state,
# screener dumps, OHLCV, LLM bodies, and a previous raw_json string) is how
# orders.raw_json grew to multiple megabytes per row.
_ORDER_SCALAR_KEYS = (
    "id",
    "order_id",
    "client_order_id",
    "alpaca_order_id",
    "symbol",
    "ticker",
    "side",
    "qty",
    "filled_qty",
    "filled_avg_price",
    "notional",
    "notional_usd",
    "shares",
    "type",
    "order_type",
    "time_in_force",
    "order_class",
    "limit_price",
    "stop_price",
    "trail_price",
    "trail_percent",
    "hwm",
    "status",
    "submitted_at",
    "created_at",
    "filled_at",
    "updated_at",
    "expired_at",
    "canceled_at",
    "cancelled_at",
    "asset_class",
    "asset_id",
    "strategy_name",
    "bucket",
    "stop_loss_price",
    "take_profit_price",
    "analysis_path",
    "rationale",
    "source",
    "entry_source",
    "candidate_source",
    "extended_hours",
)
_ORDER_TEXT_CAP = 500
MAX_ORDER_RAW_BYTES = 8 * 1024


def _order_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _ORDER_TEXT_CAP else value[:_ORDER_TEXT_CAP]
    return None


def _order_scalars(row: dict) -> dict:
    """Copy allowlisted scalars. Never follow raw_json, legs, or state blobs."""
    out = {}
    for key in _ORDER_SCALAR_KEYS:
        if key not in row:
            continue
        kept = _order_scalar(row.get(key))
        if kept is not None:
            out[key] = kept
    return out


def normalize_order_payload(order: Any) -> dict:
    """Broker order reduced to KB-scale fields.

    A previous ``raw_json`` value is ignored. Re-saving a loaded SQLite row
    used to embed that string inside the next ``raw_json``, which doubled the
    blob on every dashboard publish once Alpaca returned no open orders.
    Nested screener, OHLCV, and LLM bodies are dropped the same way.
    """
    data = order
    if isinstance(order, str):
        if not order.strip():
            return {}
        try:
            import json
            data = json.loads(order)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out = _order_scalars(data)
    legs = data.get("legs")
    if isinstance(legs, list):
        slim_legs = []
        for leg in legs[:4]:
            if isinstance(leg, dict):
                slim = _order_scalars(leg)
                if slim:
                    slim_legs.append(slim)
        if slim_legs:
            out["legs"] = slim_legs
    for nest_key, fields in (("stop_loss", ("stop_price", "limit_price")), ("take_profit", ("limit_price", "stop_price"))):
        sub = data.get(nest_key)
        if not isinstance(sub, dict):
            continue
        kept = {}
        for field in fields:
            scalar = _order_scalar(sub.get(field))
            if scalar is not None:
                kept[field] = scalar
        if kept:
            out[nest_key] = kept
    return out


def order_raw_json(order: Any) -> str:
    """Compact JSON for ``orders.raw_json``. Always at most 8 KiB."""
    import json

    payload = normalize_order_payload(order)
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_ORDER_RAW_BYTES:
        return encoded
    payload.pop("legs", None)
    payload.pop("rationale", None)
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_ORDER_RAW_BYTES:
        return encoded
    tiny = {
        key: payload[key]
        for key in ("id", "order_id", "symbol", "ticker", "side", "qty", "status", "stop_price", "limit_price")
        if key in payload
    }
    return json.dumps(tiny, separators=(",", ":"))


def fit_dashboard_projection(state: dict, limit: int) -> tuple[Optional[str], dict]:
    """Serialize state under ``limit`` bytes.

    Returns (payload, info). payload is None only when even the shed
    projection exceeds the cap. info describes what was kept or dropped.
    """
    compact = compact_dashboard_state(state if isinstance(state, dict) else {})
    info = {"shed": [], "pretty": True, "compacted": True}
    pretty = encode_projection(compact, pretty=True)
    if projection_utf8_size(pretty) <= limit:
        info["bytes"] = projection_utf8_size(pretty)
        return pretty, info
    dense = encode_projection(compact, pretty=False)
    if projection_utf8_size(dense) <= limit:
        info["pretty"] = False
        info["bytes"] = projection_utf8_size(dense)
        return dense, info
    shed, dropped = shed_optional_sections(compact)
    info["shed"] = dropped
    info["pretty"] = False
    dense = encode_projection(shed, pretty=False)
    size = projection_utf8_size(dense)
    info["bytes"] = size
    if size <= limit:
        return dense, info
    return None, info
