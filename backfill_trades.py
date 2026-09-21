#!/usr/bin/env python3
"""One-shot: import Alpaca fills into trade_log.jsonl and publish to web dir."""
import argparse
import os
import sys

APP = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP)
sys.path.insert(0, APP)


def main():
    parser = argparse.ArgumentParser(description="Backfill trade log from Alpaca fills")
    parser.add_argument("--days", type=int, default=90, help="Days of fills to import (default 90)")
    args = parser.parse_args()

    from trade_log import sync_trade_log, publish_trade_log

    result = sync_trade_log(days=args.days)
    public = os.getenv("PUBLIC_DASHBOARD_DIR", "/var/www/my_webapp__3/www")
    if result.get("ok"):
        publish_trade_log(public)

    print(f"ok={result.get('ok')} added={result.get('added', 0)} total={result.get('total', 0)}")
    if result.get("message"):
        print(result["message"])
    print(f"jsonl={os.path.join(APP, 'trade_log.jsonl')}")
    print(f"json={os.path.join(public, 'trade_log.json')}")
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
