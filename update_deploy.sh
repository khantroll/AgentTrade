#!/usr/bin/env bash
# =============================================================================
# update_deploy.sh â€” AgentTrade incremental update (SQLite ledger + reconciliation)
# =============================================================================
# Run from a copy of the project (e.g. after git pull or rsync to the server):
#
#   cd /path/to/agenttrade_v5
#   sudo bash update_deploy.sh
#
# If you see "$'\r': command not found" (Windows line endings), fix on server:
#   sed -i 's/\r$//' update_deploy.sh verify_ledger_deploy.sh
#   # or: dos2unix update_deploy.sh
#
# Or non-interactive with defaults:
#   sudo bash update_deploy.sh -y
#
# In-place update (already running from APP_DIR):
#   cd /opt/trading-agent && sudo bash update_deploy.sh -y
#
# Local/dev only (no systemd, no web copy):
#   bash update_deploy.sh --local-only
# =============================================================================

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_APP="/opt/trading-agent"
DEFAULT_WEB="/var/www/my_webapp__3/www"
DEFAULT_SERVICE="trading-agent-config"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${BOLD}â”€â”€ $* â”€â”€${NC}"; }

ASSUME_YES=0
LOCAL_ONLY=0
SKIP_MIGRATE=0
SKIP_VERIFY=0
SKIP_RESTART=0
SKIP_TESTS=0
DRY_RUN_MIGRATE_ONLY=0

usage() {
    cat << 'EOF'
Usage: bash update_deploy.sh [OPTIONS]

Options:
  -y, --yes           Accept defaults; skip most prompts
  --local-only        Update files in APP_DIR only (no web/systemd; for dev)
  --app-dir PATH      Target app directory (default: /opt/trading-agent)
  --web-dir PATH      Dashboard web root (default: /var/www/my_webapp__3/www)
  --service NAME      systemd service (default: trading-agent-config)
  --skip-migrate      Do not run SQLite migration
  --skip-verify       Do not run verify_ledger after deploy
  --skip-restart      Do not restart config server
  --skip-tests        Do not run pytest (if tests/ present)
  --migrate-dry-run   Run migration dry-run only (no deploy writes for migration)
  -h, --help          Show this help

Interactive prompts appear for paths and migration unless -y is set.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --local-only) LOCAL_ONLY=1 ;;
        --app-dir) DEFAULT_APP="$2"; shift ;;
        --web-dir) DEFAULT_WEB="$2"; shift ;;
        --service) DEFAULT_SERVICE="$2"; shift ;;
        --skip-migrate) SKIP_MIGRATE=1 ;;
        --skip-verify) SKIP_VERIFY=1 ;;
        --skip-restart) SKIP_RESTART=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        --migrate-dry-run) DRY_RUN_MIGRATE_ONLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) error "Unknown option: $1 (use -h for help)" ;;
    esac
    shift
done

prompt() {
    local var_name="$1"
    local prompt_text="$2"
    local default_val="$3"
    if [[ "$ASSUME_YES" -eq 1 ]]; then
        printf -v "$var_name" '%s' "$default_val"
        info "$prompt_text â†’ $default_val (default, -y)"
        return
    fi
    read -r -p "$prompt_text [$default_val]: " reply
    if [[ -z "$reply" ]]; then
        printf -v "$var_name" '%s' "$default_val"
    else
        printf -v "$var_name" '%s' "$reply"
    fi
}

confirm() {
    local question="$1"
    local default_yes="${2:-n}"
    if [[ "$ASSUME_YES" -eq 1 ]]; then
        return 0
    fi
    local hint="y/N"
    [[ "$default_yes" == "y" ]] && hint="Y/n"
    read -r -p "$question [$hint]: " reply
    reply="${reply:-$default_yes}"
    [[ "$reply" =~ ^[Yy] ]]
}

# â”€â”€ Files touched by SQLite ledger + reconciliation update â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
UPDATE_PY=(
    agent.py agent_config.py alpaca_client.py cycle.py config_server.py
    account_sync.py buy_guard.py buy_lock.py order_utils.py
    buckets.py margin_correction.py
    confidence_engine.py signal_attribution.py agreement_engine.py signal_performance.py
    screener.py screener_cache.py
)
UPDATE_AGENTS=(
    agents/execution.py agents/risk.py agents/position_review.py
    agents/research.py agents/analysis.py
)
UPDATE_AGENTTRADE=(
    agenttrade/__init__.py agenttrade/__main__.py agenttrade/db.py
    agenttrade/reconciliation.py agenttrade/risk.py agenttrade/migrate_state.py
    agenttrade/publish.py agenttrade/recording.py agenttrade/verify_ledger.py
    agenttrade/buy_guard.py agenttrade/indicators.py agenttrade/reset_pause.py
    agenttrade/backtest.py agenttrade/replay.py agenttrade/compare_modes.py
    agenttrade/performance.py agenttrade/signal_report.py agenttrade/walk_forward.py
    agenttrade/strategy_modes.py
    agenttrade/ledger.py agenttrade/rebuild_ledger.py
)
UPDATE_WEB=(
    dashboard.html
)
UPDATE_SCRIPTS=(
    update_deploy.sh verify_ledger_deploy.sh
)
ENV_KEYS=(
    AGENTTRADE_DB_PATH
    MAX_RISK_PER_TRADE_PCT
    MAX_ACCOUNT_DRAWDOWN_PCT
    MAX_CONSECUTIVE_LOSSES
    ALLOW_SHORTS
)

echo -e "${BOLD}"
echo " AgentTrade â€” update_deploy.sh"
echo " SQLite ledger + Reconciliation Gate"
echo -e "${NC}"
info "Source (this copy): $SRC"
echo ""

# â”€â”€ Paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Target paths"
prompt APP_DIR "App directory (Python + SQLite)" "$DEFAULT_APP"
prompt WEB_DIR "Web dashboard directory (index.html)" "$DEFAULT_WEB"
prompt SERVICE "systemd service name" "$DEFAULT_SERVICE"

VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

if [[ "$LOCAL_ONLY" -eq 1 ]]; then
    warn "Local-only mode â€” skipping web deploy and systemd restart"
    SKIP_RESTART=1
fi

IN_PLACE=0
if [[ "$(cd "$SRC" && pwd)" == "$(cd "$APP_DIR" 2>/dev/null && pwd)" ]]; then
    IN_PLACE=1
    info "In-place update: source and app dir are the same"
fi

# â”€â”€ Preflight â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Preflight checks"
MISSING=0
for f in "${UPDATE_PY[@]}" "${UPDATE_AGENTS[@]}" "${UPDATE_AGENTTRADE[@]}"; do
    if [[ ! -f "$SRC/$f" ]]; then
        warn "Missing in source: $f"
        MISSING=$((MISSING + 1))
    fi
done
[[ "$MISSING" -gt 0 ]] && error "$MISSING required file(s) missing from $SRC â€” sync repo first"

if [[ ! -d "$APP_DIR" ]]; then
    if confirm "App dir $APP_DIR does not exist. Create it?" "y"; then
        mkdir -p "$APP_DIR/agents" "$APP_DIR/agenttrade"
        success "Created $APP_DIR"
    else
        error "Aborted â€” app dir required"
    fi
fi

if [[ "$LOCAL_ONLY" -eq 0 && ! -d "$WEB_DIR" ]]; then
    warn "Web dir not found: $WEB_DIR"
    if ! confirm "Continue without dashboard deploy?" "n"; then
        error "Aborted â€” fix WEB_DIR or use --local-only"
    fi
    LOCAL_ONLY=1
fi

if [[ "$EUID" -ne 0 && "$LOCAL_ONLY" -eq 0 ]]; then
    warn "Not running as root â€” file copies to $APP_DIR may fail"
    if ! confirm "Continue anyway?" "n"; then
        error "Re-run with: sudo bash update_deploy.sh"
    fi
fi

if [[ ! -d "$VENV" ]]; then
    warn "Virtualenv not found at $VENV"
    if confirm "Create venv and install requirements?" "y"; then
        python3 -m venv "$VENV"
        "$PIP" install --upgrade pip -q
        "$PIP" install -r "$SRC/requirements.txt" -q
        success "venv created and dependencies installed"
    else
        error "venv required for migration and verify steps"
    fi
fi

if ! confirm "Proceed with deploy to $APP_DIR?" "y"; then
    info "Aborted by user"
    exit 0
fi

# â”€â”€ Backup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Backup"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$APP_DIR/backups/update-$TS"
mkdir -p "$BACKUP_DIR"

backup_if_exists() {
    local rel="$1"
    if [[ -f "$APP_DIR/$rel" ]]; then
        mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
        cp -a "$APP_DIR/$rel" "$BACKUP_DIR/$rel"
    fi
}

for f in "${UPDATE_PY[@]}" "${UPDATE_AGENTS[@]}" "${UPDATE_AGENTTRADE[@]}"; do
    backup_if_exists "$f"
done
[[ -f "$APP_DIR/.env" ]] && cp -a "$APP_DIR/.env" "$BACKUP_DIR/.env"
[[ -f "$APP_DIR/agent_state.json" ]] && cp -a "$APP_DIR/agent_state.json" "$BACKUP_DIR/agent_state.json"
[[ -f "$APP_DIR/agenttrade.sqlite3" ]] && cp -a "$APP_DIR/agenttrade.sqlite3" "$BACKUP_DIR/agenttrade.sqlite3" || true
success "Backup saved to $BACKUP_DIR"

# â”€â”€ Copy files â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Deploying files"
mkdir -p "$APP_DIR/agents" "$APP_DIR/agenttrade"

copy_file() {
    local rel="$1"
    local dest="$APP_DIR/$rel"
    mkdir -p "$(dirname "$dest")"
    cp "$SRC/$rel" "$dest"
    info "  âœ“ $rel"
}

for f in "${UPDATE_PY[@]}"; do copy_file "$f"; done
for f in "${UPDATE_AGENTS[@]}"; do copy_file "$f"; done
for f in "${UPDATE_AGENTTRADE[@]}"; do copy_file "$f"; done
for f in "${UPDATE_SCRIPTS[@]}"; do
    [[ -f "$SRC/$f" ]] && copy_file "$f" && chmod +x "$APP_DIR/$f" 2>/dev/null || true
done

if [[ -f "$SRC/requirements.txt" ]]; then
    cp "$SRC/requirements.txt" "$APP_DIR/requirements.txt"
    info "  âœ“ requirements.txt"
fi

if [[ "$LOCAL_ONLY" -eq 0 && -d "$WEB_DIR" && -f "$SRC/dashboard.html" ]]; then
    cp "$SRC/dashboard.html" "$WEB_DIR/index.html"
    chmod 644 "$WEB_DIR/index.html" 2>/dev/null || true
    success "dashboard.html â†’ $WEB_DIR/index.html"
    # Deploy JS modules (required since 2026-06-25 refactor)
    if [[ -d "$SRC/js" ]]; then
        mkdir -p "$WEB_DIR/js"
        cp "$SRC/js"/at-*.js "$WEB_DIR/js/"
        chmod 644 "$WEB_DIR/js"/at-*.js 2>/dev/null || true
        success "js/at-*.js â†’ $WEB_DIR/js/"
    else
        warn "js/ directory not found in $SRC â€” dashboard JS modules will be missing"
    fi
fi

success "Files deployed"

# Verify critical modules import from deployed app dir
section "Import verification"
IMPORT_FAIL=0
DEPLOY_IMPORTS=(
    confidence_engine
    signal_attribution
    signal_performance
    agreement_engine
    screener
    cycle
)
cd "$APP_DIR"
for mod in "${DEPLOY_IMPORTS[@]}"; do
    if "$PY" -c "import ${mod}" 2>/dev/null; then
        info "  import ok: ${mod}"
    else
        warn "  import FAILED: ${mod}"
        IMPORT_FAIL=$((IMPORT_FAIL + 1))
    fi
done
if [[ "$IMPORT_FAIL" -gt 0 ]]; then
    warn "$IMPORT_FAIL module(s) failed import check — cycle may crash until files are synced"
else
    success "All deploy-critical modules import successfully"
fi

# â”€â”€ Merge .env keys â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Environment (.env)"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" && -f "$SRC/.env.example" ]]; then
    cp "$SRC/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    warn "Created $ENV_FILE from .env.example â€” add API keys"
fi

if [[ -f "$ENV_FILE" && -f "$SRC/.env.example" ]]; then
    ADDED=0
    for key in "${ENV_KEYS[@]}"; do
        if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
            line="$(grep "^${key}=" "$SRC/.env.example" | head -1 || true)"
            if [[ -n "$line" ]]; then
                echo "$line" >> "$ENV_FILE"
                info "  + appended $key to .env"
                ADDED=$((ADDED + 1))
            fi
        fi
    done
    [[ "$ADDED" -eq 0 ]] && info "  .env already has SQLite ledger keys"
else
    warn "No .env at $ENV_FILE â€” set AGENTTRADE_DB_PATH and risk vars manually"
fi

# â”€â”€ Dependencies â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Python dependencies"
if [[ -f "$APP_DIR/requirements.txt" ]]; then
    if confirm "Run pip install -r requirements.txt?" "y"; then
        "$PIP" install -r "$APP_DIR/requirements.txt" -q
        success "Dependencies up to date"
    else
        info "Skipped pip install"
    fi
fi

# â”€â”€ Tests (optional, from source tree) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if [[ "$SKIP_TESTS" -eq 0 && -d "$SRC/tests" && -f "$SRC/requirements-dev.txt" ]]; then
    section "Tests (optional)"
    if confirm "Run pytest from source copy?" "n"; then
        TEST_PY="${SRC}/.venv/bin/python"
        if [[ ! -x "$TEST_PY" ]]; then
            python3 -m venv "$SRC/.venv" 2>/dev/null || true
            "$SRC/.venv/bin/pip" install -q -r "$SRC/requirements.txt" -r "$SRC/requirements-dev.txt" 2>/dev/null || \
                "$PIP" install -q pytest
            TEST_PY="$PY"
        fi
        if "$TEST_PY" -m pytest "$SRC/tests/test_deploy_imports.py" "$SRC/tests/test_agenttrade_db.py" \
            "$SRC/tests/test_reconciliation.py" \
            "$SRC/tests/test_agenttrade_risk.py" "$SRC/tests/test_verify_ledger.py" \
            "$SRC/tests/test_migrate_state.py" "$SRC/tests/test_recording.py" -q 2>/dev/null; then
            success "AgentTrade ledger tests passed"
        else
            warn "Some tests failed â€” review before trading live"
        fi
    fi
fi

# â”€â”€ SQLite migration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "SQLite migration"
export TRADING_AGENT_DIR="$APP_DIR"
cd "$APP_DIR"

if [[ "$SKIP_MIGRATE" -eq 1 ]]; then
    warn "Migration skipped (--skip-migrate)"
else
    info "Migration dry-run:"
    "$PY" -m agenttrade.migrate_state --dry-run || warn "Dry-run reported issues"
    echo ""

    if [[ "$DRY_RUN_MIGRATE_ONLY" -eq 1 ]]; then
        info "Stopping after dry-run (--migrate-dry-run)"
    elif confirm "Run migration for real? (backs up agent_state.json, creates/updates SQLite)" "y"; then
        if "$PY" -m agenttrade.migrate_state; then
            success "Migration completed"
        else
            error "Migration failed â€” original agent_state.json preserved in backup"
        fi
    else
        warn "Migration skipped â€” run later: $PY -m agenttrade.migrate_state"
    fi
fi

# â”€â”€ Verify ledger â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Ledger verification"
if [[ "$SKIP_VERIFY" -eq 1 ]]; then
    warn "verify_ledger skipped (--skip-verify)"
else
    if confirm "Compare live Alpaca vs SQLite now? (requires API keys in .env)" "y"; then
        if "$PY" -m agenttrade.verify_ledger; then
            success "Alpaca and SQLite match"
        else
            warn "Ledger mismatch or fetch error â€” expected if no cycle has run yet"
            info "After first cycle: $PY -m agenttrade.verify_ledger"
        fi
    else
        info "Skipped verify_ledger"
    fi
fi

# â”€â”€ Restart service â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Config server"
if [[ "$SKIP_RESTART" -eq 1 || "$LOCAL_ONLY" -eq 1 ]]; then
    info "Skipped systemd restart"
else
    if command -v systemctl >/dev/null 2>&1 && confirm "Restart $SERVICE?" "y"; then
        systemctl daemon-reload 2>/dev/null || true
        systemctl restart "$SERVICE"
        sleep 2
        if systemctl is-active --quiet "$SERVICE"; then
            success "$SERVICE is running"
        else
            warn "$SERVICE may not be running â€” check: journalctl -u $SERVICE -n 30"
        fi
    fi
fi

# â”€â”€ Post-deploy checks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
section "Sanity checks"
PASS=0; TOTAL=0
chk() {
    TOTAL=$((TOTAL + 1))
    if eval "$2" &>/dev/null; then
        success "$1"
        PASS=$((PASS + 1))
    else
        warn "FAIL â€” $1"
    fi
}

chk "margin_correction.py"          "[ -f $APP_DIR/margin_correction.py ]"
chk "agenttrade/ledger.py"          "[ -f $APP_DIR/agenttrade/ledger.py ]"
chk "agenttrade/rebuild_ledger.py"  "[ -f $APP_DIR/agenttrade/rebuild_ledger.py ]"
chk "agenttrade/db.py"              "[ -f $APP_DIR/agenttrade/db.py ]"
chk "agenttrade/reconciliation.py"  "[ -f $APP_DIR/agenttrade/reconciliation.py ]"
chk "agenttrade/backtest.py"        "[ -f $APP_DIR/agenttrade/backtest.py ]"
chk "agenttrade/strategy_modes.py"  "[ -f $APP_DIR/agenttrade/strategy_modes.py ]"
chk "cycle.py imports reconciliation" "grep -q run_reconciliation_gate $APP_DIR/cycle.py"
chk "SQLite init"                   "$PY -c 'from agenttrade.db import init_db; init_db()'"
chk "Flask /status"                 "curl -sf http://127.0.0.1:5111/status"
chk "Flask /ledger route"           "grep -q '/ledger' $APP_DIR/config_server.py"

if [[ "$LOCAL_ONLY" -eq 0 && -d "$WEB_DIR" ]]; then
    chk "dashboard index.html"      "[ -f $WEB_DIR/index.html ]"
    chk "dashboard reconciliation UI" "grep -q reconciliation $WEB_DIR/index.html"
fi

echo ""
[[ "$PASS" -eq "$TOTAL" ]] \
    && echo -e "${GREEN}${BOLD}  âœ“ All $TOTAL checks passed${NC}" \
    || echo -e "${YELLOW}${BOLD}  $PASS / $TOTAL checks passed${NC}"

# â”€â”€ Summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
echo ""
echo -e "${BOLD}â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•${NC}"
echo -e "${GREEN}${BOLD}  Update complete${NC}"
echo -e "${BOLD}â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•${NC}"
echo ""
echo -e "  ${CYAN}App:${NC}       $APP_DIR"
echo -e "  ${CYAN}SQLite:${NC}    $(grep '^AGENTTRADE_DB_PATH=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "$APP_DIR/agenttrade.sqlite3")"
echo -e "  ${CYAN}Backup:${NC}    $BACKUP_DIR"
echo ""
echo -e "  ${YELLOW}${BOLD}Useful commands:${NC}"
echo -e "  $PY -m agenttrade.migrate_state --dry-run"
echo -e "  $PY -m agenttrade.migrate_state"
echo -e "  $PY -m agenttrade.verify_ledger"
echo -e "  $PY -m agenttrade.backtest --start 2026-01-01 --end 2026-03-01"
echo -e "  $PY -m agenttrade.signal_report --days 30"
echo -e "  sudo bash $APP_DIR/run_cycle.sh"
echo -e "  tail -f $APP_DIR/cron.log"
if [[ "$LOCAL_ONLY" -eq 0 ]]; then
    echo -e "  sudo systemctl restart $SERVICE"
fi
echo ""
