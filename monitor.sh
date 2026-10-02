#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# monitor.sh — Position monitor (runs every 30 min during market hours)
#
# Crontab entry:
#   */30 9-15 * * 1-5  /opt/trading-agent/deploy/monitor.sh >> /opt/trading-agent/cron.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

APP_DIR="/opt/trading-agent"
VENV="$APP_DIR/.venv/bin/python"

cd "$APP_DIR"

# Same import-time env requirement as run_cycle.sh. CRYPTO_MAX_ALLOCATION and
# the other allocation knobs are read when buckets.py is imported, before
# load_dotenv() runs, so .env has to be exported into this process first.
if [ -f "$APP_DIR/.env" ]; then
    set +u
    set -a
    # shellcheck disable=SC1091
    source "$APP_DIR/.env"
    set +a
    set -u
fi

"$VENV" -c "
import sys, os
sys.path.insert(0, '$APP_DIR')
os.chdir('$APP_DIR')
from agent import monitor_positions
monitor_positions()
"
