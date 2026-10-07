#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_cycle.sh — Cron-safe trading cycle runner for YunoHost
#
# What this does:
#   1. Uses flock to prevent two cycles running at the same time
#      (e.g. if a screener run is slow and the next cron fires)
#   2. Runs run_trading_cycle() as a single-shot Python call (no scheduler loop)
#   3. Copies agent_state.json into the web directory so the dashboard
#      auto-refreshes without manual uploads
#   4. Rotates the log if it gets larger than 10MB
#
# Usage (cron uses TZ=America/New_York — times below are US Eastern):
#   35  9  * * 1-5  /opt/trading-agent/run_cycle.sh   # 09:35 ET (~14:35 UTC EST / 13:35 EDT)
#   30 11  * * 1-5  /opt/trading-agent/run_cycle.sh   # 11:30 ET
#   30 13  * * 1-5  /opt/trading-agent/run_cycle.sh   # 13:30 ET
#    0 15  * * 1-5  /opt/trading-agent/run_cycle.sh   # 15:00 ET
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Cron inherits TZ from /etc/cron.d/trading-agent; set here for manual runs too.
export TZ="${TZ:-America/New_York}"

APP_DIR="/opt/trading-agent"
VENV="$APP_DIR/.venv/bin/python"
WEB_DIR="/var/www/my_webapp__3/www"
LOCK="/tmp/trading-agent-cycle.lock"
LOG="$APP_DIR/trading_agent.log"
MAX_LOG_BYTES=10485760   # 10MB

cd "$APP_DIR"

# buckets.py reads CRYPTO_MAX_ALLOCATION (and the other allocation knobs) while
# the module is imported, which is before load_dotenv() runs. Cron does not
# export .env by itself. `set -a` exports every sourced assignment into Python.
if [ -f "$APP_DIR/.env" ]; then
    set +u
    set -a
    # shellcheck disable=SC1091
    source "$APP_DIR/.env"
    set +a
    set -u
fi

echo "── $(date '+%Y-%m-%d %H:%M:%S %Z') run_cycle.sh starting ──"

# DEPLOY_SHA.txt is canonical. The extensionless DEPLOY_SHA is the same bytes.
# This reconciles a disagreement left by an older deploy. It does not advance
# the stamp to a newer git HEAD; update_deploy.sh / write_deploy_sha.sh do that.
if [ -f "$APP_DIR/write_deploy_sha.sh" ]; then
    # shellcheck disable=SC1091
    source "$APP_DIR/write_deploy_sha.sh"
    reconcile_deploy_sha "$APP_DIR" || true
    if [ -n "${DEPLOY_SHA_VALUE:-}" ]; then
        echo "deploy sha ${DEPLOY_SHA_VALUE} (DEPLOY_SHA.txt and DEPLOY_SHA match)"
    fi
fi

# ── Log rotation ──────────────────────────────────────────────────────────────
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG_BYTES" ]; then
    mv "$LOG" "${LOG}.1"
    echo "Log rotated at $(date)"
fi

# ── Run with file lock (prevents overlap) ─────────────────────────────────────
# flock -E 75 is only the "lock already held" code. The Python exit status is
# passed through, so a crashed cycle is not reported as a skip and is not
# swallowed with exit 0.
CYCLE_RC=0
/usr/bin/flock -n -E 75 "$LOCK" \
    "$VENV" -c "
import sys, os
sys.path.insert(0, '$APP_DIR')
os.chdir('$APP_DIR')
from agent import run_trading_cycle
run_trading_cycle()
" || CYCLE_RC=$?

if [ "$CYCLE_RC" -eq 75 ]; then
    echo "⚠️  Another cycle is already running (flock busy) — skipping this run."
    exit 0
fi
if [ "$CYCLE_RC" -ne 0 ]; then
    echo "✗ trading cycle failed (exit $CYCLE_RC). Lock was acquired; not a flock skip."
    exit "$CYCLE_RC"
fi

# ── Publish state to web dashboard ───────────────────────────────────────────
if [ -f "$APP_DIR/agent_state.json" ]; then
    cp "$APP_DIR/agent_state.json" "$WEB_DIR/agent_state.json"
    echo "✓ agent_state.json published to web directory"
fi
# Publish the one recorded commit as both names. Never copy the two files
# independently; a stale extensionless file must not outlive DEPLOY_SHA.txt.
if [ -f "$APP_DIR/DEPLOY_SHA.txt" ]; then
    cp "$APP_DIR/DEPLOY_SHA.txt" "$WEB_DIR/DEPLOY_SHA.txt"
    cp "$APP_DIR/DEPLOY_SHA.txt" "$WEB_DIR/DEPLOY_SHA"
    echo "✓ deploy sha $(awk 'NF { print $1; exit }' "$APP_DIR/DEPLOY_SHA.txt") published to both SHA files"
fi
if [ -f "$APP_DIR/performance_history.json" ]; then
    cp "$APP_DIR/performance_history.json" "$WEB_DIR/performance_history.json"
    echo "✓ performance_history.json published to web directory"
fi
if [ -f "$APP_DIR/trade_log.json" ]; then
    cp "$APP_DIR/trade_log.json" "$WEB_DIR/trade_log.json"
    echo "✓ trade_log.json published to web directory"
fi

# Also copy settings and dashboard if they've been updated
if [ -f "$APP_DIR/dashboard.html" ]; then
    if [ "$APP_DIR/dashboard.html" -nt "$WEB_DIR/index.html" ] || [ "$APP_DIR/dashboard.html" -nt "$WEB_DIR/dashboard.html" ]; then
        cp "$APP_DIR/dashboard.html" "$WEB_DIR/index.html"
        cp "$APP_DIR/dashboard.html" "$WEB_DIR/dashboard.html"
        echo "✓ dashboard.html published as index.html and dashboard.html"
    fi
fi
if [ -f "$APP_DIR/settings.html" ] && [ "$APP_DIR/settings.html" -nt "$WEB_DIR/settings.html" ]; then
    cp "$APP_DIR/settings.html" "$WEB_DIR/settings.html"
    echo "✓ settings.html published"
fi

echo "── $(date '+%Y-%m-%d %H:%M:%S %Z') run_cycle.sh done ──"
