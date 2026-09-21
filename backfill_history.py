#!/usr/bin/env python3
"""
backfill_history.py — Import daily portfolio equity from Alpaca into performance_history.jsonl.

Usage:
  cd /opt/trading-agent
  .venv/bin/python backfill_history.py           # default 90 days
  .venv/bin/python backfill_history.py --days 30
  .venv/bin/python backfill_history.py --days 365 --publish
"""
from __future__ import annotations

import argparse
import os
import sys

APP = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP)
sys.path.insert(0, APP)


def main():
    parser = argparse.ArgumentParser(description="Backfill performance history from Alpaca")
    parser.add_argument("--days", type=int, default=90, help="Days of daily equity to import (default 90)")
    parser.add_argument("--publish", action="store_true", help="Publish performance_history.json to web dir")
    args = parser.parse_args()

    from performance_history import backfill_from_alpaca, publish_history

    result = backfill_from_alpaca(days=args.days)
    public = os.getenv("PUBLIC_DASHBOARD_DIR", "/var/www/my_webapp__3/www")

    if args.publish or result.get("ok"):
        publish_history(public)

    print(f"ok={result.get('ok')} added={result.get('added', 0)} total={result.get('total', 0)}")
    if result.get("message"):
        print(f"message={result['message']}")
    print(f"jsonl={os.path.join(APP, 'performance_history.jsonl')}")
    print(f"json={os.path.join(public, 'performance_history.json')}")

    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
