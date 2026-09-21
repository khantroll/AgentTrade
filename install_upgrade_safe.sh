#!/bin/bash
# =============================================================================
# install_upgrade_safe.sh — AgentTrade production upgrade installer
# =============================================================================
# Purpose:
#   Upgrade an EXISTING AgentTrade installation in /opt/trading-agent from a
#   recovered/development source tree without replacing production state.
#
# Preserved on production (never copied from source by this script):
#   /opt/trading-agent/.env
#   /opt/trading-agent/agenttrade.sqlite3
#   /opt/trading-agent/bucket_tags.json
#   /opt/trading-agent/agent_state.json
#   logs, existing state backups, cron configuration, nginx configuration
#
# Deployed:
#   top-level Python source files
#   agents/*.py
#   agenttrade/*.py
#   selected runtime shell scripts
#   requirements.txt
#   dashboard.html -> web index.html
#   settings.html
#   api.php
#
# Usage:
#   cd /path/to/AgentTrade-source
#   sudo bash install_upgrade_safe.sh
#
# Optional:
#   sudo bash install_upgrade_safe.sh --run-tests
# =============================================================================

set -euo pipefail

APP_DIR="/opt/trading-agent"
WEB_DIR="/var/www/my_webapp__3/www"
VENV="$APP_DIR/.venv"
SERVICE="trading-agent-config"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TESTS=0

if [[ "${1:-}" == "--run-tests" ]]; then
    RUN_TESTS=1
elif [[ $# -gt 0 ]]; then
    echo "Unknown option: $1" >&2
    echo "Usage: sudo bash install_upgrade_safe.sh [--run-tests]" >&2
    exit 2
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

[[ "$EUID" -eq 0 ]] || error "Run as root: sudo bash install_upgrade_safe.sh"
[[ -d "$APP_DIR" ]] || error "$APP_DIR does not exist. This script is for upgrades, not first installs."
[[ -d "$WEB_DIR" ]] || error "$WEB_DIR does not exist. Verify the YunoHost web-app path."

section "AgentTrade upgrade"
info "Source:      $SRC"
info "Application: $APP_DIR"
info "Web root:    $WEB_DIR"
info "Service:     $SERVICE"

# -----------------------------------------------------------------------------
# Validate source tree
# -----------------------------------------------------------------------------
section "Validating source tree"

REQUIRED=(
    "agent.py"
    "agent_config.py"
    "alpaca_client.py"
    "market_data.py"
    "cycle.py"
    "analysis_funnel.py"
    "agents/__init__.py"
    "agents/research.py"
    "agents/analysis.py"
    "agents/risk.py"
    "agents/execution.py"
    "agents/position_review.py"
    "agenttrade/__init__.py"
    "agenttrade/db.py"
    "agenttrade/ledger.py"
    "agenttrade/reconciliation.py"
    "agenttrade/risk.py"
    "agenttrade/publish.py"
    "requirements.txt"
    "dashboard.html"
    "settings.html"
    "api.php"
)

MISSING=0
for f in "${REQUIRED[@]}"; do
    if [[ ! -f "$SRC/$f" ]]; then
        warn "Missing required source file: $f"
        MISSING=$((MISSING + 1))
    else
        info "  ✓ $f"
    fi
done
[[ "$MISSING" -eq 0 ]] || error "$MISSING required source file(s) are missing. Nothing has been changed."
success "Source tree looks complete"

# -----------------------------------------------------------------------------
# Backup existing production installation before touching source
# -----------------------------------------------------------------------------
section "Creating rollback backup"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$APP_DIR/backups"
BACKUP_TAR="$BACKUP_DIR/pre-upgrade-$TIMESTAMP.tar.gz"
WEB_BACKUP="$BACKUP_DIR/pre-upgrade-web-$TIMESTAMP.tar.gz"
mkdir -p "$BACKUP_DIR"

# Archive the production application while excluding recursive/large/ephemeral
# content. The important mutable files (.env, SQLite, bucket tags, state) are
# deliberately INCLUDED in this rollback snapshot.
tar \
    --exclude='./backups' \
    --exclude='./.venv' \
    --exclude='./venv' \
    --exclude='./__pycache__' \
    --exclude='./agents/__pycache__' \
    --exclude='./agenttrade/__pycache__' \
    --exclude='./tests/__pycache__' \
    --exclude='./trading_agent.log' \
    --exclude='./trading_agent.log.*' \
    --exclude='./cron.log' \
    -czf "$BACKUP_TAR" \
    -C "$APP_DIR" .

# Back up only the AgentTrade-managed web files; do not archive the whole web app.
WEB_ITEMS=()
for f in index.html dashboard.html settings.html api.php agent_state.json; do
    [[ -f "$WEB_DIR/$f" ]] && WEB_ITEMS+=("$f")
done
if [[ ${#WEB_ITEMS[@]} -gt 0 ]]; then
    tar -czf "$WEB_BACKUP" -C "$WEB_DIR" "${WEB_ITEMS[@]}"
    success "Web rollback backup: $WEB_BACKUP"
fi
success "Application rollback backup: $BACKUP_TAR"

# Capture hashes of production mutable state before deployment.
DB_HASH_BEFORE=""
ENV_HASH_BEFORE=""
TAGS_HASH_BEFORE=""
[[ -f "$APP_DIR/agenttrade.sqlite3" ]] && DB_HASH_BEFORE="$(sha256sum "$APP_DIR/agenttrade.sqlite3" | awk '{print $1}')"
[[ -f "$APP_DIR/.env" ]] && ENV_HASH_BEFORE="$(sha256sum "$APP_DIR/.env" | awk '{print $1}')"
[[ -f "$APP_DIR/bucket_tags.json" ]] && TAGS_HASH_BEFORE="$(sha256sum "$APP_DIR/bucket_tags.json" | awk '{print $1}')"

# -----------------------------------------------------------------------------
# Deploy application source
# -----------------------------------------------------------------------------
section "Deploying AgentTrade source"

# Copy every top-level Python file from the recovered tree. This keeps the
# installed flat application layer synchronized without maintaining another
# fragile hand-written module list. Mutable JSON/SQLite/env files are not .py
# and therefore cannot be overwritten by this loop.
shopt -s nullglob
TOPLEVEL_PY=("$SRC"/*.py)
[[ ${#TOPLEVEL_PY[@]} -gt 0 ]] || error "No top-level Python files found in $SRC"
for src_file in "${TOPLEVEL_PY[@]}"; do
    base="$(basename "$src_file")"
    install -m 0644 "$src_file" "$APP_DIR/$base"
done
info "  ✓ ${#TOPLEVEL_PY[@]} top-level Python files"

mkdir -p "$APP_DIR/agents" "$APP_DIR/agenttrade"
AGENT_FILES=("$SRC"/agents/*.py)
AGENTTRADE_FILES=("$SRC"/agenttrade/*.py)
[[ ${#AGENT_FILES[@]} -gt 0 ]] || error "No agents/*.py files found"
[[ ${#AGENTTRADE_FILES[@]} -gt 0 ]] || error "No agenttrade/*.py files found"

for src_file in "${AGENT_FILES[@]}"; do
    install -m 0644 "$src_file" "$APP_DIR/agents/$(basename "$src_file")"
done
info "  ✓ ${#AGENT_FILES[@]} agents/*.py files"

for src_file in "${AGENTTRADE_FILES[@]}"; do
    install -m 0644 "$src_file" "$APP_DIR/agenttrade/$(basename "$src_file")"
done
info "  ✓ ${#AGENTTRADE_FILES[@]} agenttrade/*.py files"

install -m 0644 "$SRC/requirements.txt" "$APP_DIR/requirements.txt"

# Runtime shell helpers: deploy if present. Do NOT change /etc/cron.d here.
for f in run_cycle.sh monitor.sh midnight_reset.sh verify_backtest_deploy.sh verify_ledger_deploy.sh; do
    if [[ -f "$SRC/$f" ]]; then
        install -m 0755 "$SRC/$f" "$APP_DIR/$f"
        info "  ✓ $f"
    fi
done

# Preserve production mutable state by design.
for f in .env agenttrade.sqlite3 bucket_tags.json agent_state.json; do
    if [[ -e "$SRC/$f" ]]; then
        info "  ↷ source $f intentionally NOT copied"
    fi
done

# -----------------------------------------------------------------------------
# Deploy dashboard assets
# -----------------------------------------------------------------------------
section "Deploying dashboard assets"
install -m 0644 "$SRC/dashboard.html" "$WEB_DIR/index.html"
install -m 0644 "$SRC/settings.html" "$WEB_DIR/settings.html"
install -m 0644 "$SRC/api.php" "$WEB_DIR/api.php"
info "  ✓ dashboard.html → $WEB_DIR/index.html"
info "  ✓ settings.html"
info "  ✓ api.php"

# -----------------------------------------------------------------------------
# Python environment
# -----------------------------------------------------------------------------
section "Updating Python dependencies"
if [[ ! -x "$VENV/bin/python" ]]; then
    warn "$VENV is missing; creating it with python3 -m venv"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
success "Python dependencies updated"

# -----------------------------------------------------------------------------
# Protect mutable state and validate that installer did not replace it
# -----------------------------------------------------------------------------
section "Verifying preserved production state"

if [[ -n "$DB_HASH_BEFORE" ]]; then
    DB_HASH_AFTER="$(sha256sum "$APP_DIR/agenttrade.sqlite3" | awk '{print $1}')"
    [[ "$DB_HASH_BEFORE" == "$DB_HASH_AFTER" ]] \
        || error "Production agenttrade.sqlite3 changed during deployment. Restore from $BACKUP_TAR"
    success "agenttrade.sqlite3 unchanged"
else
    warn "No production agenttrade.sqlite3 existed before upgrade"
fi

if [[ -n "$ENV_HASH_BEFORE" ]]; then
    ENV_HASH_AFTER="$(sha256sum "$APP_DIR/.env" | awk '{print $1}')"
    [[ "$ENV_HASH_BEFORE" == "$ENV_HASH_AFTER" ]] \
        || error "Production .env changed during deployment. Restore from $BACKUP_TAR"
    chmod 600 "$APP_DIR/.env"
    success ".env unchanged"
fi

if [[ -n "$TAGS_HASH_BEFORE" ]]; then
    TAGS_HASH_AFTER="$(sha256sum "$APP_DIR/bucket_tags.json" | awk '{print $1}')"
    [[ "$TAGS_HASH_BEFORE" == "$TAGS_HASH_AFTER" ]] \
        || error "Production bucket_tags.json changed during deployment. Restore from $BACKUP_TAR"
    success "bucket_tags.json unchanged"
fi

# -----------------------------------------------------------------------------
# Static/runtime validation BEFORE service restart
# -----------------------------------------------------------------------------
section "Validating deployed code"

"$VENV/bin/python" -m compileall -q "$APP_DIR"
success "compileall passed"

# Import smoke test only. It deliberately does not call run_trading_cycle(),
# broker APIs, LLM APIs, or other external services.
cd "$APP_DIR"
"$VENV/bin/python" - <<'PY'
import importlib
modules = [
    "agent",
    "cycle",
    "analysis_funnel",
    "agents.research",
    "agents.analysis",
    "agents.risk",
    "agents.execution",
    "agents.position_review",
    "agenttrade.db",
    "agenttrade.ledger",
    "agenttrade.reconciliation",
    "agenttrade.risk",
    "agenttrade.publish",
]
for name in modules:
    importlib.import_module(name)
    print(f"import OK: {name}")
PY
success "Import smoke test passed"

if [[ "$RUN_TESTS" -eq 1 ]]; then
    section "Running test suite"
    if [[ -d "$SRC/tests" ]]; then
        if ! "$VENV/bin/python" -c 'import pytest' 2>/dev/null; then
            info "pytest not installed; installing for requested test run"
            "$VENV/bin/pip" install pytest --quiet
        fi
        cd "$SRC"
        "$VENV/bin/python" -m pytest -q
        success "Test suite passed"
    else
        warn "--run-tests requested but no tests/ directory exists in source"
    fi
fi

# -----------------------------------------------------------------------------
# Restart existing service. Do not rewrite systemd unit, cron, or nginx.
# -----------------------------------------------------------------------------
section "Restarting AgentTrade config service"
systemctl daemon-reload
if systemctl list-unit-files "${SERVICE}.service" --no-legend 2>/dev/null | grep -q "${SERVICE}.service"; then
    systemctl restart "$SERVICE"
    sleep 2
    if systemctl is-active --quiet "$SERVICE"; then
        success "$SERVICE is running"
    else
        warn "$SERVICE did not become active"
        journalctl -u "$SERVICE" -n 30 --no-pager || true
        error "Service restart failed. Application backup: $BACKUP_TAR"
    fi
else
    warn "${SERVICE}.service is not installed; source was upgraded but no service was restarted"
fi

# -----------------------------------------------------------------------------
# Final health checks. These do not execute a trading cycle.
# -----------------------------------------------------------------------------
section "Final health checks"
PASS=0
TOTAL=0
chk() {
    TOTAL=$((TOTAL + 1))
    if eval "$2" &>/dev/null; then
        success "$1"
        PASS=$((PASS + 1))
    else
        warn "FAIL — $1"
    fi
}

chk "agent.py"                    "[ -f '$APP_DIR/agent.py' ]"
chk "cycle.py"                    "[ -f '$APP_DIR/cycle.py' ]"
chk "agents/risk.py"              "[ -f '$APP_DIR/agents/risk.py' ]"
chk "agenttrade/risk.py"          "[ -f '$APP_DIR/agenttrade/risk.py' ]"
chk "agenttrade/ledger.py"        "[ -f '$APP_DIR/agenttrade/ledger.py' ]"
chk "production SQLite ledger"    "[ -f '$APP_DIR/agenttrade.sqlite3' ]"
chk "dashboard deployed"          "[ -f '$WEB_DIR/index.html' ]"
chk "settings deployed"           "[ -f '$WEB_DIR/settings.html' ]"
chk "API proxy deployed"          "[ -f '$WEB_DIR/api.php' ]"
chk "Python venv"                 "[ -x '$VENV/bin/python' ]"
chk "config server HTTP status"   "curl -sf http://127.0.0.1:5111/status"

cat <<SUMMARY

══════════════════════════════════════════════════
 AgentTrade upgrade complete
══════════════════════════════════════════════════
 Source:          $SRC
 Application:     $APP_DIR
 Web root:        $WEB_DIR
 Rollback backup: $BACKUP_TAR
 Checks:          $PASS / $TOTAL passed

 NOT performed:
   • No trading cycle was started
   • No Alpaca orders were submitted
   • No production SQLite database was copied from source
   • No .env was copied from source
   • No nginx configuration was changed
   • No cron configuration was changed

 Suggested manual verification:
   cd $APP_DIR
   $VENV/bin/python agenttrade/verify_ledger.py
   systemctl status $SERVICE --no-pager
   tail -n 100 $APP_DIR/cron.log

 To run the recovered test suite during a future upgrade:
   sudo bash install_upgrade_safe.sh --run-tests
SUMMARY

[[ "$PASS" -eq "$TOTAL" ]] \
    && success "All final checks passed" \
    || warn "$PASS / $TOTAL final checks passed — review warnings before running a live cycle"
