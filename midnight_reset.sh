#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# midnight_reset.sh — Clears daily counters at midnight
#
# Crontab entry:
#   0 0 * * *  /opt/trading-agent/deploy/midnight_reset.sh >> /opt/trading-agent/cron.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

APP_DIR="/opt/trading-agent"
VENV="$APP_DIR/.venv/bin/python"

cd "$APP_DIR"

echo "── $(date '+%Y-%m-%d %H:%M:%S') midnight reset ──"

"$VENV" -c "
import sys, os, json
sys.path.insert(0, '$APP_DIR')
os.chdir('$APP_DIR')

# Reset daily_trades in agent_state.json
try:
    with open('agent_state.json') as f:
        state = json.load(f)
    state['daily_trades'] = 0
    state['daily_sells_today'] = 0
    state['daily_sells_placed_today'] = 0
    state.pop('emergency_rebalance_at', None)
    if state.get('buy_lock_reason') in ('recent_sell', 'emergency_rebalance'):
        state.pop('buy_lock_reason', None)
    with open('agent_state.json', 'w') as f:
        json.dump(state, f, indent=2)
    print('daily_trades and sell counters reset in agent_state.json')
except Exception as e:
    print(f'Reset warning (non-critical): {e}')

# Reset token_usage.json (new day, fresh budget)
try:
    import os
    if os.path.exists('token_usage.json'):
        os.remove('token_usage.json')
        print('token_usage.json cleared for new day')
except Exception as e:
    print(f'Token reset warning: {e}')
"

echo "── reset done ──"
