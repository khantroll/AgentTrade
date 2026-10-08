"""Opportunity-cost rebalance.

Each cycle rescores holdings on the same signal_strength scale as candidates,
then chooses stay, decay exit, fade trim/close, or a swap into a stronger name.

Sells are decisions only until ``execute_opportunity_sells``. They do not
consume MAX_DAILY_TRADES. Buys stay on the normal path and still see the cash
reserve, bucket limits, and post-sell cooldown.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Defaults. The on/off switch is Settings / config.json when that key is
# saved there; that saved value wins over .env. A missing config.json key
# does not overwrite an explicit OPPORTUNITY_REBALANCE in the environment.
# If neither source sets it, rebalance stays on. The numeric knobs are
# environment values and are not written into config.json by this default.
DEFAULTS = {
    "enabled": True,
    "decay_score": 25.0,
    "decay_drop": 20.0,
    "swap_margin": 15.0,
    "swap_cost_points": 3.0,
    "min_hold_hours": 24.0,
    "max_swaps_per_day": 1,
    "fade_min_gain": 0.03,
    "fade_drop": 8.0,
    "fade_trim_pct": 0.33,
    "fade_close_drop": 15.0,
}

_SELL_ACTIONS = frozenset({"decay_exit", "fade_close", "fade_trim", "swap_sell"})
_FULL_EXIT_ACTIONS = frozenset({"decay_exit", "fade_close", "swap_sell"})


def load_settings() -> dict:
    """Current knobs. Missing keys use DEFAULTS. The toggle defaults to on."""
    return {
        "enabled": _env_flag("OPPORTUNITY_REBALANCE", DEFAULTS["enabled"]),
        "decay_score": _env_float("OPPORTUNITY_DECAY_SCORE", DEFAULTS["decay_score"]),
        "decay_drop": _env_float("OPPORTUNITY_DECAY_DROP", DEFAULTS["decay_drop"]),
        "swap_margin": _env_float("OPPORTUNITY_SWAP_MARGIN", DEFAULTS["swap_margin"]),
        "swap_cost_points": _env_float("OPPORTUNITY_SWAP_COST_POINTS", DEFAULTS["swap_cost_points"]),
        "min_hold_hours": _env_float("OPPORTUNITY_MIN_HOLD_HOURS", DEFAULTS["min_hold_hours"]),
        "max_swaps_per_day": int(_env_float("OPPORTUNITY_MAX_SWAPS_PER_DAY", DEFAULTS["max_swaps_per_day"])),
        "fade_min_gain": _env_float("OPPORTUNITY_FADE_MIN_GAIN", DEFAULTS["fade_min_gain"]),
        "fade_drop": _env_float("OPPORTUNITY_FADE_DROP", DEFAULTS["fade_drop"]),
        "fade_trim_pct": _env_float("OPPORTUNITY_FADE_TRIM_PCT", DEFAULTS["fade_trim_pct"]),
        "fade_close_drop": _env_float("OPPORTUNITY_FADE_CLOSE_DROP", DEFAULTS["fade_close_drop"]),
    }


def client_id_owns_position(client_order_id: str) -> bool:
    """True for an AgentTrade entry id.

    ``agenttrade-`` marks our orders. ``agenttrade-manual-`` is a hand order
    on the shared account and does not make the position ours to rebalance.
    CryptoAgent and other prefixes are not ours.
    """
    cid = str(client_order_id or "").strip()
    if not cid.startswith("agenttrade-"):
        return False
    if cid.startswith("agenttrade-manual-"):
        return False
    return True


def symbol_forms(symbol: str) -> set[str]:
    """``SOLUSD`` and ``SOL/USD`` are one pair for ownership and score lookup."""
    sym = "".join(str(symbol or "").upper().split())
    if not sym:
        return set()
    forms = {sym}
    if "/" in sym:
        forms.add(sym.replace("/", ""))
    elif sym.endswith("USD") and len(sym) > 3:
        forms.add(f"{sym[:-3]}/USD")
    return forms


def symbols_match(left: str, right: str) -> bool:
    return bool(symbol_forms(left) & symbol_forms(right))


def position_owned_by_agenttrade(symbol: str, ledger_rows: list) -> bool:
    """True when a ledger buy for this symbol carries our entry client id."""
    wanted = symbol_forms(symbol)
    if not wanted:
        return False
    for row in ledger_rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("side") or "").lower() not in ("buy", "long"):
            continue
        row_sym = str(row.get("symbol") or row.get("ticker") or "")
        if not (symbol_forms(row_sym) & wanted):
            continue
        if client_id_owns_position(_row_client_id(row)):
            return True
    return False


def plan_rebalance(
    holdings: list,
    candidates: list,
    settings: dict,
    *,
    now: Optional[datetime] = None,
    swaps_today: int = 0,
    bought_this_cycle: Optional[set] = None,
    locked_symbols: Optional[set] = None,
) -> list:
    """Compare holdings with candidates. Pure: no orders are sent.

    Every holding produces a decision with both scores and a reason. Foreign
    positions are recorded as ``not_owned`` and never sold.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    bought = {_norm(s) for s in (bought_this_cycle or set())}
    locked = set()
    for s in locked_symbols or set():
        locked |= symbol_forms(s)

    if not settings.get("enabled", True):
        return [{
            "action": "disabled",
            "reason": "OPPORTUNITY_REBALANCE is off",
            "symbol": "",
            "score": None,
            "entry_score": None,
            "candidate": "",
            "candidate_score": None,
        }]

    held_forms = set()
    for holding in holdings or []:
        held_forms |= symbol_forms(holding.get("symbol"))

    decisions = []
    exiting = set()
    for holding in holdings or []:
        decision = _decide_holding(holding, settings, now, bought)
        decisions.append(decision)
        if decision["action"] in _SELL_ACTIONS:
            exiting |= symbol_forms(decision["symbol"])

    room = max(0, int(settings.get("max_swaps_per_day") or 0) - int(swaps_today or 0))
    if room <= 0:
        decisions.append({
            "action": "hold",
            "symbol": "",
            "reason": f"swap cap reached ({swaps_today} today)",
            "score": None,
            "entry_score": None,
            "candidate": "",
            "candidate_score": None,
        })
        return decisions

    eligible = []
    for holding in holdings or []:
        sym = str(holding.get("symbol") or "")
        if symbol_forms(sym) & exiting:
            continue
        if not holding.get("owned"):
            continue
        if holding.get("score") is None:
            continue
        if _was_bought_this_cycle(sym, bought):
            continue
        if not _held_long_enough(holding, settings, now):
            continue
        eligible.append(holding)
    if not eligible:
        return decisions

    eligible.sort(key=lambda h: (float(h["score"]), str(h.get("symbol") or "")))
    used_candidates = set()
    for holding in eligible:
        if room <= 0:
            break
        weakest_score = float(holding["score"])
        hurdle = weakest_score + float(settings["swap_margin"]) + float(settings["swap_cost_points"])
        best = _best_candidate(
            candidates,
            bucket=str(holding.get("bucket") or ""),
            held_forms=held_forms,
            locked=locked,
            used=used_candidates,
        )
        if best is None:
            decisions.append(_decision(
                "hold", holding, reason="no same-bucket candidate to swap into",
            ))
            continue
        best_score = float(best["score"])
        if best_score + 1e-9 < hurdle:
            decisions.append(_decision(
                "hold",
                holding,
                reason=(
                    f"swap skipped: {best.get('symbol')} {best_score:.1f} does not clear "
                    f"{holding.get('symbol')} {weakest_score:.1f} + margin {settings['swap_margin']:.0f} "
                    f"+ cost {settings['swap_cost_points']:.0f}"
                ),
                candidate=best.get("symbol"),
                candidate_score=best_score,
            ))
            continue
        decisions.append(_decision(
            "swap_sell",
            holding,
            reason=(
                f"swap: sell {holding.get('symbol')} {weakest_score:.1f} for "
                f"{best.get('symbol')} {best_score:.1f} "
                f"(margin {settings['swap_margin']:.0f} + fees/slippage {settings['swap_cost_points']:.0f})"
            ),
            candidate=best.get("symbol"),
            candidate_score=best_score,
            qty=_full_qty(holding),
        ))
        decisions.append({
            "action": "swap_buy",
            "symbol": str(best.get("symbol") or ""),
            "bucket": holding.get("bucket") or best.get("bucket") or "",
            "score": best_score,
            "entry_score": weakest_score,
            "candidate": holding.get("symbol"),
            "candidate_score": weakest_score,
            "reason": (
                f"swap target {best.get('symbol')} {best_score:.1f} replaces "
                f"{holding.get('symbol')} {weakest_score:.1f}; buy stays on the normal entry path"
            ),
        })
        used_candidates |= symbol_forms(best.get("symbol"))
        exiting |= symbol_forms(holding.get("symbol"))
        room -= 1
    return decisions


def sell_orders_from_decisions(decisions: list) -> list:
    """Sell intents only. Swap buys are not orders."""
    out = []
    for row in decisions or []:
        if row.get("action") not in _SELL_ACTIONS:
            continue
        if not row.get("owned", True):
            continue
        if row.get("symbol") and float(row.get("qty") or 0) > 0:
            out.append(row)
    return out


def snapshot_with_sell_proceeds(snapshot: dict, orders: list, cash_before: float) -> dict:
    """Cash the buy path should see after same-cycle sells.

    A refresh that already includes the fills is left alone. A refresh that
    still shows the pre-sell cash gets the placed sells' estimated proceeds
    so the reserve check can fund the entries those sells were meant to free.
    """
    proceeds = 0.0
    for order in orders or []:
        if str(order.get("status") or "") != "placed":
            continue
        if str(order.get("side") or "sell").lower() != "sell":
            continue
        proceeds += float(order.get("estimated_proceeds") or 0)
    snap = json.loads(json.dumps(snapshot or {}))
    acct = dict(snap.get("account") or {})
    live_cash = float(acct.get("cash") if acct.get("cash") is not None else snap.get("cash") or 0)
    if live_cash + 0.01 >= float(cash_before) + proceeds:
        return snap
    gap = (float(cash_before) + proceeds) - live_cash
    new_cash = round(live_cash + gap, 2)
    new_bp = round(float(acct.get("buying_power") if acct.get("buying_power") is not None else snap.get("buying_power") or 0) + gap, 2)
    acct["cash"] = new_cash
    acct["buying_power"] = new_bp
    snap["account"] = acct
    snap["cash"] = new_cash
    snap["buying_power"] = new_bp
    snap["opportunity_sell_proceeds"] = round(proceeds, 2)
    return snap


def _decide_holding(holding: dict, settings: dict, now: datetime, bought: set) -> dict:
    sym = str(holding.get("symbol") or "")
    if not holding.get("owned"):
        return _decision(
            "skip", holding,
            reason="not an AgentTrade position (no agenttrade- entry in the ledger)",
            owned=False,
        )
    score = holding.get("score")
    if score is None:
        return _decision(holding=holding, action="hold", reason="no fresh score on the candidate scale; staying")
    score = float(score)
    entry = holding.get("entry_score")
    drop = (float(entry) - score) if entry is not None else None
    if score < float(settings["decay_score"]):
        return _decision(
            "decay_exit", holding,
            reason=f"decay exit: score {score:.1f} below {settings['decay_score']:.0f}",
            qty=_full_qty(holding),
        )
    if drop is not None and drop >= float(settings["decay_drop"]):
        return _decision(
            "decay_exit", holding,
            reason=(
                f"decay exit: score {score:.1f} dropped {drop:.1f} from entry {float(entry):.1f} "
                f"(threshold {settings['decay_drop']:.0f})"
            ),
            qty=_full_qty(holding),
        )
    if _was_bought_this_cycle(sym, bought):
        return _decision(holding=holding, action="hold", reason="bought this cycle; not eligible to swap or fade")
    if not _held_long_enough(holding, settings, now):
        return _decision(
            holding=holding, action="hold",
            reason=f"held under {settings['min_hold_hours']:.0f}h; swap and fade wait",
        )
    gain = float(holding.get("unrealized_pl_pct") or 0)
    sentiment_fading = _sentiment_fading(holding)
    score_fading = drop is not None and drop >= float(settings["fade_drop"])
    if gain >= float(settings["fade_min_gain"]) and (score_fading or sentiment_fading):
        why = []
        if score_fading:
            why.append(f"score faded {drop:.1f} from entry {float(entry):.1f}")
        if sentiment_fading:
            why.append(
                f"sentiment {float(holding.get('sentiment')):.2f} down from "
                f"{float(holding.get('last_sentiment')):.2f}"
            )
        if drop is not None and drop >= float(settings["fade_close_drop"]):
            return _decision(
                "fade_close", holding,
                reason=f"profit-take close ({gain * 100:.1f}% up): " + "; ".join(why),
                qty=_full_qty(holding),
            )
        qty = _trim_qty(holding, float(settings["fade_trim_pct"]))
        action = "fade_close" if qty >= _full_qty(holding) - 1e-9 else "fade_trim"
        verb = "close" if action == "fade_close" else f"trim {settings['fade_trim_pct'] * 100:.0f}%"
        return _decision(
            action, holding,
            reason=f"profit-take {verb} ({gain * 100:.1f}% up): " + "; ".join(why),
            qty=qty,
        )
    return _decision(
        holding=holding, action="hold",
        reason=f"stay: score {score:.1f}" + (f" vs entry {float(entry):.1f}" if entry is not None else ""),
    )


def _best_candidate(candidates, *, bucket, held_forms, locked, used) -> Optional[dict]:
    # A swap stays inside the holding's bucket. An untagged name is not a donor.
    if not bucket:
        return None
    best = None
    best_score = None
    for row in candidates or []:
        if not isinstance(row, dict) or row.get("score") is None:
            continue
        sym = str(row.get("symbol") or row.get("ticker") or "")
        forms = symbol_forms(sym)
        if not forms or forms & held_forms or forms & locked or forms & used:
            continue
        row_bucket = str(row.get("bucket") or "")
        if bucket and row_bucket and row_bucket != bucket:
            continue
        if bucket and not row_bucket:
            continue
        score = float(row["score"])
        if best is None or score > best_score:
            best = row
            best_score = score
    return best


def _decision(action, holding, *, reason, qty=None, candidate="", candidate_score=None, owned=None) -> dict:
    score = holding.get("score")
    entry = holding.get("entry_score")
    return {
        "action": action,
        "symbol": str(holding.get("symbol") or ""),
        "bucket": holding.get("bucket") or "",
        "owned": holding.get("owned") if owned is None else owned,
        "score": None if score is None else round(float(score), 4),
        "entry_score": None if entry is None else round(float(entry), 4),
        "candidate": str(candidate or ""),
        "candidate_score": None if candidate_score is None else round(float(candidate_score), 4),
        "qty": qty,
        "reason": reason,
        "unrealized_pl_pct": holding.get("unrealized_pl_pct"),
    }


def _held_long_enough(holding: dict, settings: dict, now: datetime) -> bool:
    entry_at = _as_dt(holding.get("entry_at"))
    if entry_at is None:
        return False
    hours = (now - entry_at).total_seconds() / 3600
    return hours + 1e-9 >= float(settings["min_hold_hours"])


def _was_bought_this_cycle(symbol: str, bought: set) -> bool:
    forms = symbol_forms(symbol)
    return any(symbols_match(symbol, item) or _norm(item) in {_norm(s) for s in forms} for item in bought)


def _sentiment_fading(holding: dict) -> bool:
    last = holding.get("last_sentiment")
    current = holding.get("sentiment")
    if last is None or current is None:
        return False
    return float(current) < float(last) - 0.05


def _full_qty(holding: dict) -> float:
    return float(holding.get("qty") or 0)


def _trim_qty(holding: dict, pct: float) -> float:
    qty = _full_qty(holding)
    crypto = bool(holding.get("crypto"))
    if crypto:
        trimmed = round(qty * pct, 6)
        return trimmed if trimmed > 0 else qty
    whole = int(qty)
    if whole <= 1:
        return float(whole)
    trimmed = int(whole * pct)
    if trimmed < 1:
        trimmed = 1
    return float(min(trimmed, whole))


def _norm(symbol: str) -> str:
    return "".join(str(symbol or "").upper().split())


def _row_client_id(row: dict) -> str:
    cid = str(row.get("client_order_id") or "").strip()
    if cid:
        return cid
    raw = row.get("raw_json")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if isinstance(raw, dict):
        return str(raw.get("client_order_id") or "").strip()
    return ""


def _as_dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _env_flag(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def index_scores(decisions: list, attributions: Optional[dict]) -> dict:
    """Symbol forms → score and sentiment on the candidate scale."""
    from signal_attribution import resolve_signal_strength

    book: dict = {}
    for sym, attr in (attributions or {}).items():
        if not isinstance(attr, dict):
            continue
        strength = resolve_signal_strength(attr)
        detail = attr.get("reddit_detail") if isinstance(attr.get("reddit_detail"), dict) else {}
        sentiment = detail.get("avg_sentiment") if detail else None
        _remember_score(book, sym, strength, sentiment)
    for row in decisions or []:
        if not isinstance(row, dict):
            continue
        sym = row.get("ticker") or row.get("symbol")
        strength = row.get("signal_strength")
        if strength is None:
            strength = row.get("score")
        _remember_score(book, sym, strength, row.get("sentiment"), row.get("bucket"))
    return book


def run_opportunity_cycle(
    *,
    positions: list,
    decisions: list,
    attributions: Optional[dict] = None,
    tags: Optional[dict] = None,
    buy_lock: Optional[dict] = None,
    market_open: bool = True,
    cycle_started_at=None,
    open_orders: Optional[list] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Score holdings, plan, sell, and persist entry scores.

    Failures are logged and returned. They do not stop the buy pass.
    """
    now = now or datetime.now(timezone.utc)
    settings = load_settings()
    if not settings.get("enabled", True):
        decisions_out = plan_rebalance([], [], settings, now=now)
        log_decisions(decisions_out)
        return {"enabled": False, "decisions": decisions_out, "orders": [], "knobs": settings}

    try:
        book = index_scores(decisions, attributions)
        ledger_rows, entry_times, bought = load_ownership_context(cycle_started_at)
        entry_rows = load_entry_scores()
        locked = set(str(s) for s in (buy_lock or {}).get("locked_symbols") or [])
        holdings = build_holdings(
            positions, ledger_rows, entry_rows, entry_times, book, tags or {},
        )
        candidates = _swap_candidates(decisions, book)
        swaps = swaps_placed_today(now)
        planned = plan_rebalance(
            holdings,
            candidates,
            settings,
            now=now,
            swaps_today=swaps,
            bought_this_cycle=bought,
            locked_symbols=locked,
        )
        log_decisions(planned)
        orders = execute_opportunity_sells(
            planned, positions, market_open=market_open, open_orders=open_orders,
        )
        _persist_after_plan(planned, orders, holdings, entry_rows, entry_times, now)
        placed_swaps = sum(
            1 for order in orders
            if order.get("status") == "placed" and order.get("opportunity_action") == "swap_sell"
        )
        if placed_swaps:
            record_swaps_placed(placed_swaps, now)
        return {"enabled": True, "decisions": planned, "orders": orders, "knobs": settings}
    except Exception as exc:
        log.exception("[Opportunity] rebalance step failed")
        failed = [{
            "action": "hold",
            "symbol": "",
            "score": None,
            "entry_score": None,
            "candidate": "",
            "candidate_score": None,
            "reason": f"opportunity rebalance failed: {exc}",
        }]
        return {"enabled": True, "decisions": failed, "orders": [], "knobs": settings}


def build_holdings(positions, ledger_rows, entry_rows, entry_times, scorebook, tags) -> list:
    holdings = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        symbol = str(pos.get("symbol") or "")
        if not symbol:
            continue
        owned = position_owned_by_agenttrade(symbol, ledger_rows)
        scored = _score_for(scorebook, symbol)
        stored = _match_row(entry_rows, symbol)
        entry_at = None
        if stored and stored.get("entry_at"):
            entry_at = stored.get("entry_at")
        elif owned:
            entry_at = _entry_time_for(entry_times, symbol)
        sentiment = scored.get("sentiment")
        holdings.append({
            "symbol": symbol,
            "owned": owned,
            "score": scored.get("score"),
            "sentiment": None if sentiment is None else float(sentiment),
            "entry_score": None if not stored else stored.get("entry_score"),
            "entry_at": entry_at,
            "last_sentiment": None if not stored else stored.get("last_sentiment"),
            "bucket": _bucket_for_symbol(symbol, tags) or scored.get("bucket") or "",
            "qty": _position_qty(pos),
            "price": _position_price(pos),
            "unrealized_pl_pct": _float_or_none(pos.get("unrealized_plpc")),
            "crypto": _position_is_crypto(pos),
        })
    return holdings


def execute_opportunity_sells(decisions, positions, *, market_open: bool, open_orders: Optional[list] = None) -> list:
    """Place market sells. Does not increment the daily entry counter.

    Equity waits for the stock session. Crypto uses ``gtc`` even when the
    equity session is closed, matched by symbol or asset class.
    """
    from alpaca_client import alpaca_post, has_pending_sell_order
    from order_utils import format_qty_for_asset, is_crypto_symbol
    from trading_day import new_client_order_id

    if open_orders is None:
        from alpaca_client import get_open_orders
        try:
            open_orders = get_open_orders()
        except Exception as exc:
            log.warning("[Opportunity] open orders unavailable: %s", exc)
            open_orders = []

    orders = []
    for decision in sell_orders_from_decisions(decisions):
        symbol = str(decision.get("symbol") or "")
        pos = _position_for(positions, symbol) or {}
        crypto = _position_is_crypto(pos) or is_crypto_symbol(symbol)
        qty = decision.get("qty")
        base = {
            "ticker": symbol,
            "symbol": symbol,
            "shares": qty,
            "qty": qty,
            "side": "sell",
            "bucket": decision.get("bucket") or "",
            "rationale": decision.get("reason") or "",
            "source": "opportunity_rebalance",
            "opportunity_action": decision.get("action"),
            "score": decision.get("score"),
            "entry_score": decision.get("entry_score"),
            "candidate": decision.get("candidate") or "",
            "candidate_score": decision.get("candidate_score"),
        }
        if has_pending_sell_order(symbol, open_orders):
            orders.append({**base, "status": "skipped", "error": "pending sell already open"})
            continue
        if not crypto and not market_open:
            orders.append({**base, "status": "deferred", "error": "equity market closed"})
            continue
        try:
            asset = {"symbol": symbol, "asset_class": "crypto" if crypto else "us_equity"}
            qty_str = format_qty_for_asset(qty, asset)
            price = _position_price(pos)
            payload = {
                "symbol": symbol,
                "qty": qty_str,
                "side": "sell",
                "type": "market",
                "time_in_force": "gtc" if crypto else "day",
                "client_order_id": new_client_order_id(),
            }
            broker = alpaca_post("/v2/orders", payload)
            proceeds = round(float(qty_str) * price, 2) if price else 0.0
            orders.append({
                **base,
                "status": "placed",
                "order_id": broker.get("id"),
                "client_order_id": payload["client_order_id"],
                "time_in_force": payload["time_in_force"],
                "estimated_proceeds": proceeds,
            })
            log.info(
                "[Opportunity] SELL %s ×%s tif=%s | %s",
                symbol, qty_str, payload["time_in_force"], (decision.get("reason") or "")[:120],
            )
        except Exception as exc:
            log.error("[Opportunity] SELL %s failed: %s", symbol, exc)
            orders.append({**base, "status": "failed", "error": str(exc), "retry_next_cycle": True})
    return orders


def load_ownership_context(cycle_started_at):
    """Ledger buys, oldest owned entry time, and symbols bought this cycle."""
    from agenttrade.db import get_connection

    rows = []
    entry_times = {}
    bought = set()
    started = _as_dt(cycle_started_at) if cycle_started_at else None
    with get_connection() as conn:
        order_rows = conn.execute(
            """
            SELECT symbol, side, client_order_id, submitted_at
            FROM orders
            WHERE LOWER(side) IN ('buy', 'long')
            """
        ).fetchall()
        fill_rows = conn.execute(
            """
            SELECT f.symbol AS symbol, f.filled_at AS filled_at, o.client_order_id AS client_order_id
            FROM fills f
            LEFT JOIN orders o ON o.id = f.order_id
            WHERE LOWER(f.side) IN ('buy', 'long')
            """
        ).fetchall()
    for row in order_rows:
        item = {
            "symbol": row["symbol"],
            "side": row["side"],
            "client_order_id": row["client_order_id"] or "",
            "submitted_at": row["submitted_at"],
        }
        rows.append(item)
        _note_owned_time(entry_times, bought, item["symbol"], item["client_order_id"], item["submitted_at"], started)
    for row in fill_rows:
        cid = row["client_order_id"] or ""
        rows.append({"symbol": row["symbol"], "side": "buy", "client_order_id": cid})
        _note_owned_time(entry_times, bought, row["symbol"], cid, row["filled_at"], started)
    return rows, entry_times, bought


def load_entry_scores() -> list:
    from agenttrade.db import get_connection

    with get_connection() as conn:
        found = conn.execute(
            """
            SELECT symbol, entry_score, entry_at, last_score, last_sentiment
            FROM position_entry_scores
            """
        ).fetchall()
    return [dict(row) for row in found]


def swaps_placed_today(now: Optional[datetime] = None) -> int:
    from agenttrade.db import get_json_flag
    from trading_day import chicago_trading_date

    data = get_json_flag(_SWAPS_FLAG) or {}
    if not isinstance(data, dict):
        return 0
    if str(data.get("date") or "") != str(chicago_trading_date(now)):
        return 0
    try:
        return int(data.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def record_swaps_placed(count: int, now: Optional[datetime] = None) -> None:
    from agenttrade.db import set_system_flag
    from trading_day import chicago_trading_date

    if count <= 0:
        return
    payload = {
        "date": str(chicago_trading_date(now)),
        "count": swaps_placed_today(now) + int(count),
    }
    set_system_flag(_SWAPS_FLAG, json.dumps(payload))


_SWAPS_FLAG = "OPPORTUNITY_SWAPS_TODAY"


def _persist_after_plan(planned, orders, holdings, entry_rows, entry_times, now: datetime) -> None:
    from agenttrade.db import get_connection, utc_now

    placed_full = set()
    for order in orders or []:
        if order.get("status") != "placed":
            continue
        if order.get("opportunity_action") in _FULL_EXIT_ACTIONS:
            placed_full |= symbol_forms(order.get("symbol") or order.get("ticker"))

    chosen = {}
    for decision in planned or []:
        sym = str(decision.get("symbol") or "")
        if not sym or decision.get("action") in ("disabled", "swap_buy", "skip"):
            continue
        prev = chosen.get(sym)
        if prev and prev.get("action") in _SELL_ACTIONS and decision.get("action") not in _SELL_ACTIONS:
            continue
        chosen[sym] = decision

    by_symbol = {str(h.get("symbol") or ""): h for h in holdings or []}
    stamp = utc_now()
    with get_connection() as conn:
        for sym, decision in chosen.items():
            forms = symbol_forms(sym)
            if forms & placed_full or (
                decision.get("action") in _FULL_EXIT_ACTIONS
                and _order_status_for(orders, sym) == "placed"
            ):
                _delete_entry_forms(conn, forms)
                continue
            holding = by_symbol.get(sym) or {}
            score = decision.get("score")
            if score is None:
                score = holding.get("score")
            if score is None:
                continue
            sentiment = holding.get("sentiment")
            existing = _match_row(entry_rows, sym)
            if existing:
                last_sent = sentiment if sentiment is not None else existing.get("last_sentiment")
                conn.execute(
                    """
                    UPDATE position_entry_scores
                    SET last_score=?, last_sentiment=?, updated_at=?
                    WHERE symbol=?
                    """,
                    (float(score), last_sent, stamp, existing["symbol"]),
                )
                continue
            entry_at = holding.get("entry_at") or _entry_time_for(entry_times, sym) or now
            if isinstance(entry_at, datetime):
                entry_at = entry_at.astimezone(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO position_entry_scores(
                    symbol, entry_score, entry_at, last_score, last_sentiment, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sym, float(score), str(entry_at), float(score), sentiment, stamp),
            )


def _swap_candidates(decisions, book) -> list:
    out = []
    for row in decisions or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("action") or "").upper() != "BUY":
            continue
        sym = str(row.get("ticker") or row.get("symbol") or "")
        info = _score_for(book, sym)
        score = row.get("signal_strength")
        if score is None:
            score = row.get("score")
        if score is None:
            score = info.get("score")
        if not sym or score is None:
            continue
        out.append({
            "symbol": sym,
            "score": float(score),
            "bucket": row.get("bucket") or info.get("bucket") or "",
        })
    return out


def _remember_score(book, symbol, score, sentiment=None, bucket=None) -> None:
    if score is None or not symbol:
        return
    for form in symbol_forms(symbol):
        prev = book.get(form) or {}
        book[form] = {
            "score": float(score),
            "sentiment": sentiment if sentiment is not None else prev.get("sentiment"),
            "bucket": bucket or prev.get("bucket") or "",
        }


def _score_for(book, symbol) -> dict:
    for form in symbol_forms(symbol):
        found = (book or {}).get(form)
        if found and found.get("score") is not None:
            return found
    return {}


def _match_row(rows, symbol):
    forms = symbol_forms(symbol)
    for row in rows or []:
        if symbol_forms(row.get("symbol")) & forms:
            return row
    return None


def _entry_time_for(entry_times, symbol):
    for form in symbol_forms(symbol):
        if form in (entry_times or {}):
            return entry_times[form]
    return None


def _note_owned_time(entry_times, bought, symbol, client_order_id, when, started) -> None:
    if not client_id_owns_position(client_order_id):
        return
    moment = _as_dt(when)
    if moment is None:
        return
    for form in symbol_forms(symbol):
        prev = entry_times.get(form)
        if prev is None or moment < prev:
            entry_times[form] = moment
    if started and moment >= started:
        bought.add(symbol)


def _bucket_for_symbol(symbol, tags) -> str:
    forms = symbol_forms(symbol)
    for key, name in (tags or {}).items():
        if symbol_forms(key) & forms:
            return str(name or "")
    return ""


def _position_for(positions, symbol):
    forms = symbol_forms(symbol)
    for pos in positions or []:
        if symbol_forms(pos.get("symbol")) & forms:
            return pos
    return None


def _position_qty(pos: dict) -> float:
    raw = pos.get("qty")
    if raw is None:
        raw = pos.get("shares")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _position_price(pos: dict) -> float:
    for key in ("current_price", "market_price", "price", "lastday_price"):
        value = _float_or_none(pos.get(key))
        if value:
            return value
    qty = _position_qty(pos)
    value = _float_or_none(pos.get("market_value"))
    if qty and value:
        return value / qty
    return 0.0


def _position_is_crypto(pos: dict) -> bool:
    from order_utils import is_crypto_symbol

    if str(pos.get("asset_class") or "").lower() == "crypto":
        return True
    return is_crypto_symbol(str(pos.get("symbol") or ""))


def _order_status_for(orders, symbol) -> str:
    forms = symbol_forms(symbol)
    for order in orders or []:
        if symbol_forms(order.get("symbol") or order.get("ticker")) & forms:
            return str(order.get("status") or "")
    return ""


def _delete_entry_forms(conn, forms: set) -> None:
    found = conn.execute("SELECT symbol FROM position_entry_scores").fetchall()
    for row in found:
        if symbol_forms(row["symbol"]) & forms:
            conn.execute("DELETE FROM position_entry_scores WHERE symbol=?", (row["symbol"],))


def _float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def log_decisions(decisions: list) -> None:
    for row in decisions or []:
        log.info(
            "[Opportunity] %s %s score=%s entry=%s candidate=%s candidate_score=%s — %s",
            row.get("action"),
            row.get("symbol") or "-",
            row.get("score"),
            row.get("entry_score"),
            row.get("candidate") or "-",
            row.get("candidate_score"),
            row.get("reason"),
        )
