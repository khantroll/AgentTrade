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

"$VENV" -c "
import sys, os
sys.path.insert(0, '$APP_DIR')
os.chdir('$APP_DIR')
from agent import monitor_positions
monitor_positions()
"
