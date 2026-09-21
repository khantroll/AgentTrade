#!/bin/bash
# verify_backtest_deploy.sh — run ON THE SERVER as root
# Checks that backtest API is wired up (404 usually means old config_server still running).

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/trading-agent}"
VENV="$APP_DIR/.venv/bin/python"
WEB_DIR="${WEB_DIR:-/var/www/my_webapp__3/www}"

echo "=== AgentTrade backtest deploy check ==="
echo "App dir: $APP_DIR"
echo ""

fail=0
ok()   { echo "  OK   $*"; }
bad()  { echo "  FAIL $*"; fail=1; }

for f in backtest.py backtest_engine.py backtest_data.py backtest_decisions.py backtest_simulator.py \
         config_server.py period_summary.py buckets.py agent_config.py screener.py; do
  [ -f "$APP_DIR/$f" ] && ok "$f" || bad "missing $APP_DIR/$f"
done

[ -f "$APP_DIR/agents/risk.py" ] && ok "agents/risk.py" || bad "missing agents/risk.py"

if grep -q '@app.route("/backtest"' "$APP_DIR/config_server.py" 2>/dev/null; then
  ok 'config_server.py has @app.route("/backtest")'
else
  bad 'config_server.py has NO /backtest route — copy latest config_server.py from repo'
fi

if systemctl is-active --quiet trading-agent-config; then
  ok "trading-agent-config is running"
else
  bad "trading-agent-config is NOT running — sudo systemctl start trading-agent-config"
fi

echo ""
echo "--- Quick import test (venv) ---"
if "$VENV" -c "from backtest_engine import run_backtest; print('import ok')" 2>/dev/null; then
  ok "backtest_engine imports in .venv"
else
  bad "backtest_engine import failed — run: $APP_DIR/.venv/bin/pip install -r $APP_DIR/requirements.txt"
  "$VENV" -c "from backtest_engine import run_backtest" 2>&1 | head -5 || true
fi

echo ""
echo "--- Live HTTP (Flask on 127.0.0.1:5111) ---"
code=$(curl -s -o /tmp/bt_probe.json -w '%{http_code}' "http://127.0.0.1:5111/backtest?days=30&capital=10000&interval=5" || echo "000")
if [ "$code" = "404" ]; then
  bad "GET /backtest returned HTTP 404 — config_server process is OLD; run: sudo systemctl restart trading-agent-config"
elif [ "$code" = "000" ]; then
  bad "Cannot reach Flask on :5111 — is trading-agent-config running?"
else
  ok "GET /backtest returned HTTP $code (200 or 500 means route exists; 500 may be runtime error)"
  head -c 120 /tmp/bt_probe.json 2>/dev/null; echo ""
fi

if [ -f "$WEB_DIR/api.php" ]; then
  if grep -q 'backtest.*120' "$WEB_DIR/api.php" 2>/dev/null || grep -q "backtest" "$WEB_DIR/api.php"; then
    ok "api.php mentions backtest"
  else
    bad "api.php may be old (no backtest / short timeout)"
  fi
else
  bad "missing $WEB_DIR/api.php"
fi

echo ""
if [ "$fail" -eq 0 ]; then
  echo "All checks passed (or only long-running backtest still in progress)."
else
  echo "Fix failures above, then:"
  echo "  sudo systemctl restart trading-agent-config"
  echo "  curl -s 'http://127.0.0.1:5111/backtest?days=30' | head -c 200"
fi
exit "$fail"
