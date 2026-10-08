"""America/Chicago trading day and the daily trade count.

The host clock is often UTC. Alpaca timestamps are UTC as well. The operator's
day, including the 10:30 CT cycle, is America/Chicago. Partial fills of one
order are one trade. Exits and protective bracket legs are not trades.
Cancelled, rejected, expired, failed, and skipped orders are not trades.

AgentTrade stamps ``agenttrade-`` on ``client_order_id``. The shared count
also accepts broker ids recorded on orders AgentTrade itself submitted.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

TRADING_TIMEZONE_NAME = "America/Chicago"
TRADING_TZ = ZoneInfo(TRADING_TIMEZONE_NAME)
# Cron and run_cycle.sh force this zone. Naive last_run stamps are that clock,
# even when the config server process is in another zone.
CYCLE_CLOCK_TIMEZONE_NAME = "America/New_York"
CYCLE_CLOCK_TZ = ZoneInfo(CYCLE_CLOCK_TIMEZONE_NAME)

# Alpaca client_order_id is limited to 48 characters. The prefix plus a
# 32-char hex uuid is 43.
CLIENT_ORDER_PREFIX = "agenttrade-"

# Statuses that must not consume a daily trade slot.
_IGNORED_ORDER_STATUSES = frozenset({
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "replaced",
    "suspended",
    "failed",
    "skipped",
    "blocked",
    "done_for_day",
    "pending_cancel",
    "pending_replace",
})

# Resting stop / take-profit children of a bracket are not new trades.
_PROTECTIVE_ORDER_CLASSES = frozenset({"bracket", "oco", "oto"})
_PROTECTIVE_SELL_TYPES = frozenset({"stop", "stop_limit", "trailing_stop"})

_FRACTIONAL_SECONDS = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tail>Z|[+-]\d{2}:?\d{2})?$"
)


def parse_timestamp(value) -> Optional[datetime]:
    """Parse an Alpaca or ledger timestamp as timezone-aware UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        match = _FRACTIONAL_SECONDS.match(text)
        if match:
            frac = (match.group("frac") or "")[:6]
            tail = match.group("tail") or ""
            if tail == "Z":
                tail = "+00:00"
            elif len(tail) == 5 and tail[3] != ":":
                tail = tail[:3] + ":" + tail[3:]
            text = match.group("head") + (f".{frac}" if frac else "") + tail
        text = text.replace(" ", "T")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def trading_day_bounds(now=None) -> tuple[datetime, datetime]:
    """UTC instants covering the America/Chicago calendar day that contains ``now``."""
    moment = parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
    if moment is None:
        moment = datetime.now(timezone.utc)
    local = moment.astimezone(TRADING_TZ)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def chicago_trading_date(now=None):
    """The America/Chicago date that contains ``now``."""
    start, _end = trading_day_bounds(now)
    return start.astimezone(TRADING_TZ).date()


def host_local_timezone():
    """Timezone of naive ``datetime.now()`` stamps on this host."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def aware_now_iso() -> str:
    """UTC timestamp with an explicit offset. Safe to store and compare."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_cycle_timestamp(value) -> Optional[datetime]:
    """Parse a cycle clock as an aware UTC instant.

    New stamps include an offset. Older ``last_run`` values were written with
    ``datetime.now().isoformat()`` under ``TZ=America/New_York`` (cron and
    ``run_cycle.sh``). The config server does not set that variable. Reading
    11:32 Eastern as Chicago displays 11:32 CT instead of 10:32 CT.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00").replace(" ", "T")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CYCLE_CLOCK_TZ)
    return dt.astimezone(timezone.utc)


def format_chicago(dt: datetime) -> str:
    """Operator-facing clock. Always America/Chicago, labeled CT."""
    return dt.astimezone(TRADING_TZ).strftime("%Y-%m-%d %H:%M CT")


def hours_since(dt: datetime, now=None) -> float:
    """Elapsed hours between an aware instant and ``now`` (default: UTC now)."""
    if now is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        moment = parse_cycle_timestamp(now) or datetime.now(timezone.utc)
    return (moment.astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


def new_client_order_id() -> str:
    """Client order id Alpaca will store for an AgentTrade submission."""
    return CLIENT_ORDER_PREFIX + uuid.uuid4().hex


def new_manual_client_order_id(symbol: str) -> str:
    """Client id for an operator close. Alpaca allows 48 characters.

    ``agenttrade-manual-`` still counts as ours for the sell cooldown.
    """
    slug = "".join(ch for ch in str(symbol or "").lower() if ch.isalnum())[:12] or "exit"
    prefix = f"{CLIENT_ORDER_PREFIX}manual-stop-{slug}-"
    room = 48 - len(prefix)
    token = uuid.uuid4().hex[: max(4, min(8, room))]
    return (prefix + token)[:48]


def client_order_id_is_ours(value) -> bool:
    return str(value or "").startswith(CLIENT_ORDER_PREFIX)


def in_trading_day(ts, now=None) -> bool:
    dt = parse_timestamp(ts)
    if dt is None:
        return False
    start, end = trading_day_bounds(now)
    return start <= dt < end


def _load_raw(fill: dict) -> dict:
    raw = fill.get("raw_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _activity_ids(row: dict) -> set[str]:
    """Ids that identify a fill activity, not the broker order."""
    parsed = _load_raw(row)
    found = set()
    for value in (
        row.get("alpaca_fill_id"),
        parsed.get("id") if parsed.get("order_id") else None,
    ):
        if value:
            found.add(str(value))
    # ``id`` is the activity id on Alpaca fill payloads. On an order payload
    # it is the broker order id, so only treat it as an activity when a
    # distinct order id is also present or the row is explicitly a fill.
    row_id = row.get("id")
    order_id = row.get("order_id") or parsed.get("order_id")
    if row_id and (order_id or row.get("alpaca_fill_id") or row.get("source") == "alpaca_fill"):
        if not order_id or str(row_id) != str(order_id):
            found.add(str(row_id))
    return found


def _explicit_order_ids(row: dict) -> list[str]:
    parsed = _load_raw(row)
    found = []
    for value in (
        parsed.get("order_id"),
        row.get("order_id"),
        row.get("alpaca_order_id"),
    ):
        if value and str(value) not in found:
            found.append(str(value))
    return found


def broker_order_id_from_fill(fill: dict) -> Optional[str]:
    """Real Alpaca order id, never the fill activity id."""
    if not isinstance(fill, dict):
        return None
    activities = _activity_ids(fill)
    for candidate in _explicit_order_ids(fill):
        if candidate not in activities:
            return candidate
    return None


def broker_order_aliases(fills: list) -> dict[str, str]:
    """Map a fill activity id to the broker order id when any row has both.

    Ledger rows often stored the activity id in ``alpaca_order_id``. A live
    fill from the same activity carries the real order UUID in ``order_id``.
    Partial fills share that UUID and must collapse to it.
    """
    alias: dict[str, str] = {}
    for fill in fills or []:
        if not isinstance(fill, dict):
            continue
        activities = _activity_ids(fill)
        broker_id = broker_order_id_from_fill(fill)
        if not broker_id:
            continue
        for activity_id in activities:
            if activity_id != broker_id:
                alias[activity_id] = broker_id
        stored = str(fill.get("alpaca_order_id") or "")
        if stored and stored in activities and stored != broker_id:
            alias[stored] = broker_id
    return alias


def fill_order_key(fill: dict, aliases: Optional[dict] = None) -> str:
    """One key per broker order, collapsing partial fills.

    Legacy rows stored the Alpaca activity id in ``order_id``. When the raw
    activity still carries the real order id, that id wins. ``aliases`` maps
    an activity id seen on another row (usually ``state.recent_fills``) onto
    that order UUID.
    """
    if not isinstance(fill, dict):
        return ""
    activities = _activity_ids(fill)
    candidates = list(_explicit_order_ids(fill))
    row_id = str(fill.get("id") or "")
    # A payload whose only id is the broker order id (no separate activity id)
    # must keep that id. An activity id is not a candidate once a real order
    # id is known.
    if row_id and row_id not in activities and row_id not in candidates:
        candidates.append(row_id)
    for candidate in candidates:
        if candidate not in activities:
            key = candidate
            break
    else:
        if activities:
            key = min(activities)
        else:
            symbol = fill.get("symbol") or fill.get("ticker") or ""
            ts = fill.get("filled_at") or fill.get("submitted_at") or fill.get("transaction_time") or ""
            key = f"{symbol}|{ts}"
    if aliases and key in aliases:
        return aliases[key]
    return key


def shape_order_for_count(order: dict) -> dict:
    """Alpaca-shaped order for the counter.

    Ledger rows store the broker id in ``alpaca_order_id`` and use ``id`` for
    the local row. The counter keys on the broker id.
    """
    if not isinstance(order, dict):
        return {}
    broker_id = order.get("alpaca_order_id")
    if broker_id and not order.get("order_id"):
        adapted = dict(order)
        adapted["id"] = str(broker_id)
        if not adapted.get("type") and order.get("order_type"):
            adapted["type"] = order["order_type"]
        return adapted
    return dict(order)


def _status(row: dict) -> str:
    return str(row.get("status") or "").strip().lower()


def _is_ignored_status(status: str) -> bool:
    return status in _IGNORED_ORDER_STATUSES


def _is_protective_child(order: dict) -> bool:
    """Bracket / OCO / OTO sell legs and resting stops are not extra trades."""
    if order.get("parent_order_id") or order.get("parent_id"):
        return True
    side = str(order.get("side") or "").strip().lower()
    klass = str(order.get("order_class") or "").strip().lower()
    order_type = str(order.get("type") or order.get("order_type") or "").strip().lower()
    if side == "sell" and klass in _PROTECTIVE_ORDER_CLASSES:
        return True
    if side == "sell" and order_type in _PROTECTIVE_SELL_TYPES:
        return True
    return False


def _leg_ids(open_orders: list) -> set[str]:
    found = set()
    for order in open_orders or []:
        if not isinstance(order, dict):
            continue
        for leg in order.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            leg_id = leg.get("id") or leg.get("order_id")
            if leg_id:
                found.add(str(leg_id))
    return found


def _order_type(order: dict) -> str:
    return str(order.get("type") or order.get("order_type") or "").strip().lower()


def _order_broker_id(order: dict) -> str:
    for key in ("id", "order_id", "alpaca_order_id"):
        value = order.get(key)
        if value:
            return str(value)
    return ""


def _in_chicago_day(ts, start, end) -> bool:
    moment = parse_timestamp(ts)
    return moment is not None and start <= moment < end


def _prefer_status(statuses: list) -> str:
    """Live Alpaca status wins over a stored open-order snapshot."""
    live = [status for origin, status in statuses if origin == "live" and status]
    pool = live or [status for _origin, status in statuses if status]
    for status in pool:
        if _is_ignored_status(status):
            return status
    return pool[-1] if pool else ""


def _live_terminal_status(statuses: list) -> str:
    for origin, status in statuses:
        if origin == "live" and status and _is_ignored_status(status):
            return status
    return ""


def _exit_reason(group: dict) -> str:
    """Why a sell is not a daily trade. Take-profit limits are named explicitly."""
    order_type = str(group.get("order_type") or "").lower()
    if order_type in _PROTECTIVE_SELL_TYPES:
        reason = "protective stop leg"
    elif order_type == "limit" or str(group.get("order_class") or "").lower() in _PROTECTIVE_ORDER_CLASSES:
        reason = "exit / protective take-profit leg"
    elif group.get("is_leg"):
        reason = "protective bracket leg"
    else:
        reason = "exit (sells do not count toward the daily cap)"
    live_terminal = _live_terminal_status(group.get("statuses") or [])
    if live_terminal and live_terminal not in reason:
        reason = f"{reason}; broker status {live_terminal}"
    return reason


def _blank_group(order_id: str) -> dict:
    return {
        "order_id": order_id,
        "symbol": "",
        "side": "",
        "order_type": "",
        "order_class": "",
        "client_order_id": "",
        "strategy_name": "",
        "timestamps": [],
        "in_day": False,
        "merges": [],
        "statuses": [],
        "has_fill": False,
        "owned_hint": False,
        "is_leg": False,
        "is_exit": False,
    }


def _note_identity(group: dict, row: dict) -> None:
    symbol = row.get("symbol") or row.get("ticker") or ""
    if symbol and not group["symbol"]:
        group["symbol"] = str(symbol)
    side = str(row.get("side") or "").strip().lower()
    if side == "sell" or not group["side"]:
        if side:
            group["side"] = side
    order_type = _order_type(row)
    if order_type:
        group["order_type"] = order_type
    klass = str(row.get("order_class") or "").strip().lower()
    if klass and not group["order_class"]:
        group["order_class"] = klass
    client_order_id = str(row.get("client_order_id") or "").strip()
    if not client_order_id:
        client_order_id = str(_load_raw(row).get("client_order_id") or "").strip()
    if client_order_id and not group["client_order_id"]:
        group["client_order_id"] = client_order_id
    strategy = str(row.get("strategy_name") or "").strip()
    if strategy and not group["strategy_name"]:
        group["strategy_name"] = strategy
    if client_order_id_is_ours(group["client_order_id"]) or group["strategy_name"]:
        group["owned_hint"] = True
    if side == "sell" or _is_protective_child(row):
        group["is_exit"] = True


def explain_trades_for_day(
    fills: list,
    open_orders: list,
    now=None,
    owned_order_ids: Optional[set] = None,
) -> dict:
    """Classify each order as counted or excluded for the Chicago day.

    ``owned_order_ids`` limits the count to AgentTrade submissions. ``None``
    keeps the older unfiltered entry count (tests and callers that already
    passed only AgentTrade rows). An empty set still counts a row whose
    ``client_order_id`` uses the AgentTrade prefix.

    Activity ids are merged into the broker order UUID before the decision,
    so a ledger fill and ``state.recent_fills`` row for the same order count
    once, and partial fills collapse.
    """
    start, end = trading_day_bounds(now)
    aliases = broker_order_aliases(fills)
    groups: dict[str, dict] = {}
    legs = _leg_ids(open_orders)

    def _group(resolved: str) -> dict:
        return groups.setdefault(resolved, _blank_group(resolved))

    for fill in fills or []:
        if not isinstance(fill, dict):
            continue
        raw_key = fill_order_key(fill)
        resolved = aliases.get(raw_key, raw_key)
        if not resolved:
            continue
        group = _group(resolved)
        _note_identity(group, fill)
        group["has_fill"] = True
        ts = fill.get("filled_at") or fill.get("submitted_at") or fill.get("transaction_time") or ""
        if ts:
            group["timestamps"].append(str(ts))
            if _in_chicago_day(ts, start, end):
                group["in_day"] = True
        status = _status(fill)
        if status:
            group["statuses"].append(("fill", status))
        qty = fill.get("qty")
        if qty in (None, ""):
            qty = fill.get("shares")
        if raw_key != resolved and not any(item["activity_id"] == raw_key for item in group["merges"]):
            group["merges"].append({
                "activity_id": raw_key,
                "order_id": resolved,
                "qty": qty,
                "symbol": group["symbol"],
                "source": str(fill.get("_count_origin") or "fill"),
            })

    for order in open_orders or []:
        if not isinstance(order, dict):
            continue
        raw_key = _order_broker_id(order)
        if not raw_key:
            continue
        resolved = aliases.get(raw_key, raw_key)
        group = _group(resolved)
        _note_identity(group, order)
        if raw_key in legs or resolved in legs:
            group["is_leg"] = True
            group["is_exit"] = True
        origin = str(order.get("_count_origin") or "order")
        status = _status(order) or "open"
        group["statuses"].append((origin, status))
        ts = order.get("submitted_at") or order.get("created_at") or ""
        if ts:
            group["timestamps"].append(str(ts))
            if _in_chicago_day(ts, start, end):
                group["in_day"] = True

    counted = []
    excluded = []
    for order_id, group in groups.items():
        status = _prefer_status(group["statuses"])
        group["status"] = status
        group["merges"].sort(key=lambda item: str(item.get("activity_id") or ""))
        owned = True
        if owned_order_ids is not None:
            owned = (
                order_id in owned_order_ids
                or group["owned_hint"]
                or client_order_id_is_ours(group["client_order_id"])
            )
        if not group["in_day"]:
            reason = "outside America/Chicago day"
        elif group["is_exit"] or group["is_leg"] or group["side"] == "sell":
            reason = _exit_reason(group)
        elif not group["has_fill"] and status and _is_ignored_status(status):
            reason = f"status {status}"
        elif not owned:
            reason = "not an AgentTrade order"
        else:
            reason = ""
        row = {
            "order_id": order_id,
            "symbol": group["symbol"],
            "side": group["side"],
            "status": status,
            "order_type": group["order_type"],
            "order_class": group["order_class"],
            "client_order_id": group["client_order_id"],
            "merges": group["merges"],
            "reason": reason,
        }
        if reason:
            excluded.append(row)
        else:
            counted.append(row)

    counted.sort(key=lambda row: (str(row.get("symbol") or ""), str(row.get("order_id") or "")))
    excluded.sort(key=lambda row: (str(row.get("reason") or ""), str(row.get("symbol") or ""), str(row.get("order_id") or "")))
    return {
        "day": chicago_trading_date(now).isoformat(),
        "count": len(counted),
        "counted": counted,
        "excluded": excluded,
    }


def count_trades_for_day(
    fills: list,
    open_orders: list,
    now=None,
    owned_order_ids: Optional[set] = None,
) -> int:
    """Distinct AgentTrade entries in the America/Chicago day.

    A partial fill and its still-open parent count once. Activity ids stored
    on ledger fills collapse into the broker order id. Cancelled, rejected,
    expired, failed, and skipped orders do not count. Sells, stops, and
    take-profit legs do not count: the cap limits entries.
    """
    return explain_trades_for_day(
        fills,
        open_orders,
        now=now,
        owned_order_ids=owned_order_ids,
    )["count"]


def _format_qty(qty) -> str:
    try:
        number = float(qty)
    except (TypeError, ValueError):
        return str(qty)
    if number.is_integer():
        return str(int(number))
    return str(number)


def format_trade_report(report: dict, limit: Optional[int] = None) -> str:
    """Plain-text diagnostic. Read-only: it does not rewrite the ledger."""
    day = report.get("day") or ""
    count = int(report.get("count") or 0)
    header = f"America/Chicago {day}: {count} counted"
    if limit is not None:
        header += f" / cap {int(limit)}"
    lines = [header, "", "COUNTED"]
    counted = report.get("counted") or []
    if not counted:
        lines.append("  (none)")
    for row in counted:
        lines.append(
            f"  {row.get('symbol') or '—'} {row.get('side') or 'buy'} "
            f"{row.get('order_id')} status={row.get('status') or '—'}"
        )
        for merge in row.get("merges") or []:
            qty = merge.get("qty")
            qty_bit = f" qty {_format_qty(qty)}" if qty not in (None, "") else ""
            source = merge.get("source") or "fill"
            lines.append(
                f"    merge activity {merge.get('activity_id')} -> "
                f"{merge.get('order_id')}{qty_bit} ({source})"
            )
    lines.extend(["", "EXCLUDED"])
    excluded = report.get("excluded") or []
    if not excluded:
        lines.append("  (none)")
    for row in excluded:
        lines.append(
            f"  {row.get('symbol') or '—'} {row.get('side') or '—'} "
            f"{row.get('order_id')} type={row.get('order_type') or '—'} "
            f"status={row.get('status') or '—'} "
            f"reason: {row.get('reason')}"
        )
        for merge in row.get("merges") or []:
            lines.append(
                f"    merge activity {merge.get('activity_id')} -> {merge.get('order_id')}"
            )
    return "\n".join(lines)
