"""Replay a historical cycle without trading (Tier 4 debug)."""

from __future__ import annotations

import argparse
import json
import sys

from agenttrade import db as ledger
from agenttrade.buy_guard import enforce_no_margin_order_guard
from agenttrade.risk import evaluate_proposed_order


def replay_cycle(cycle_run_id: int) -> dict:
    """Read-only replay — never submits orders."""
    ledger.init_db()
    cycle = ledger.get_cycle_run(cycle_run_id)
    if not cycle:
        return {"ok": False, "message": f"Cycle {cycle_run_id} not found"}

    data = ledger.get_cycle_signals(cycle_run_id)
    report = {
        "ok": True,
        "cycle_run_id": cycle_run_id,
        "cycle": cycle,
        "candidates": [],
        "reddit_sentiment": data.get("sentiment_scores") or [],
        "attributions": data.get("signal_attributions") or [],
        "analysis_decisions": [],
        "risk_results": [],
        "hypothetical_orders": [],
        "safety_checks": [],
        "risk_events": data.get("risk_events") or [],
        "reconciliation": data.get("reconciliation"),
    }

    for row in data.get("strategy_signals") or []:
        sig = str(row.get("signal") or "").upper()
        if sig == "CANDIDATE":
            report["candidates"].append(row)
        else:
            report["analysis_decisions"].append(row)

    for snap in data.get("signal_snapshots") or []:
        if str(snap.get("signal_type") or "").upper() != "ANALYSIS":
            continue
        if str(snap.get("decision") or "").upper() != "BUY":
            continue

        sym = snap.get("symbol")
        price = float(snap.get("price") or 0)
        decision = {
            "ticker": sym,
            "action": "BUY",
            "current_price": price,
            "stop_loss_price": None,
            "shares": None,
        }
        try:
            comps = json.loads(snap.get("components_json") or "{}")
        except json.JSONDecodeError:
            comps = {}
        decision["signal_components"] = comps

        acct_snap = ledger.get_latest_account_snapshot() or {}
        account = {
            "account": {
                "cash": acct_snap.get("cash") or 50000,
                "equity": acct_snap.get("equity") or 100000,
                "buying_power": acct_snap.get("buying_power") or 50000,
            },
            "open_buy_notional": 0,
        }

        ok, reason, adj = evaluate_proposed_order(decision, account, cycle_run_id=cycle_run_id)
        guard_ok, guard_reason = enforce_no_margin_order_guard(
            account.get("account") or account,
            {
                "side": "buy",
                "symbol": sym,
                "qty": adj.get("shares"),
                "current_price": price,
            },
        )

        risk_row = {
            "symbol": sym,
            "sizing_approved": ok,
            "sizing_reason": reason,
            "adjusted": adj,
            "margin_guard_ok": guard_ok,
            "margin_guard_reason": guard_reason,
            "would_submit": ok and guard_ok,
        }
        report["risk_results"].append(risk_row)

        if ok and guard_ok:
            report["hypothetical_orders"].append({
                "symbol": sym,
                "side": "buy",
                "qty": adj.get("shares"),
                "notional": adj.get("notional_usd"),
                "stop_loss_price": adj.get("stop_loss_price"),
                "take_profit_price": adj.get("take_profit_price"),
                "status": "would_submit",
            })
        else:
            report["safety_checks"].append({
                "symbol": sym,
                "blocked": True,
                "reason": reason or guard_reason,
            })

    recon_status = data.get("reconciliation")
    if recon_status and not recon_status.get("passed"):
        report["safety_checks"].append({
            "type": "reconciliation",
            "blocked": True,
            "reason": recon_status.get("message"),
        })

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay historical cycle (no orders)")
    parser.add_argument("--cycle-id", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = replay_cycle(args.cycle_id)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"Cycle {args.cycle_id} replay")
        print(f"  Candidates: {len(report.get('candidates', []))}")
        print(f"  Reddit rows: {len(report.get('reddit_sentiment', []))}")
        print(f"  Analysis decisions: {len(report.get('analysis_decisions', []))}")
        print(f"  Would submit: {len(report.get('hypothetical_orders', []))}")
        print(f"  Safety blocks: {len(report.get('safety_checks', []))}")
        for o in report.get("hypothetical_orders") or []:
            print(f"    ORDER {o['symbol']} qty={o.get('qty')} stop={o.get('stop_loss_price')}")
        for b in report.get("safety_checks") or []:
            print(f"    BLOCKED {b.get('symbol', b.get('type'))}: {b.get('reason')}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
