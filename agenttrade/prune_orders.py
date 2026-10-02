"""Slim historical ``orders.raw_json`` and reclaim stuck cycle runs.

The live paper ledger grew to ~3.7 GB because each order row stored a
multi-megabyte ``raw_json`` blob (a previous blob nested inside the next
one, plus screener/OHLCV/LLM bodies). This rewrites those blobs in place.
It does not delete order rows, fills, or cycle history.

Stop the trading agent before running this against the live database.
An in-place ``VACUUM`` needs about as much free disk as the current file
(~3.7 GB). ``VACUUM INTO`` writes a compact new file instead, which only
needs room for the slim copy.

Examples::

    python -m agenttrade.prune_orders --db /opt/trading-agent/agenttrade.sqlite3

    python -m agenttrade.prune_orders \\
        --db /opt/trading-agent/agenttrade.sqlite3 \\
        --reclaim-cycle 2313 \\
        --vacuum-into /opt/trading-agent/agenttrade.sqlite3.pruned
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def vacuum_into(source: str, dest: str) -> None:
    """Write a compact copy of ``source`` to ``dest``. Does not delete ``source``."""
    if os.path.abspath(source) == os.path.abspath(dest):
        raise SystemExit("refusing to VACUUM INTO the same file as the source")
    if os.path.exists(dest):
        raise SystemExit(f"refusing to overwrite existing file {dest}")
    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(source, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        escaped = os.path.abspath(dest).replace("'", "''")
        conn.execute(f"VACUUM INTO '{escaped}'")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Path to agenttrade.sqlite3 (default: AGENTTRADE_DB_PATH or app dir)")
    parser.add_argument(
        "--reclaim-cycle",
        type=int,
        action="append",
        default=[],
        help="Mark this cycle_runs.id interrupted if it is still status=running. Repeatable.",
    )
    parser.add_argument(
        "--reclaim-stale-running",
        action="store_true",
        help="Also interrupt every other status=running row older than --stale-minutes",
    )
    parser.add_argument("--stale-minutes", type=int, default=30)
    parser.add_argument(
        "--vacuum-into",
        help="After the prune, write a compact database to this new path. Does not replace the original.",
    )
    args = parser.parse_args(argv)
    if args.db:
        os.environ["AGENTTRADE_DB_PATH"] = args.db

    from agenttrade import db

    db.init_db()
    stats = db.prune_orders_raw_json()
    print(
        f"orders.raw_json pruned: rows={stats['rows']} "
        f"bytes {stats['bytes_before']} -> {stats['bytes_after']}"
    )
    print("Order rows were not deleted. File size stays large until VACUUM INTO.")

    reclaimed = []
    for cycle_id in args.reclaim_cycle:
        reclaimed.extend(
            db.reclaim_running_cycles(
                cycle_id,
                notes="OOM or crash before finish; reclaimed without deleting history",
            )
        )
    if args.reclaim_stale_running:
        reclaimed.extend(db.reclaim_running_cycles(older_than_minutes=args.stale_minutes))
    if reclaimed:
        print("cycle_runs marked interrupted: " + ", ".join(str(row["id"]) for row in reclaimed))
    elif args.reclaim_cycle or args.reclaim_stale_running:
        print("no status=running cycle rows matched")

    if args.vacuum_into:
        vacuum_into(db.get_db_path(), args.vacuum_into)
        print(f"compact copy written to {args.vacuum_into}")
        print("Stop the agent, move the original aside, then move this file into its place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
