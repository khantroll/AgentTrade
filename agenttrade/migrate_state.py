"""Migrate legacy agent_state.json into SQLite ledger."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime

import agent_config as cfg
from agenttrade import db as ledger

log = logging.getLogger(__name__)


def _migration_plan(state: dict) -> dict:
    """Describe what would be imported from legacy JSON (no writes)."""
    equity = state.get("equity") or state.get("portfolio_value")
    flags = {}
    if equity:
        flags["STARTING_EQUITY"] = str(equity)
        flags["HIGH_WATER_EQUITY"] = str(equity)
    flags.setdefault("TRADING_HALTED", "false")
    for key in ("TRADING_HALTED", "TRADING_PAUSED"):
        val = state.get(key.lower())
        if val:
            flags[key] = str(val)
    return {
        "decisions": len(state.get("decisions") or []),
        "positions": len(state.get("positions") or []),
        "account": {
            "equity": equity,
            "cash": state.get("cash"),
            "buying_power": state.get("buying_power"),
            "portfolio_value": state.get("portfolio_value"),
        },
        "system_flags": flags,
    }


def migrate_state(dry_run: bool = False) -> dict:
    """
    Back up agent_state.json, init SQLite, import useful history.
    Never deletes the original JSON on failure.

    With dry_run=True, parse and report the migration plan without writing
    backups, SQLite rows, or system flags.
    """
    state_path = cfg.STATE_FILE
    summary = {
        "ok": False,
        "dry_run": dry_run,
        "backup_path": None,
        "db_path": ledger.get_db_path(),
        "state_path": state_path,
        "imported": {},
        "message": "",
    }

    if not os.path.isfile(state_path):
        if dry_run:
            summary["ok"] = True
            summary["message"] = "Dry run — no agent_state.json; would initialize empty SQLite"
            summary["imported"] = {"decisions": 0, "positions": 0}
            return summary
        ledger.init_db()
        ledger.set_system_flag("MIGRATION_COMPLETED", datetime.utcnow().isoformat() + "Z")
        summary["ok"] = True
        summary["message"] = "No agent_state.json — empty SQLite initialized"
        return summary

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(cfg.APP_DIR, f"agent_state.backup.{ts}.json")
    summary["backup_path"] = backup

    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        summary["message"] = f"Could not read agent_state.json: {e}"
        return summary

    plan = _migration_plan(state)
    summary["imported"] = {
        "decisions": plan["decisions"],
        "positions": plan["positions"],
    }

    if dry_run:
        summary["ok"] = True
        summary["would_backup_to"] = backup
        summary["would_import"] = plan
        summary["message"] = (
            f"Dry run — would back up to {backup}, "
            f"import {plan['positions']} positions and {plan['decisions']} decisions"
        )
        log.info("[Migrate] %s", summary["message"])
        return summary

    try:
        shutil.copy2(state_path, backup)
        ledger.init_db()
        cycle_id = ledger.start_cycle_run("migration")
        acct = plan["account"]
        ledger.insert_account_snapshot(cycle_id, "migration_import", acct)
        ledger.insert_positions(cycle_id, "migration", state.get("positions") or [])

        for d in state.get("decisions") or []:
            ledger.insert_strategy_signal(cycle_id, {
                "strategy_name": "migration",
                "symbol": d.get("ticker"),
                "signal": d.get("action", "SKIP"),
                "confidence": d.get("confidence"),
                "reason": d.get("rationale"),
            })

        for key, val in plan["system_flags"].items():
            ledger.set_system_flag(key, val)

        ledger.finish_cycle_run(cycle_id, "migration_complete", "Imported from agent_state.json")
        ledger.set_system_flag("MIGRATION_COMPLETED", datetime.utcnow().isoformat() + "Z")
        summary["ok"] = True
        summary["message"] = "Migration completed — SQLite is now authoritative for trading state"
    except Exception as e:
        log.exception("Migration failed")
        summary["message"] = f"Migration failed (JSON preserved): {e}"

    return summary


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    import argparse
    parser = argparse.ArgumentParser(description="Migrate agent_state.json to SQLite")
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Parse agent_state.json and print migration plan without writing files or SQLite",
    )
    args = parser.parse_args()
    result = migrate_state(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
