"""Manual reset for TRADING_PAUSED (Tier 2 consecutive-loss pause)."""

from __future__ import annotations

import argparse
import sys

from agenttrade import db as ledger
from agenttrade.risk import reset_trading_pause


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clear TRADING_PAUSED after human review")
    parser.add_argument(
        "--reason",
        default="Reviewed consecutive losses",
        help="Reason recorded in risk_events",
    )
    args = parser.parse_args(argv)

    ledger.init_db()
    if not ledger.trading_paused():
        print("TRADING_PAUSED is already false — no action taken.")
        return 0

    reset_trading_pause(args.reason)
    print(f"TRADING_PAUSED cleared. Reason logged: {args.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
