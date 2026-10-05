"""AgentTrade package entry.

``python -m agenttrade`` migrates legacy state.
``python -m agenttrade daily-trades --explain`` prints the daily entry count.
"""

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("daily-trades", "daily_trades"):
        from agenttrade.daily_trades import main as daily_main
        raise SystemExit(daily_main(sys.argv[2:]))
    from agenttrade.migrate_state import main as migrate_main
    migrate_main()


if __name__ == "__main__":
    main()
