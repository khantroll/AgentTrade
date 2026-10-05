"""America/Chicago trading day and the daily trade count.

The host clock is often UTC. Alpaca timestamps are UTC as well. The operator's
day, including the 10:30 CT cycle, is America/Chicago. Partial fills of one
order and protective bracket legs are not extra trades. Cancelled, rejected,
expired, failed, and skipped orders are not trades.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

TRADING_TIMEZONE_NAME = "America/Chicago"
TRADING_TZ = ZoneInfo(TRADING_TIMEZONE_NAME)

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


def fill_order_key(fill: dict) -> str:
    """One key per broker order, collapsing partial fills.

    Legacy rows stored the Alpaca activity id in ``order_id``. When the raw
    activity still carries the real order id, that id wins.
    """
    if not isinstance(fill, dict):
        return ""
    parsed = _load_raw(fill)
    activity_ids = {
        str(value)
        for value in (
            fill.get("id"),
            fill.get("alpaca_fill_id"),
            parsed.get("id"),
        )
        if value
    }
    candidates = (
        parsed.get("order_id"),
        fill.get("order_id"),
        fill.get("alpaca_order_id"),
    )
    for candidate in candidates:
        if candidate and str(candidate) not in activity_ids:
            return str(candidate)
    for candidate in candidates:
        if candidate:
            return str(candidate)
    if activity_ids:
        return min(activity_ids)
    symbol = fill.get("symbol") or fill.get("ticker") or ""
    ts = fill.get("filled_at") or fill.get("submitted_at") or fill.get("transaction_time") or ""
    return f"{symbol}|{ts}"


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


def count_trades_for_day(fills: list, open_orders: list, now=None) -> int:
    """Distinct live orders in the America/Chicago day.

    A partial fill and its still-open parent count once. Cancelled, rejected,
    expired, failed, and skipped orders do not count. Protective sell legs
    do not count in addition to the entry that created them.
    """
    start, end = trading_day_bounds(now)
    seen: set[str] = set()
    legs = _leg_ids(open_orders)

    def _add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)

    for fill in fills or []:
        if not isinstance(fill, dict):
            continue
        status = _status(fill)
        if status and _is_ignored_status(status):
            continue
        ts = fill.get("filled_at") or fill.get("submitted_at") or fill.get("transaction_time") or ""
        moment = parse_timestamp(ts)
        if moment is None or not (start <= moment < end):
            continue
        _add(fill_order_key(fill))

    for order in open_orders or []:
        if not isinstance(order, dict):
            continue
        status = _status(order) or "open"
        if _is_ignored_status(status):
            continue
        order_id = str(order.get("id") or order.get("order_id") or "")
        if not order_id or order_id in legs or order_id in seen:
            continue
        if _is_protective_child(order):
            continue
        ts = order.get("submitted_at") or order.get("created_at") or ""
        moment = parse_timestamp(ts)
        if moment is None or not (start <= moment < end):
            continue
        _add(order_id)

    return len(seen)
