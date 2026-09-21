"""
AI Trading Agent - Entry Point
==============================
Multi-agent system with bucket-aware portfolio management and multi-model LLM routing.

Run directly for scheduled daemon mode:
    python agent.py

Cron / single-shot (YunoHost):
    from agent import run_trading_cycle
    run_trading_cycle()

Module layout:
    agent_config.py   — env, paths, bucket manager, trade counters
    alpaca_client.py  — Alpaca REST helpers
    market_data.py    — yFinance fetch, audit logs, prompt compaction
    agents/           — research, analysis, risk, execution pipeline stages
    cycle.py          — run_trading_cycle orchestration
"""

import logging
import os
import sys
import time

import schedule

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
for _dep in (
    "account_sync.py",
    "buy_guard.py",
    "buy_lock.py",
    "alpaca_client.py",
    "order_utils.py",
    "cycle.py",
    "agenttrade/db.py",
    "agenttrade/reconciliation.py",
):
    if not os.path.isfile(os.path.join(_APP_DIR, _dep)):
        print(f"ERROR: missing {_dep} in {_APP_DIR} — copy full deploy bundle")
        sys.exit(1)

import agent_config as cfg
from config_server import start_server
from cycle import monitor_positions, reset_daily_counters, run_trading_cycle
from llm_router import active_mode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("trading_agent.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

try:
    from agenttrade.db import init_db
    init_db()
    log.info("SQLite ledger initialized at %s", __import__("agenttrade.db", fromlist=["get_db_path"]).get_db_path())
except Exception as _db_init_e:
    log.error("SQLite ledger init failed — trading will be blocked: %s", _db_init_e)

__all__ = ["run_trading_cycle", "monitor_positions", "reset_daily_counters"]


if __name__ == "__main__":
    log.info("🚀 AI Trading Agent starting...")
    log.info("   LLM mode:  %s", active_mode())
    log.info("   Buckets:   %s", [b.name for b in cfg.bucket_manager.active_buckets()])
    log.info("   Paper:     %s", cfg.ALPACA_PAPER)

    try:
        start_server(port=5111)
        log.info("   Config UI: open settings.html in your browser")
    except Exception as e:
        log.warning("   Config server failed to start (non-critical): %s", e)

    schedule.every().day.at("09:35").do(run_trading_cycle)
    schedule.every().day.at("11:30").do(run_trading_cycle)
    schedule.every().day.at("13:30").do(run_trading_cycle)
    schedule.every().day.at("15:00").do(run_trading_cycle)
    schedule.every().day.at("00:00").do(reset_daily_counters)
    schedule.every(30).minutes.do(monitor_positions)

    run_trading_cycle()

    log.info("Scheduler running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(60)
