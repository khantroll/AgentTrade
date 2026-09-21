#!/bin/bash
# =============================================================================
# install.sh — AgentTrade YunoHost Installer v2
# =============================================================================
# All files must be in the SAME directory as this script. No subfolders.
#
# Required files:
#   agent.py  agent_config.py  alpaca_client.py  market_data.py  cycle.py
#   agents/*.py  screener.py  buckets.py  llm_router.py  config_server.py
#   requirements.txt  dashboard.html  settings.html  api.php  .env.example
#   run_cycle.sh  monitor.sh  midnight_reset.sh  trading-agent-config.service
#
# Usage:
#   cd /home/deploy
#   sudo bash install.sh
# =============================================================================

set -euo pipefail

APP_DIR="/opt/trading-agent"
WEB_DIR="/var/www/my_webapp__3/www"
VENV="$APP_DIR/.venv"
SERVICE="trading-agent-config"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN="nedragaardkeep.quest"
APP_PATH="AgentTrader"   # YunoHost app URL path

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

[ "$EUID" -ne 0 ] && error "Run as root: sudo bash install.sh"

echo -e "${BOLD}"
echo "  █████╗  ██████╗ ███████╗███╗   ██╗████████╗"
echo " ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝"
echo " ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   "
echo " ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   "
echo " ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   "
echo " ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝  "
echo -e " TRADE  —  YunoHost Installer v2${NC}"
echo ""
info "Source:  $SRC"
info "App dir: $APP_DIR"
info "Web dir: $WEB_DIR"
echo ""

# ── Check source files ────────────────────────────────────────────────────────
section "Checking source files"
REQUIRED=(
    "agent.py" "agent_config.py" "alpaca_client.py" "market_data.py" "cycle.py" "analysis_funnel.py"
    "agents/research.py" "agents/analysis.py" "agents/risk.py" "agents/execution.py"
    "agents/position_review.py"
    "screener.py" "buckets.py" "llm_router.py" "config_server.py"
    "requirements.txt" "dashboard.html" "settings.html" "api.php"
    "run_cycle.sh" "monitor.sh" "midnight_reset.sh" "trading-agent-config.service"
)
MISSING=0
for f in "${REQUIRED[@]}"; do
    if [ ! -f "$SRC/$f" ]; then
        warn "Missing: $f"; MISSING=$((MISSING+1))
    else
        info "  ✓ $f"
    fi
done
[ "$MISSING" -gt 0 ] && error "$MISSING file(s) missing from $SRC"
success "All source files present"

# ── Step 1: App directory ─────────────────────────────────────────────────────
section "Step 1: App directory"
mkdir -p "$APP_DIR"
success "Created $APP_DIR"

# ── Step 2: Python files ──────────────────────────────────────────────────────
section "Step 2: Copying Python files"
for f in agent.py agent_config.py alpaca_client.py market_data.py cycle.py \
         analysis_funnel.py app_paths.py \
         backtest.py backtest_engine.py backtest_data.py backtest_decisions.py backtest_simulator.py \
         period_summary.py performance_history.py trade_log.py pnl_attribution.py \
         llm_attribution.py rationale_attribution.py report_export.py \
         health_check.py screener_cache.py screener.py buckets.py llm_router.py config_server.py requirements.txt \
         backfill_stops.py; do
    cp "$SRC/$f" "$APP_DIR/$f" && info "  ✓ $f"
done
mkdir -p "$APP_DIR/agents"
cp "$SRC/agents/"*.py "$APP_DIR/agents/" && info "  ✓ agents/*.py"
for f in run_cycle.sh monitor.sh midnight_reset.sh verify_backtest_deploy.sh; do
    cp "$SRC/$f" "$APP_DIR/$f"
    chmod +x "$APP_DIR/$f"
    info "  ✓ $f (executable)"
done

# Patch run_cycle.sh with correct web dir
sed -i "s|/var/www/my_webapp/www|$WEB_DIR|g"   "$APP_DIR/run_cycle.sh"
sed -i "s|/var/www/my_webapp_3/www|$WEB_DIR|g" "$APP_DIR/run_cycle.sh"
sed -i "s|/var/www/my_webapp__3/www|$WEB_DIR|g" "$APP_DIR/run_cycle.sh"
info "  ✓ run_cycle.sh patched → $WEB_DIR"

# .env
if [ ! -f "$APP_DIR/.env" ]; then
    [ -f "$SRC/.env.example" ] && cp "$SRC/.env.example" "$APP_DIR/.env" \
        && warn ".env created from .env.example — add API keys via settings.html"
else
    info "  .env already exists — not overwriting"
fi
success "Python files copied"

# ── Step 3: Virtual environment ───────────────────────────────────────────────
section "Step 3: Python virtual environment"
if [ ! -d "$VENV" ]; then
    info "Creating virtual environment..."
    python3 -m venv "$VENV"
    success "venv created"
fi
info "Installing dependencies..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
success "Dependencies installed"

# ── Step 4: Web files ─────────────────────────────────────────────────────────
section "Step 4: Web files → $WEB_DIR"
[ -d "$WEB_DIR" ] || error "$WEB_DIR not found. Is My Webapp installed?"

cp "$SRC/dashboard.html" "$WEB_DIR/index.html"
info "  ✓ dashboard.html → index.html"

cp "$SRC/settings.html" "$WEB_DIR/settings.html"
info "  ✓ settings.html"

# api.php — PHP proxy that bypasses YunoHost SSOwat Lua auth
# Sits in the web dir so PHP processes it server-side, talking directly
# to Flask on 127.0.0.1:5111 without going through nginx/SSOwat
cp "$SRC/api.php" "$WEB_DIR/api.php"
info "  ✓ api.php (PHP proxy for config server — bypasses SSOwat)"

chmod 644 "$WEB_DIR/index.html" "$WEB_DIR/settings.html" "$WEB_DIR/api.php"

# Placeholder agent_state.json
if [ ! -f "$WEB_DIR/agent_state.json" ]; then
    cat > "$WEB_DIR/agent_state.json" << 'JSONEOF'
{
  "mode": "PAPER", "portfolio_value": "0", "cash": "0",
  "positions": [], "last_orders": [], "daily_trades": 0,
  "buckets": [], "rebalance": {}, "screener_sources": {}, "universes": {},
  "token_usage": {"total_tokens":0,"total_cost_usd":0,"calls":0,
    "budget":200000,"by_model":{},"calls_log":[]}
}
JSONEOF
    chmod 644 "$WEB_DIR/agent_state.json"
    info "  ✓ agent_state.json placeholder"
fi
success "Web files deployed"

# ── Step 5: Permissions ───────────────────────────────────────────────────────
section "Step 5: Permissions"
chown -R root:root "$APP_DIR"
chmod 750 "$APP_DIR"
touch "$APP_DIR/trading_agent.log" "$APP_DIR/cron.log"
chmod 644 "$APP_DIR/trading_agent.log" "$APP_DIR/cron.log"
[ -f "$APP_DIR/.env" ] && chmod 600 "$APP_DIR/.env"
success "Permissions set"

# ── Step 6: Systemd service ───────────────────────────────────────────────────
section "Step 6: Config server systemd service"
sed "s|/opt/trading-agent|$APP_DIR|g" \
    "$SRC/trading-agent-config.service" \
    > "/etc/systemd/system/${SERVICE}.service"

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 3
systemctl is-active --quiet "$SERVICE" \
    && success "Config server running (http://127.0.0.1:5111)" \
    || warn "Config server may not have started — check: sudo journalctl -u $SERVICE -n 20"

# ── Step 7: Clean nginx conf ──────────────────────────────────────────────────
section "Step 7: Clean nginx configuration"
NGINX_CONF="/etc/nginx/conf.d/${DOMAIN}.conf"

if [ -f "$NGINX_CONF" ]; then
    cp "$NGINX_CONF" "${NGINX_CONF}.bak.install.$(date +%s)"
    info "Backed up existing nginx conf"

    # Strip any config-api blocks left from previous install attempts
    python3 - << 'PYEOF'
import re
CONF = '/etc/nginx/conf.d/nedragaardkeep.quest.conf'
try:
    with open(CONF) as f:
        content = f.read()
    # Remove any orphaned proxy directives or config-api location blocks
    cleaned = re.sub(
        r'\s*#[^\n]*AgentTrade[^\n]*\n(?:\s*#[^\n]*\n)?(?:\s*location\s+[^\{]*\{[^}]*\}\s*)?',
        '\n', content, flags=re.DOTALL
    )
    cleaned = re.sub(
        r'\s*location\s+[^\{]*config-api[^\{]*\{[^}]*\}',
        '', cleaned, flags=re.DOTALL
    )
    # Remove orphaned proxy_pass directives outside location blocks
    cleaned = re.sub(
        r'(?<!location[^\n]{0,50})\n\s+proxy_pass\s+http://127\.0\.0\.1:5111[^\n]*\n(?:\s+proxy_[^\n]+\n)*',
        '\n', cleaned
    )
    # Fix double closing braces from previous botched insertions
    while '\n    }\n\n    }\n' in cleaned:
        cleaned = cleaned.replace('\n    }\n\n    }\n', '\n    }\n')
    with open(CONF, 'w') as f:
        f.write(cleaned)
    print('nginx conf cleaned')
except Exception as e:
    print(f'Cleanup warning (non-critical): {e}')
PYEOF

    if nginx -t 2>&1 | grep -q "successful"; then
        systemctl reload nginx
        success "nginx reloaded with clean conf"
    else
        warn "nginx conf has issues — showing errors:"
        nginx -t 2>&1 | tail -5
        warn "The dashboard will still work. Fix nginx manually if needed."
    fi
else
    info "nginx conf not found at $NGINX_CONF — skipping (non-critical)"
fi

# ── Step 8: Cron jobs ─────────────────────────────────────────────────────────
section "Step 8: Cron jobs"
TZ_CURRENT=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "unknown")
info "Server timezone: $TZ_CURRENT"

cat > /etc/cron.d/trading-agent << CRONEOF
# AgentTrade — AI Trading Agent
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
TZ=America/New_York

# 4 trading cycles per weekday (US market hours ET)
35  9  * * 1-5  root  $APP_DIR/run_cycle.sh      >> $APP_DIR/cron.log 2>&1
30 11  * * 1-5  root  $APP_DIR/run_cycle.sh      >> $APP_DIR/cron.log 2>&1
30 13  * * 1-5  root  $APP_DIR/run_cycle.sh      >> $APP_DIR/cron.log 2>&1
 0 15  * * 1-5  root  $APP_DIR/run_cycle.sh      >> $APP_DIR/cron.log 2>&1

# Position monitor every 30 min during market hours
*/30 9-15 * * 1-5  root  $APP_DIR/monitor.sh     >> $APP_DIR/cron.log 2>&1

# Midnight reset — clears daily trade counter + token budget
0 0 * * *          root  $APP_DIR/midnight_reset.sh >> $APP_DIR/cron.log 2>&1

CRONEOF
chmod 644 /etc/cron.d/trading-agent
success "Cron jobs installed"

# ── Step 9: Test PHP proxy ────────────────────────────────────────────────────
section "Step 9: Testing PHP proxy"
sleep 2
PHP_URL="https://${DOMAIN}/${APP_PATH}/api.php?_path=status"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$PHP_URL" 2>/dev/null || echo "000")
info "PHP proxy test: $PHP_URL → HTTP $CODE"
if [ "$CODE" = "200" ]; then
    success "PHP proxy working — settings.html can talk to config server!"
elif [ "$CODE" = "302" ]; then
    warn "302 redirect — YunoHost SSO intercepting PHP file."
    warn "This may mean the app path is different. Try:"
    warn "  curl -sk https://${DOMAIN}/${APP_PATH}/api.php?_path=status"
    warn "If wrong path, edit APP_PATH at top of this script and re-run."
else
    warn "HTTP $CODE — PHP may not be processing the file yet."
    info "Test manually: curl -sk $PHP_URL"
fi

# ── Sanity checks ─────────────────────────────────────────────────────────────
section "Sanity checks"
PASS=0; TOTAL=0
chk() {
    TOTAL=$((TOTAL+1))
    eval "$2" &>/dev/null \
        && { success "$1"; PASS=$((PASS+1)); } \
        || warn "FAIL — $1"
}
chk "Python venv"             "[ -d $VENV ]"
chk "agent.py"                "[ -f $APP_DIR/agent.py ]"
chk "screener.py"             "[ -f $APP_DIR/screener.py ]"
chk "llm_router.py"           "[ -f $APP_DIR/llm_router.py ]"
chk "config_server.py"        "[ -f $APP_DIR/config_server.py ]"
chk "run_cycle.sh executable" "[ -x $APP_DIR/run_cycle.sh ]"
chk "dashboard deployed"      "[ -f $WEB_DIR/index.html ]"
chk "settings.html deployed"  "[ -f $WEB_DIR/settings.html ]"
chk "api.php deployed"        "[ -f $WEB_DIR/api.php ]"
chk "agent_state.json"        "[ -f $WEB_DIR/agent_state.json ]"
chk "Cron file"               "[ -f /etc/cron.d/trading-agent ]"
chk "Service enabled"         "systemctl is-enabled $SERVICE"
chk "Service running"         "systemctl is-active $SERVICE"
chk "Flask responds"          "curl -sf http://127.0.0.1:5111/status"
chk "anthropic importable"    "$VENV/bin/python -c 'import anthropic'"
chk "yfinance importable"     "$VENV/bin/python -c 'import yfinance'"
chk "flask importable"        "$VENV/bin/python -c 'import flask'"

echo ""
[ "$PASS" -eq "$TOTAL" ] \
    && echo -e "${GREEN}${BOLD}  ✓ All $TOTAL checks passed${NC}" \
    || echo -e "${YELLOW}${BOLD}  $PASS / $TOTAL checks passed${NC}"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  Installation complete!${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}Dashboard:${NC}   https://${DOMAIN}/${APP_PATH}/"
echo -e "  ${CYAN}Settings:${NC}    https://${DOMAIN}/${APP_PATH}/settings.html"
echo -e "  ${CYAN}App files:${NC}   $APP_DIR"
echo -e "  ${CYAN}Web files:${NC}   $WEB_DIR"
echo -e "  ${CYAN}Cron jobs:${NC}   /etc/cron.d/trading-agent"
echo ""
echo -e "  ${YELLOW}${BOLD}Next steps:${NC}"
echo -e "  1. Add API keys:  https://${DOMAIN}/${APP_PATH}/settings.html"
echo -e "  2. Test a cycle:  sudo bash $APP_DIR/run_cycle.sh"
echo -e "  3. Watch logs:    tail -f $APP_DIR/cron.log"
echo ""
echo -e "  ${CYAN}Note:${NC} settings.html uses api.php as a PHP proxy to reach"
echo -e "  the config server, bypassing YunoHost's SSOwat Lua auth."
echo ""
