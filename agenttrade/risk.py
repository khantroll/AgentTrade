"""Deterministic risk manager — no strategy or LLM may override executable sizing (Tier 2)."""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

import agent_config as cfg
from agenttrade import db as ledger
from agenttrade.indicators import (
    calculate_atr,
    calculate_atr_stop,
    calculate_atr_take_profit_recommendation,
)

log = logging.getLogger(__name__)

MAX_RISK_PER_TRADE_PCT = 0.005
MAX_ACCOUNT_DRAWDOWN_PCT = 0.10
MAX_CONSECUTIVE_LOSSES = 5

LLM_SIZING_KEYS = ("qty", "shares", "notional", "notional_usd", "position_size")


def _reserved_open_buy(snapshot: Optional[dict]) -> float:
    from account_sync import reserved_open_buy_notional

    return reserved_open_buy_notional(snapshot)


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def refresh_risk_constants() -> None:
    global MAX_RISK_PER_TRADE_PCT, MAX_ACCOUNT_DRAWDOWN_PCT, MAX_CONSECUTIVE_LOSSES
    import os
    try:
        MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.005"))
    except ValueError:
        MAX_RISK_PER_TRADE_PCT = 0.005
    try:
        MAX_ACCOUNT_DRAWDOWN_PCT = float(os.getenv("MAX_ACCOUNT_DRAWDOWN_PCT", "0.10"))
    except ValueError:
        MAX_ACCOUNT_DRAWDOWN_PCT = 0.10
    try:
        MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
    except ValueError:
        MAX_CONSECUTIVE_LOSSES = 5


def strip_llm_sizing(decision: dict, cycle_run_id: Optional[int] = None) -> dict:
    """Remove LLM-provided executable sizing; log warning if present."""
    out = dict(decision)
    ignored = {}
    for key in LLM_SIZING_KEYS:
        if key in out and out[key] not in (None, "", 0, 0.0):
            ignored[key] = out.pop(key)
    if ignored:
        log.warning(
            "Ignored LLM-provided sizing; deterministic risk module used instead: %s",
            ignored,
        )
        ledger.increment_ignored_llm_sizing()
        if cycle_run_id:
            try:
                ledger.insert_risk_event(
                    cycle_run_id,
                    "warning",
                    "LLM_SIZING_IGNORED",
                    f"Ignored LLM sizing fields: {list(ignored.keys())}",
                    symbol=str(out.get("ticker") or ""),
                    raw=ignored,
                )
            except Exception as e:
                log.debug("[RiskManager] Could not record LLM sizing ignore event: %s", e)
    return out


def calculate_position_size(
    *,
    symbol: str,
    account_equity: float,
    available_cash: float,
    entry_price: float,
    stop_price: float,
    max_risk_pct: float = 0.005,
    max_position_pct: float | None = None,
    allow_fractional: bool = True,
) -> dict:
    """Only approved source of executable qty/notional."""
    refresh_risk_constants()
    if max_risk_pct <= 0:
        max_risk_pct = MAX_RISK_PER_TRADE_PCT

    if account_equity <= 0 or entry_price <= 0:
        return {
            "approved": False,
            "symbol": symbol,
            "qty": 0,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_dollars": 0.0,
            "estimated_notional": 0.0,
            "reason": "invalid equity or entry price",
        }

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return {
            "approved": False,
            "symbol": symbol,
            "qty": 0,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_dollars": 0.0,
            "estimated_notional": 0.0,
            "reason": "zero stop distance",
        }

    risk_dollars = account_equity * max_risk_pct
    raw_qty = risk_dollars / stop_distance
    if allow_fractional:
        qty = round(raw_qty, 6)
    else:
        qty = float(int(raw_qty))
        if qty <= 0 and raw_qty > 0:
            qty = 1.0

    estimated_notional = qty * entry_price

    if max_position_pct is not None and max_position_pct > 0:
        cap = account_equity * max_position_pct
        if estimated_notional > cap + 0.01:
            qty = cap / entry_price
            if not allow_fractional:
                qty = float(max(1, int(qty)))
            else:
                qty = round(qty, 6)
            estimated_notional = qty * entry_price

    allow_margin = cfg.ALLOW_MARGIN
    if not allow_margin and estimated_notional > available_cash + 0.01:
        if available_cash <= 0:
            return {
                "approved": False,
                "symbol": symbol,
                "qty": 0,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "risk_dollars": risk_dollars,
                "estimated_notional": estimated_notional,
                "reason": "insufficient cash (margin disabled)",
            }
        qty = available_cash / entry_price
        if not allow_fractional:
            qty = float(max(0, int(qty)))
        else:
            qty = round(qty, 2)
        estimated_notional = qty * entry_price
        if qty <= 0 or estimated_notional <= 0:
            return {
                "approved": False,
                "symbol": symbol,
                "qty": 0,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "risk_dollars": risk_dollars,
                "estimated_notional": 0.0,
                "reason": "cash too low for minimum position",
            }

    if qty <= 0:
        return {
            "approved": False,
            "symbol": symbol,
            "qty": 0,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_dollars": risk_dollars,
            "estimated_notional": 0.0,
            "reason": "computed quantity is zero",
        }

    return {
        "approved": True,
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "risk_dollars": round(risk_dollars, 2),
        "estimated_notional": round(estimated_notional, 2),
        "reason": f"risk-based size ({max_risk_pct * 100:.2f}% equity)",
    }


def size_by_risk(equity: float, entry_price: float, stop_price: float) -> Optional[int]:
    """Backward-compatible whole-share sizing helper."""
    result = calculate_position_size(
        symbol="",
        account_equity=equity,
        available_cash=equity,
        entry_price=entry_price,
        stop_price=stop_price,
        allow_fractional=False,
    )
    if not result["approved"]:
        return None
    return int(result["qty"]) or None


def _resolve_atr(decision: dict) -> float:
    atr = _float(decision.get("atr"))
    if atr > 0:
        return atr
    ohlcv = decision.get("ohlcv")
    if ohlcv:
        atr = calculate_atr(ohlcv)
        if atr > 0:
            return atr
    price = _float(decision.get("current_price") or decision.get("price"))
    atr_pct = _float(decision.get("atr_pct"))
    if price > 0 and atr_pct > 0:
        return round(price * atr_pct / 100.0, 6)
    return 0.0


def _max_position_pct(bucket) -> Optional[float]:
    if not bucket:
        return None
    alloc = _float(getattr(bucket, "allocation_pct", 0))
    pos_pct = _float(getattr(bucket, "max_position_pct", 0))
    if alloc > 0 and pos_pct > 0:
        return alloc * pos_pct
    return pos_pct or None


def compute_stop_price(decision: dict, bucket=None, cycle_run_id: Optional[int] = None) -> Optional[float]:
    """ATR stop with bucket flat-% fallback; logs fallback to risk_events."""
    price = _float(decision.get("current_price") or decision.get("price"))
    if price <= 0:
        return None

    atr = _resolve_atr(decision)
    fb_pct = _float(getattr(bucket, "stop_loss_pct", None), 0.07) if bucket else 0.07
    stop_result = calculate_atr_stop(
        side="buy",
        entry_price=price,
        atr=atr,
        fallback_stop_pct=fb_pct,
        bucket=bucket,
    )
    decision["stop_source"] = "fallback_flat_pct" if stop_result.get("used_fallback") else "atr"
    decision["atr"] = atr
    decision["atr_multiplier"] = stop_result.get("atr_multiplier")

    if stop_result.get("used_fallback") and cycle_run_id:
        ledger.insert_risk_event(
            cycle_run_id,
            "warning",
            "ATR_STOP_FALLBACK",
            stop_result.get("reason", "ATR unavailable; used flat bucket stop"),
            symbol=str(decision.get("ticker") or ""),
            raw={"atr": atr, "stop_price": stop_result.get("stop_price")},
        )
        log.warning(
            "[RiskManager] ATR fallback for %s: %s",
            decision.get("ticker"),
            stop_result.get("reason"),
        )

    if not stop_result.get("approved"):
        return None
    return stop_result.get("stop_price")


def _apply_take_profit(decision: dict, bucket=None, entry_price: float = 0, atr: float = 0) -> None:
    """Preserve bucket take-profit; log ATR recommendation if different."""
    if bucket and entry_price > 0:
        if not decision.get("take_profit_price"):
            tp_pct = _float(getattr(bucket, "take_profit_pct", 0))
            if tp_pct > 0:
                decision["take_profit_price"] = round(entry_price * (1 + tp_pct), 4)
                decision["take_profit_source"] = "bucket_pct"

    rec = calculate_atr_take_profit_recommendation(
        entry_price=entry_price,
        atr=atr,
        atr_multiplier=_float(decision.get("atr_multiplier"), 2.0),
    )
    if rec.get("recommended") and rec.get("take_profit_price"):
        decision["atr_take_profit_recommendation"] = rec["take_profit_price"]
        existing = _float(decision.get("take_profit_price"))
        if existing and abs(existing - rec["take_profit_price"]) > 0.01:
            log.info(
                "[RiskManager] %s ATR TP recommendation $%s (keeping bucket TP $%s)",
                decision.get("ticker"),
                rec["take_profit_price"],
                existing,
            )


def prepare_buy_order(
    decision: dict,
    account_snapshot: dict,
    bucket=None,
    cycle_run_id: Optional[int] = None,
    *,
    is_crypto: bool = False,
) -> tuple[bool, str, dict]:
    """
    Tier 2 pipeline: strip LLM sizing → ATR stop → deterministic size.
    Returns (approved, reason, adjusted_decision).
    """
    refresh_risk_constants()
    decision = strip_llm_sizing(decision, cycle_run_id)

    action = str(decision.get("action") or decision.get("decision", "SKIP")).upper()
    if action != "BUY":
        return True, "", decision

    if ledger.trading_paused():
        reason = ledger.get_pause_reason() or "TRADING_PAUSED=true"
        return False, "trading_paused", {"blocked_reason": reason}

    acct = account_snapshot.get("account") or account_snapshot
    equity = _float(acct.get("equity") or acct.get("portfolio_value"))
    cash = _float(acct.get("cash"))
    open_buy = _reserved_open_buy(account_snapshot)
    available_cash = max(cash - open_buy, 0) if not cfg.ALLOW_MARGIN else cash

    price = _float(decision.get("current_price") or decision.get("price"))
    if price <= 0:
        return False, "invalid_price", decision

    stop = compute_stop_price(decision, bucket, cycle_run_id)
    if not stop:
        return False, "invalid_stop", decision

    atr = _resolve_atr(decision)
    _apply_take_profit(decision, bucket, price, atr)

    size_result = calculate_position_size(
        symbol=decision.get("ticker", ""),
        account_equity=equity,
        available_cash=available_cash,
        entry_price=price,
        stop_price=stop,
        max_risk_pct=MAX_RISK_PER_TRADE_PCT,
        max_position_pct=_max_position_pct(bucket),
        allow_fractional=is_crypto,
    )
    decision["position_sizing_source"] = "deterministic"
    decision["risk_per_trade_pct"] = MAX_RISK_PER_TRADE_PCT
    decision["stop_loss_price"] = stop

    if not size_result["approved"]:
        if cycle_run_id:
            ledger.insert_risk_event(
                cycle_run_id,
                "warning",
                "POSITION_SIZE_REJECTED",
                size_result.get("reason", "sizing rejected"),
                symbol=str(decision.get("ticker") or ""),
            )
        return False, size_result.get("reason", "sizing_rejected"), decision

    if is_crypto:
        decision["notional_usd"] = size_result["estimated_notional"]
        decision.pop("shares", None)
    else:
        decision["shares"] = int(size_result["qty"])
        decision.pop("notional_usd", None)

    decision["estimated_notional"] = size_result["estimated_notional"]
    decision["risk_dollars"] = size_result["risk_dollars"]
    return True, "", decision


def _is_crypto_bucket(bucket) -> bool:
    if not bucket:
        return False
    return (
        getattr(bucket, "asset_class", "us_equity") == "crypto"
        or getattr(bucket, "mode", "") == "crypto"
    )


def evaluate_proposed_order(
    decision: dict,
    account_snapshot: dict,
    bucket=None,
    cycle_run_id: Optional[int] = None,
) -> tuple[bool, str, dict]:
    """Final deterministic check before order submission."""
    is_crypto = _is_crypto_bucket(bucket) if bucket else bool(decision.get("asset_class") == "crypto")
    ok, reason, adj = prepare_buy_order(
        decision, account_snapshot, bucket, cycle_run_id, is_crypto=is_crypto,
    )
    if not ok:
        return False, reason, adj

    action = str(adj.get("action", "SKIP")).upper()
    if action != "BUY":
        return True, "", adj

    acct = account_snapshot.get("account") or account_snapshot
    equity = _float(acct.get("equity") or acct.get("portfolio_value"))
    cash = _float(acct.get("cash"))
    open_buy = _reserved_open_buy(account_snapshot)
    allow_margin = cfg.ALLOW_MARGIN

    price = _float(adj.get("current_price") or adj.get("price"))
    stop = _float(adj.get("stop_loss_price"))
    shares = adj.get("shares")
    notional = _float(adj.get("notional_usd"))
    order_notional = notional if notional else int(shares or 0) * price

    projected = cash - open_buy - order_notional
    if not allow_margin and not cfg.ALLOW_NEGATIVE_CASH:
        if cash <= 0:
            return False, "insufficient_live_cash", {"cash": cash, "order_notional": order_notional}
        if projected < cfg.MIN_CASH_RESERVE:
            return False, "below_min_cash_reserve", {"projected_cash": projected}
        if projected < 0:
            return False, "insufficient_live_cash", {"projected_cash": projected}

    max_risk = equity * MAX_RISK_PER_TRADE_PCT
    if shares and price and stop:
        risk_at_stop = abs(price - stop) * int(shares)
        if risk_at_stop > max_risk * 1.05:
            return False, "exceeds_max_risk_per_trade", {"risk": risk_at_stop, "max": max_risk}

    return True, "", adj


def _is_cash_block(reason: str) -> bool:
    text = (reason or "").lower()
    return "cash" in text or "buying power" in text or "buying_power" in text


def _positive(value: Optional[float]) -> Optional[float]:
    if value is None or value <= 0:
        return None
    return value


class CycleRiskState:
    """Remaining cash and held symbols shared across one trading cycle.

    Approvals reserve deterministic notional against ``remaining_cash``. Later
    approvals in the same cycle, including other buckets, see that reduced
    balance and are refused when it cannot fund the order.

    ``ALLOW_MARGIN`` still permits orders beyond cash; the ledger records them
    but does not refuse for cash. Drawdown and max-invested checks read config
    at decision time (``MAX_ACCOUNT_DRAWDOWN_PCT``, ``MAX_INVESTED_PCT``).
    A setting <= 0 turns that guard off.
    """

    def __init__(
        self,
        spendable: float,
        *,
        equity: float = 0.0,
        long_market_value: float = 0.0,
        high_water: Optional[float] = None,
        position_symbols: Optional[set] = None,
        position_buckets: Optional[dict] = None,
        positions_by_symbol: Optional[dict] = None,
    ):
        self.starting_cash = round(float(spendable or 0), 2)
        self.remaining_cash = self.starting_cash
        self.equity = float(equity or 0)
        self.long_market_value = float(long_market_value or 0)
        self.high_water = high_water
        self.position_symbols = set(position_symbols or ())
        self.position_buckets = dict(position_buckets or {})
        self.positions_by_symbol = dict(positions_by_symbol or {})
        self.reservations: dict[str, float] = {}
        self.pending: dict[str, str] = {}
        self.confirmed: dict[str, str] = {}

    @classmethod
    def from_snapshot(cls, account_snapshot: Optional[dict], positions: Optional[list] = None) -> "CycleRiskState":
        snap = account_snapshot if isinstance(account_snapshot, dict) else {}
        acct = snap.get("account") if isinstance(snap.get("account"), dict) else snap
        if positions is None:
            positions = list(snap.get("positions") or [])
        else:
            positions = list(positions)

        raw_cash = _float(acct.get("cash"))
        open_buy = _reserved_open_buy(snap)
        if cfg.ALLOW_MARGIN:
            buying_power = acct.get("buying_power")
            spendable = _float(buying_power, raw_cash) if buying_power not in (None, "") else raw_cash
        else:
            spendable = raw_cash - open_buy

        equity = _float(acct.get("equity") or acct.get("portfolio_value"))
        if acct.get("long_market_value") not in (None, ""):
            long_mv = _float(acct.get("long_market_value"))
        else:
            long_mv = 0.0

        try:
            raw_tags = cfg.bucket_manager._load_tags() or {}
        except Exception:
            raw_tags = {}
        tags = {str(k).upper(): v for k, v in raw_tags.items() if k}

        symbols = set()
        buckets = {}
        by_symbol = {}
        summed_mv = 0.0
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            sym = str(pos.get("symbol") or pos.get("ticker") or "").upper()
            if not sym:
                continue
            symbols.add(sym)
            by_symbol[sym] = pos
            summed_mv += _float(pos.get("market_value"))
            if sym in tags:
                buckets[sym] = str(tags[sym])
        if acct.get("long_market_value") in (None, ""):
            long_mv = summed_mv

        high_water = None
        if "high_water_equity" in snap:
            high_water = _positive(_float(snap.get("high_water_equity"), None))
        elif isinstance(acct, dict) and "high_water_equity" in acct:
            high_water = _positive(_float(acct.get("high_water_equity"), None))
        else:
            try:
                raw_hw = ledger.get_system_flag("HIGH_WATER_EQUITY")
            except Exception:
                raw_hw = None
            if raw_hw not in (None, ""):
                high_water = _positive(_float(raw_hw, None))

        return cls(
            spendable,
            equity=equity,
            long_market_value=long_mv,
            high_water=high_water,
            position_symbols=symbols,
            position_buckets=buckets,
            positions_by_symbol=by_symbol,
        )

    def reserved_total(self) -> float:
        return round(sum(self.reservations.values()), 2)

    def reserved_excluding(self, ticker: str) -> float:
        key = str(ticker or "").upper()
        return round(sum(v for sym, v in self.reservations.items() if sym != key), 2)

    def confirmed_reserved(self) -> float:
        return round(sum(v for sym, v in self.reservations.items() if sym in self.confirmed), 2)

    def snapshot_for(self, ticker: str, base: Optional[dict]) -> dict:
        """Copy of the account snapshot with other reservations removed from cash."""
        base = base if isinstance(base, dict) else {}
        snap = dict(base)
        acct_src = base.get("account") if isinstance(base.get("account"), dict) else base
        acct = dict(acct_src)
        acct["cash"] = round(_float(acct.get("cash")) - self.reserved_excluding(ticker), 2)
        snap["account"] = acct
        snap["cash"] = acct["cash"]
        return snap

    def commit(self, ticker: str, notional: float, *, replace: bool = True) -> bool:
        """Reserve ``notional`` for ticker. ``replace`` updates a prior quote."""
        key = str(ticker or "").upper()
        notional = round(float(notional or 0), 2)
        if not key or notional <= 0:
            return False
        prev = round(self.reservations.get(key, 0.0), 2)
        if replace:
            new_total = notional
            delta = round(new_total - prev, 2)
        else:
            delta = notional
            new_total = round(prev + notional, 2)
        if not cfg.ALLOW_MARGIN and delta > self.remaining_cash + 0.01:
            return False
        self.reservations[key] = new_total
        if not cfg.ALLOW_MARGIN:
            self.remaining_cash = round(self.remaining_cash - delta, 2)
        return True

    def release(self, ticker: str) -> None:
        """Drop a reservation that was not confirmed as an order."""
        key = str(ticker or "").upper()
        if not key or key in self.confirmed:
            return
        prev = round(self.reservations.pop(key, 0.0), 2)
        if not cfg.ALLOW_MARGIN:
            self.remaining_cash = round(self.remaining_cash + prev, 2)
        self.pending.pop(key, None)

    def note_pending(self, ticker: str, bucket_name: str) -> None:
        key = str(ticker or "").upper()
        if not key or key in self.confirmed:
            return
        self.pending[key] = bucket_name or ""

    def confirm(self, ticker: str, bucket_name: str, notional: float) -> bool:
        key = str(ticker or "").upper()
        if not self.commit(key, notional, replace=key not in self.confirmed):
            return False
        self.pending.pop(key, None)
        self.confirmed[key] = bucket_name or self.confirmed.get(key, "")
        return True

    def holding_bucket(self, ticker: str) -> Optional[str]:
        """Bucket that owns the symbol, ``""`` if held but untagged, or None."""
        key = str(ticker or "").upper()
        if key in self.position_symbols:
            return self.position_buckets.get(key, "")
        if key in self.confirmed:
            return self.confirmed[key]
        if key in self.pending:
            return self.pending[key]
        return None

    def position_for(self, ticker: str) -> Optional[dict]:
        return self.positions_by_symbol.get(str(ticker or "").upper())

    def drawdown_reason(self) -> Optional[str]:
        try:
            threshold = float(cfg.MAX_ACCOUNT_DRAWDOWN_PCT)
        except (TypeError, ValueError):
            threshold = 0.10
        if threshold <= 0 or not self.high_water or self.high_water <= 0 or self.equity <= 0:
            return None
        drawdown = (self.high_water - self.equity) / self.high_water
        if drawdown >= threshold - 1e-9:
            return "drawdown_pause"
        return None

    def invested_reason(self, projected: float) -> Optional[str]:
        try:
            cap = float(cfg.MAX_INVESTED_PCT)
        except (TypeError, ValueError):
            cap = 0.90
        if cap <= 0:
            return None
        projected = float(projected or 0)
        if self.equity <= 0:
            return "max_invested" if projected > 0.01 else None
        if projected / self.equity >= cap - 1e-9:
            return "max_invested"
        return None


def quote_buy_notional(decision: dict, account_snapshot: dict, bucket=None) -> tuple[bool, str, float]:
    """Estimate Tier 2 notional without writing shares onto the caller's decision."""
    scratch = dict(decision or {})
    for key in LLM_SIZING_KEYS:
        scratch.pop(key, None)
    ok, reason, adj = prepare_buy_order(
        scratch,
        account_snapshot,
        bucket,
        cycle_run_id=None,
        is_crypto=_is_crypto_bucket(bucket),
    )
    if not ok:
        return False, reason or "sizing_rejected", 0.0
    return True, "", _float(adj.get("estimated_notional"))


def evaluate_batch(
    decisions: list,
    account_snapshot: dict,
    bucket=None,
    cycle_run_id: Optional[int] = None,
    cycle_state: Optional[CycleRiskState] = None,
) -> list:
    """Filter approved BUY decisions through Tier 2 deterministic risk.

    Cash reserved by an approval is removed from the balance seen by the next
    approval. Pass the cycle's ``CycleRiskState`` so later buckets share it.
    """
    state = cycle_state or CycleRiskState.from_snapshot(account_snapshot)
    approved = []
    gate = state.drawdown_reason() or state.invested_reason(
        state.long_market_value + state.confirmed_reserved()
    )
    if gate:
        for d in decisions or []:
            if str(d.get("action", "SKIP")).upper() != "BUY":
                continue
            d["blocked_reason"] = gate
            state.release(str(d.get("ticker") or ""))
        return []

    bucket_name = getattr(bucket, "name", "") if bucket else ""
    for d in decisions:
        if str(d.get("action", "SKIP")).upper() != "BUY":
            continue
        ticker = str(d.get("ticker") or "")
        snap = state.snapshot_for(ticker, account_snapshot)
        ok, reason, adj = evaluate_proposed_order(d, snap, bucket, cycle_run_id)
        if ok:
            notional = _float(adj.get("estimated_notional"))
            projected = state.long_market_value + state.reserved_excluding(ticker) + notional
            block = state.drawdown_reason() or state.invested_reason(projected)
            if block:
                ok = False
                reason = block
            elif not state.confirm(ticker, bucket_name, notional):
                ok = False
                reason = "insufficient_cash"
        if ok:
            approved.append(adj)
        else:
            if _is_cash_block(reason) and (approved or state.reserved_total() > 0.01):
                reason = "insufficient_cash"
            log.warning("[RiskManager] Rejected %s: %s", d.get("ticker"), reason)
            d["blocked_reason"] = reason
            if (
                isinstance(adj, dict)
                and adj.get("blocked_reason")
                and reason not in ("insufficient_cash", "drawdown_pause", "max_invested")
            ):
                d["blocked_reason"] = adj["blocked_reason"] or reason
            state.release(ticker)
    return approved


def _is_consecutive_loss_pause_reason(reason: Optional[str]) -> bool:
    text = (reason or "").lower()
    return "consecutive loss" in text or text == ""


def get_consecutive_loss_status(limit: int = 20) -> dict:
    """Single source of truth — completed_trades only."""
    return ledger.get_consecutive_loss_status(limit=limit)


def count_consecutive_losses_from_fills(limit: int = 100) -> int:
    """Deprecated alias — returns deterministic completed_trades count only."""
    return get_consecutive_loss_status(limit=limit).get("count", 0)


def check_consecutive_loss_pause(db=None, max_losses: int = None) -> tuple[bool, str]:
    """
    If consecutive losses >= threshold, set TRADING_PAUSED and block new buys.
    Returns (paused, reason). Protective sells are not blocked here.

    Pause enforcement uses completed_trades only. When the ledger is empty,
    no pause is set and any stale consecutive-loss pause is cleared.
    """
    refresh_risk_constants()
    threshold = max_losses if max_losses is not None else MAX_CONSECUTIVE_LOSSES
    status = get_consecutive_loss_status(limit=max(threshold * 4, 20))

    if not status.get("ledger_complete"):
        msg = status.get("message", "ledger incomplete")
        if ledger.trading_paused() and _is_consecutive_loss_pause_reason(ledger.get_pause_reason()):
            reset_trading_pause("Cleared stale pause — ledger incomplete (no completed_trades)")
        log.warning("[RiskManager] %s — consecutive-loss pause not enforced", msg)
        return False, msg

    consecutive = int(status.get("count", 0))

    if ledger.trading_paused():
        reason = ledger.get_pause_reason() or "Consecutive loss limit reached"
        if _is_consecutive_loss_pause_reason(reason) and consecutive < threshold:
            reset_trading_pause(
                f"Auto-cleared: deterministic count={consecutive} < threshold={threshold}"
            )
            return False, ""
        return True, reason

    if consecutive >= threshold:
        reason = "Consecutive loss limit reached"
        ledger.set_system_flag("TRADING_PAUSED", "true")
        ledger.set_system_flag("PAUSE_REASON", reason)
        ledger.insert_risk_event(
            None,
            "critical",
            "CONSECUTIVE_LOSS_PAUSE",
            f"{consecutive} consecutive losses (limit {threshold})",
            raw={"consecutive_losses": consecutive, "max_losses": threshold},
        )
        log.warning("[RiskManager] TRADING_PAUSED: %s (%d losses)", reason, consecutive)
        return True, reason

    return False, ""


def reset_trading_pause(reason: str = "Manual pause reset") -> None:
    """Clear TRADING_PAUSED flags and log risk event."""
    ledger.set_system_flag("TRADING_PAUSED", "false")
    ledger.set_system_flag("PAUSE_REASON", "")
    ledger.insert_risk_event(
        None,
        "info",
        "TRADING_PAUSE_RESET",
        reason,
    )
    log.info("[RiskManager] Trading pause cleared: %s", reason)
