"""Compare live Alpaca account state against the SQLite ledger."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from account_sync import refresh_alpaca_snapshot
from agenttrade import db as ledger

log = logging.getLogger(__name__)

CASH_TOLERANCE = 1.0
EQUITY_TOLERANCE = 5.0
QTY_TOLERANCE = 0.0001


@dataclass
class FieldCompare:
    field: str
    alpaca: Optional[float]
    sqlite: Optional[float]
    delta: Optional[float]
    match: bool


@dataclass
class VerifyReport:
    ok: bool
    verified_at: str
    db_path: str
    alpaca_sync_at: Optional[str]
    sqlite_snapshot_at: Optional[str]
    sqlite_cycle_run_id: Optional[int]
    account: list[FieldCompare] = field(default_factory=list)
    positions: dict[str, Any] = field(default_factory=dict)
    open_orders: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    summary: str = ""
    margin_detected: bool = False
    trading_halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verified_at": self.verified_at,
            "db_path": self.db_path,
            "alpaca_sync_at": self.alpaca_sync_at,
            "sqlite_snapshot_at": self.sqlite_snapshot_at,
            "sqlite_cycle_run_id": self.sqlite_cycle_run_id,
            "account": [
                {
                    "field": c.field,
                    "alpaca": c.alpaca,
                    "sqlite": c.sqlite,
                    "delta": c.delta,
                    "match": c.match,
                }
                for c in self.account
            ],
            "positions": self.positions,
            "open_orders": self.open_orders,
            "issues": self.issues,
            "summary": self.summary,
            "margin_detected": self.margin_detected,
            "trading_halted": self.trading_halted,
            "halt_reason": self.halt_reason,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _f(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compare_scalar(
    name: str,
    alpaca_val: Optional[float],
    sqlite_val: Optional[float],
    tolerance: float,
) -> FieldCompare:
    if alpaca_val is None and sqlite_val is None:
        return FieldCompare(name, None, None, None, True)
    a = _f(alpaca_val)
    s = _f(sqlite_val)
    delta = round(a - s, 4)
    match = abs(delta) <= tolerance
    return FieldCompare(name, round(a, 2), round(s, 2), delta, match)


def _position_map_from_alpaca(positions: list) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in positions or []:
        sym = p.get("symbol")
        if sym:
            out[sym] = _f(p.get("qty"))
    return out


def _position_map_from_sqlite(positions: list) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in positions or []:
        sym = p.get("symbol")
        if sym:
            out[sym] = _f(p.get("qty"))
    return out


def _normalize_order(order: dict, *, from_sqlite: bool = False) -> dict:
    oid = order.get("alpaca_order_id") if from_sqlite else (order.get("id") or order.get("order_id"))
    return {
        "id": str(oid) if oid else "",
        "symbol": order.get("symbol") or order.get("ticker") or "",
        "side": str(order.get("side", "")).lower(),
        "qty": _f(order.get("qty") or order.get("shares")),
        "status": str(order.get("status", "")).lower(),
        "order_type": order.get("order_type") or order.get("type") or "",
    }


def _compare_positions(alpaca_positions: list, sqlite_positions: list) -> dict[str, Any]:
    alpaca_map = _position_map_from_alpaca(alpaca_positions)
    sqlite_map = _position_map_from_sqlite(sqlite_positions)
    all_syms = sorted(set(alpaca_map) | set(sqlite_map))

    matches: list[str] = []
    qty_mismatches: dict[str, dict] = {}
    only_alpaca: dict[str, float] = {}
    only_sqlite: dict[str, float] = {}

    for sym in all_syms:
        a_qty = alpaca_map.get(sym)
        s_qty = sqlite_map.get(sym)
        if sym not in sqlite_map:
            only_alpaca[sym] = a_qty
        elif sym not in alpaca_map:
            only_sqlite[sym] = s_qty
        elif abs(a_qty - s_qty) <= QTY_TOLERANCE:
            matches.append(sym)
        else:
            qty_mismatches[sym] = {"alpaca_qty": a_qty, "sqlite_qty": s_qty, "delta": round(a_qty - s_qty, 6)}

    return {
        "match_count": len(matches),
        "alpaca_count": len(alpaca_map),
        "sqlite_count": len(sqlite_map),
        "matches": matches,
        "qty_mismatches": qty_mismatches,
        "only_alpaca": only_alpaca,
        "only_sqlite": only_sqlite,
    }


def _compare_open_orders(alpaca_orders: list, sqlite_orders: list) -> dict[str, Any]:
    alpaca_by_id = {_normalize_order(o)["id"]: _normalize_order(o) for o in alpaca_orders or [] if _normalize_order(o)["id"]}
    sqlite_by_id = {
        _normalize_order(o, from_sqlite=True)["id"]: _normalize_order(o, from_sqlite=True)
        for o in sqlite_orders or []
        if _normalize_order(o, from_sqlite=True)["id"]
    }

    common = sorted(set(alpaca_by_id) & set(sqlite_by_id))
    only_alpaca = sorted(set(alpaca_by_id) - set(sqlite_by_id))
    only_sqlite = sorted(set(sqlite_by_id) - set(alpaca_by_id))

    field_mismatches: dict[str, dict] = {}
    for oid in common:
        a = alpaca_by_id[oid]
        s = sqlite_by_id[oid]
        diffs = {}
        for key in ("symbol", "side", "qty", "status"):
            if key == "qty":
                if abs(a[key] - s[key]) > QTY_TOLERANCE:
                    diffs[key] = {"alpaca": a[key], "sqlite": s[key]}
            elif a[key] != s[key]:
                diffs[key] = {"alpaca": a[key], "sqlite": s[key]}
        if diffs:
            field_mismatches[oid] = diffs

    return {
        "alpaca_count": len(alpaca_by_id),
        "sqlite_count": len(sqlite_by_id),
        "common_count": len(common),
        "only_alpaca": [{"id": oid, **alpaca_by_id[oid]} for oid in only_alpaca],
        "only_sqlite": [{"id": oid, **sqlite_by_id[oid]} for oid in only_sqlite],
        "field_mismatches": field_mismatches,
    }


def verify_ledger(*, fetch_alpaca: bool = True, snapshot: Optional[dict] = None) -> VerifyReport:
    """
    Pull current Alpaca account, read SQLite, compare cash/equity/positions/open orders.
    Returns a structured report. Does not mutate SQLite or Alpaca.
    """
    verified_at = _utc_now()
    issues: list[str] = []

    try:
        ledger.init_db()
    except Exception as e:
        return VerifyReport(
            ok=False,
            verified_at=verified_at,
            db_path=ledger.get_db_path(),
            alpaca_sync_at=None,
            sqlite_snapshot_at=None,
            sqlite_cycle_run_id=None,
            issues=[f"SQLite unavailable: {e}"],
            summary="FAILED — cannot read SQLite ledger",
        )

    db_path = ledger.get_db_path()
    acct_snap = ledger.get_latest_account_snapshot()
    sqlite_positions = ledger.get_latest_positions()
    sqlite_orders = ledger.get_latest_open_orders()

    if not acct_snap:
        issues.append("No account snapshot in SQLite — run a trading cycle or migrate_state first")

    if fetch_alpaca:
        try:
            alpaca = snapshot or refresh_alpaca_snapshot()
        except Exception as e:
            return VerifyReport(
                ok=False,
                verified_at=verified_at,
                db_path=db_path,
                alpaca_sync_at=None,
                sqlite_snapshot_at=acct_snap.get("captured_at") if acct_snap else None,
                sqlite_cycle_run_id=acct_snap.get("cycle_run_id") if acct_snap else None,
                issues=[f"Alpaca fetch failed: {e}"],
                summary="FAILED — cannot fetch Alpaca account",
            )
    else:
        alpaca = snapshot or {}

    alpaca_acct = alpaca.get("account") or {}
    alpaca_positions = alpaca.get("positions") or []
    alpaca_orders = alpaca.get("open_orders") or []

    # long_market_value: prefer the account field; if absent (some Alpaca responses
    # omit it when there are no short positions), derive it from the positions list
    # so the verifier doesn't report a spurious zero mismatch.
    def _long_mv_from_positions(positions: list) -> Optional[float]:
        if not positions:
            return None
        total = 0.0
        for p in positions:
            if str(p.get("side", "long")).lower() == "short":
                continue
            mv = _f(p.get("market_value"))
            if not mv:
                # Derive from price * qty when market_value absent
                price = _f(p.get("current_price") or p.get("market_price"))
                qty = _f(p.get("qty"))
                mv = price * qty
            total += mv
        return round(total, 2) if total > 0 else None

    _alpaca_long_mv = (
        alpaca_acct.get("long_market_value")
        if alpaca_acct.get("long_market_value") is not None
        else _long_mv_from_positions(alpaca_positions)
    )
    _sqlite_long_mv = (
        acct_snap.get("long_market_value")
        if acct_snap and acct_snap.get("long_market_value") is not None
        else _long_mv_from_positions(sqlite_positions)
    )

    account_fields = [
        _compare_scalar("cash", alpaca_acct.get("cash") or alpaca.get("cash"),
                        acct_snap.get("cash") if acct_snap else None, CASH_TOLERANCE),
        _compare_scalar("equity", alpaca_acct.get("equity") or alpaca.get("equity"),
                        acct_snap.get("equity") if acct_snap else None, EQUITY_TOLERANCE),
        _compare_scalar(
            "buying_power",
            alpaca_acct.get("buying_power") or alpaca.get("buying_power"),
            acct_snap.get("buying_power") if acct_snap else None,
            CASH_TOLERANCE,
        ),
        _compare_scalar(
            "long_market_value",
            _alpaca_long_mv,
            _sqlite_long_mv,
            EQUITY_TOLERANCE,
        ),
        _compare_scalar(
            "short_market_value",
            alpaca_acct.get("short_market_value"),
            acct_snap.get("short_market_value") if acct_snap else None,
            CASH_TOLERANCE,
        ),
    ]

    cash = _f(alpaca_acct.get("cash") or alpaca.get("cash"))
    equity = _f(alpaca_acct.get("equity") or alpaca.get("equity"))
    long_mv = _f(_alpaca_long_mv)
    short_mv = _f(alpaca_acct.get("short_market_value"))
    mult = _f(alpaca_acct.get("multiplier")) or 1.0
    margin_detected = cash < -0.01 or long_mv > equity + 0.01 or short_mv > 0.01 or mult > 1.01
    halted = ledger.trading_halted()
    halt_reason = ledger.get_halt_reason() or ""

    for cmp in account_fields:
        if not cmp.match:
            issues.append(
                f"{cmp.field} mismatch: Alpaca={cmp.alpaca} SQLite={cmp.sqlite} (delta={cmp.delta})"
            )

    pos_cmp = _compare_positions(alpaca_positions, sqlite_positions)
    if pos_cmp["only_alpaca"]:
        issues.append(f"Positions only on Alpaca: {', '.join(sorted(pos_cmp['only_alpaca']))}")
    if pos_cmp["only_sqlite"]:
        issues.append(f"Positions only in SQLite: {', '.join(sorted(pos_cmp['only_sqlite']))}")
    for sym, diff in pos_cmp["qty_mismatches"].items():
        issues.append(
            f"Position qty mismatch {sym}: Alpaca={diff['alpaca_qty']} SQLite={diff['sqlite_qty']}"
        )

    ord_cmp = _compare_open_orders(alpaca_orders, sqlite_orders)
    if ord_cmp["only_alpaca"]:
        ids = [o["id"] for o in ord_cmp["only_alpaca"]]
        issues.append(f"Open orders on Alpaca not in SQLite: {', '.join(ids)}")
    if ord_cmp["only_sqlite"]:
        ids = [o["id"] for o in ord_cmp["only_sqlite"]]
        issues.append(f"Open orders in SQLite not on Alpaca: {', '.join(ids)}")
    for oid, diffs in ord_cmp["field_mismatches"].items():
        issues.append(f"Open order {oid} field mismatch: {diffs}")

    ok = len(issues) == 0
    if ok:
        summary = (
            f"OK — Alpaca and SQLite match "
            f"({pos_cmp['match_count']} positions, {ord_cmp['common_count']} open orders)"
        )
    else:
        summary = f"MISMATCH — {len(issues)} issue(s) found"

    return VerifyReport(
        ok=ok,
        verified_at=verified_at,
        db_path=db_path,
        alpaca_sync_at=alpaca.get("alpaca_sync_at"),
        sqlite_snapshot_at=acct_snap.get("captured_at") if acct_snap else None,
        sqlite_cycle_run_id=acct_snap.get("cycle_run_id") if acct_snap else None,
        account=account_fields,
        positions=pos_cmp,
        open_orders=ord_cmp,
        issues=issues,
        summary=summary,
        margin_detected=margin_detected,
        trading_halted=halted,
        halt_reason=halt_reason or "",
    )


def format_report(report: VerifyReport) -> str:
    """Human-readable verification report."""
    lines = [
        "=" * 60,
        "AgentTrade Ledger Verification",
        "=" * 60,
        f"Verified at (UTC):  {report.verified_at}",
        f"SQLite path:        {report.db_path}",
        f"Alpaca sync:        {report.alpaca_sync_at or '—'}",
        f"SQLite snapshot:    {report.sqlite_snapshot_at or '—'}",
        f"SQLite cycle run:   {report.sqlite_cycle_run_id or '—'}",
        "",
        f"Result: {'PASS' if report.ok else 'FAIL'} — {report.summary}",
        "",
        "Account",
        "-" * 40,
    ]
    for cmp in report.account:
        status = "OK" if cmp.match else "MISMATCH"
        lines.append(
            f"  {cmp.field:16} Alpaca={cmp.alpaca!s:>12}  SQLite={cmp.sqlite!s:>12}  "
            f"delta={cmp.delta!s:>8}  [{status}]"
        )

    lines.extend([
        "",
        "Positions",
        "-" * 40,
        f"  Alpaca: {report.positions.get('alpaca_count', 0)}  "
        f"SQLite: {report.positions.get('sqlite_count', 0)}  "
        f"Matched: {report.positions.get('match_count', 0)}",
    ])
    if report.positions.get("qty_mismatches"):
        lines.append("  Qty mismatches:")
        for sym, d in report.positions["qty_mismatches"].items():
            lines.append(f"    {sym}: Alpaca={d['alpaca_qty']} SQLite={d['sqlite_qty']}")
    if report.positions.get("only_alpaca"):
        lines.append(f"  Only Alpaca: {report.positions['only_alpaca']}")
    if report.positions.get("only_sqlite"):
        lines.append(f"  Only SQLite: {report.positions['only_sqlite']}")

    lines.extend([
        "",
        "Open Orders",
        "-" * 40,
        f"  Alpaca: {report.open_orders.get('alpaca_count', 0)}  "
        f"SQLite: {report.open_orders.get('sqlite_count', 0)}  "
        f"Common: {report.open_orders.get('common_count', 0)}",
    ])
    for o in report.open_orders.get("only_alpaca") or []:
        lines.append(f"  + Alpaca only: {o['id']} {o['side']} {o['qty']} {o['symbol']}")
    for o in report.open_orders.get("only_sqlite") or []:
        lines.append(f"  + SQLite only: {o['id']} {o['side']} {o['qty']} {o['symbol']}")

    lines.extend([
        "",
        "Safety",
        "-" * 40,
        f"  Margin detected: {'YES' if report.margin_detected else 'NO'}",
        f"  Trading halted:  {'YES' if report.trading_halted else 'NO'}",
    ])
    if report.halt_reason:
        lines.append(f"  Halt reason:     {report.halt_reason}")

    if report.issues:
        lines.extend(["", "Issues", "-" * 40])
        for i, issue in enumerate(report.issues, 1):
            lines.append(f"  {i}. {issue}")

    lines.append("=" * 60)
    return "\n".join(lines)


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Compare live Alpaca account against SQLite ledger",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    args = parser.parse_args()

    report = verify_ledger()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report))

    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
