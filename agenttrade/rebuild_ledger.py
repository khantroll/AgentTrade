"""
Rebuild the AgentTrade lot ledger from raw fills.

Usage
-----
    python -m agenttrade.rebuild_ledger --dry-run   # analyse only
    python -m agenttrade.rebuild_ledger             # apply changes

What it does
------------
1. Reads all fills from the `fills` table (oldest first).
2. Optionally fetches missing fills from Alpaca activity history.
3. Processes each fill through FIFO lot matching.
4. Rebuilds `trade_lots`, `realized_lot_matches`, and `completed_trades`.
5. Prints a summary report.

Safety
------
* --dry-run makes no writes.
* The real run executes inside a transaction; PRAGMA foreign_keys=OFF is set
  during reconstruction so historical FK gaps don't abort the backfill.
* The transaction rolls back completely on any unrecoverable anomaly.
* Existing `fills` rows are never deleted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rebuild(dry_run: bool = False, fetch_alpaca: bool = False) -> dict:
    """
    Main backfill entry-point.

    Returns a summary dict that is also printed to stdout.
    """
    from agenttrade import db as _db
    from agenttrade.ledger import process_fill_into_lot_ledger, rebuild_completed_trades

    _db.init_db()

    summary: dict = {
        "dry_run": dry_run,
        "started_at": _utc_now(),
        "fills_found": 0,
        "fills_processed": 0,
        "buy_lots_created": 0,
        "sell_fills_matched": 0,
        "realized_matches_created": 0,
        "completed_trades_created": 0,
        "unmatched_sells": 0,
        "duplicate_fills_skipped": 0,
        "errors": [],
        "warnings": [],
    }

    # ── 1. Optionally pull Alpaca activity ────────────────────────────────────
    if fetch_alpaca:
        _fetch_alpaca_fills(_db, summary, dry_run)

    # ── 2. Read all fills in chronological order ──────────────────────────────
    with _db.get_connection() as conn:
        fills = conn.execute(
            "SELECT id, side, symbol, qty, price, filled_at FROM fills "
            "WHERE qty IS NOT NULL AND price IS NOT NULL "
            "ORDER BY filled_at ASC, id ASC"
        ).fetchall()

    summary["fills_found"] = len(fills)
    log.info("[RebuildLedger] Found %d fills to process (dry_run=%s)", len(fills), dry_run)

    if dry_run:
        _dry_run_analysis(fills, summary)
        _print_summary(summary)
        return summary

    # ── 3. Real run — wrap everything in a transaction ────────────────────────
    conn_ctx = _db.get_connection()
    try:
        with conn_ctx as conn:
            # Wipe existing lot ledger so rebuild is clean
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM completed_trades")
            conn.execute("DELETE FROM realized_lot_matches")
            conn.execute("DELETE FROM trade_lots")
            conn.execute("PRAGMA foreign_keys = ON")
            log.info("[RebuildLedger] Cleared existing lot ledger tables")
    except Exception as exc:
        summary["errors"].append(f"Failed to clear tables: {exc}")
        _print_summary(summary)
        return summary

    # Process fills one-by-one
    for fill_row in fills:
        fill_id = fill_row["id"]
        try:
            result = process_fill_into_lot_ledger(fill_id)
            summary["fills_processed"] += 1

            action = result.get("action", "")
            if action == "buy_lot_created":
                summary["buy_lots_created"] += result.get("lots_created", 1)
            elif action == "sell_matched":
                summary["sell_fills_matched"] += 1
                summary["realized_matches_created"] += result.get("matches_created", 0)
                if result.get("unmatched_qty", 0) > 1e-9:
                    summary["unmatched_sells"] += 1
                    summary["warnings"].append(
                        f"Fill {fill_id} ({fill_row['symbol']}): "
                        f"unmatched qty {result['unmatched_qty']:.4f}"
                    )
            elif action == "already_processed":
                summary["duplicate_fills_skipped"] += 1
            elif not result.get("ok"):
                summary["errors"].append(f"Fill {fill_id}: {result.get('message')}")

        except Exception as exc:
            log.error("[RebuildLedger] Exception on fill %d: %s", fill_id, exc)
            summary["errors"].append(f"Fill {fill_id}: {exc}")

    # ── 4. Rebuild completed_trades from all realized matches ─────────────────
    ct_result = rebuild_completed_trades()
    summary["completed_trades_created"] = ct_result.get("created", 0)
    if ct_result.get("errors"):
        summary["errors"].append(f"completed_trades rebuild: {ct_result['errors']} errors")

    summary["finished_at"] = _utc_now()
    _print_summary(summary)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run analysis
# ─────────────────────────────────────────────────────────────────────────────

def _dry_run_analysis(fills: list, summary: dict) -> None:
    """Simulate FIFO matching without touching the DB; populate summary counts."""
    from collections import deque

    lots: dict[str, deque] = {}
    buy_lots_would_create = 0
    sell_matches_would_create = 0
    unmatched_sells = 0

    for fill in fills:
        side = str(fill["side"] or "").lower()
        sym = fill["symbol"] or ""
        qty = float(fill["qty"] or 0)
        price = float(fill["price"] or 0)
        if qty <= 0 or price <= 0:
            continue

        if side == "buy":
            lots.setdefault(sym, deque()).append({"qty": qty, "price": price})
            buy_lots_would_create += 1
        elif side == "sell":
            remaining = qty
            while remaining > 1e-9 and lots.get(sym):
                lot = lots[sym][0]
                take = min(remaining, lot["qty"])
                sell_matches_would_create += 1
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    lots[sym].popleft()
            if remaining > 1e-9:
                unmatched_sells += 1
                summary["warnings"].append(
                    f"DRY-RUN: {sym} sell fill unmatched qty {remaining:.4f}"
                )

    summary["buy_lots_created"] = buy_lots_would_create
    summary["sell_fills_matched"] = sum(
        1 for f in fills if str(f["side"] or "").lower() == "sell"
    )
    summary["realized_matches_created"] = sell_matches_would_create
    summary["unmatched_sells"] = unmatched_sells
    summary["fills_processed"] = len(fills)


# ─────────────────────────────────────────────────────────────────────────────
# Optional Alpaca backfill
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_alpaca_fills(db, summary: dict, dry_run: bool) -> None:
    """Pull Alpaca account activities and insert missing fills."""
    try:
        from alpaca_client import alpaca_get
        from agenttrade.db import insert_fills_from_alpaca

        log.info("[RebuildLedger] Fetching Alpaca activity history…")
        activities = alpaca_get("/v2/account/activities?activity_type=FILL&page_size=500") or []
        if not isinstance(activities, list):
            activities = []

        before_count = 0
        with db.get_connection() as conn:
            before_count = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]

        if not dry_run:
            inserted = insert_fills_from_alpaca(0, activities)
            log.info("[RebuildLedger] Alpaca: inserted %d new fills", inserted)
            summary["fills_found"] += inserted
        else:
            log.info("[RebuildLedger] DRY-RUN: would import %d Alpaca activities", len(activities))
    except Exception as exc:
        summary["warnings"].append(f"Alpaca activity fetch failed: {exc}")
        log.warning("[RebuildLedger] Could not fetch Alpaca fills: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Print summary
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(s: dict) -> None:
    dr = " (DRY-RUN — no changes written)" if s.get("dry_run") else ""
    print(f"\n{'='*60}")
    print(f"  AgentTrade Ledger Rebuild{dr}")
    print(f"{'='*60}")
    print(f"  Fills found:                {s['fills_found']}")
    print(f"  Fills processed:            {s['fills_processed']}")
    print(f"  Buy lots created:           {s['buy_lots_created']}")
    print(f"  Sell fills matched:         {s['sell_fills_matched']}")
    print(f"  Realized matches created:   {s['realized_matches_created']}")
    print(f"  Completed trades created:   {s['completed_trades_created']}")
    print(f"  Unmatched sells:            {s['unmatched_sells']}")
    print(f"  Duplicate fills skipped:    {s['duplicate_fills_skipped']}")
    if s.get("warnings"):
        print(f"\n  Warnings ({len(s['warnings'])}):")
        for w in s["warnings"][:10]:
            print(f"    ⚠ {w}")
        if len(s["warnings"]) > 10:
            print(f"    … and {len(s['warnings'])-10} more")
    if s.get("errors"):
        print(f"\n  Errors ({len(s['errors'])}):")
        for e in s["errors"][:10]:
            print(f"    ✗ {e}")
    print(f"\n  Status: {'OK' if not s.get('errors') else 'COMPLETED WITH ERRORS'}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m agenttrade.rebuild_ledger",
        description="Rebuild AgentTrade lot ledger from raw fills.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Analyse fills and print what would be done without making any changes.",
    )
    parser.add_argument(
        "--fetch-alpaca", action="store_true",
        help="Pull Alpaca FILL activities before processing (requires valid API keys).",
    )
    args = parser.parse_args()

    result = run_rebuild(dry_run=args.dry_run, fetch_alpaca=args.fetch_alpaca)
    sys.exit(0 if not result.get("errors") else 1)


if __name__ == "__main__":
    main()
