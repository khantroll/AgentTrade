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
# In-place update (source and /opt/trading-agent are the same directory):
#   cd /opt/trading-agent && sudo bash update_deploy.sh -y
# Same-file copies are skipped. SQLite migration is NOT part of this command.
# A first-time JSON import is opt-in: --migrate
#
# Deploy from a separate source tree onto the live app:
#   sudo bash /path/to/source/update_deploy.sh -y --app-dir /opt/trading-agent
#   sudo bash update_deploy.sh -y --source /path/to/source --app-dir /opt/trading-agent
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
RUN_MIGRATE=0
SKIP_VERIFY=0
SKIP_RESTART=0
SKIP_TESTS=0
SKIP_PIP=0
DRY_RUN=0
DRY_RUN_MIGRATE_ONLY=0
SOURCE_DIR=""

usage() {
    cat << 'EOF'
Usage: bash update_deploy.sh [OPTIONS]

Options:
  -y, --yes           Accept defaults; skip most prompts
  --dry-run           Print the plan and write nothing
  --local-only        Update files in APP_DIR only (no web/systemd; for dev)
  --source PATH       Directory of files to deploy (default: this script's directory)
  --app-dir PATH      Target app directory (default: /opt/trading-agent)
  --web-dir PATH      Dashboard web root (default: /var/www/my_webapp__3/www)
  --service NAME      systemd service (default: trading-agent-config)
  --migrate           Opt in to a first-time JSON import. Existing ledgers are preserved.
  --skip-migrate      Do not run SQLite migration (this is already the default)
  --skip-verify       Do not run verify_ledger after deploy
  --skip-restart      Do not restart config server
  --skip-tests        Do not run pytest (if tests/ present)
  --skip-pip          Do not create a venv or run pip
  --migrate-dry-run   Print the migration plan only; do not import
  -h, --help          Show this help

The plain command is: sudo bash update_deploy.sh -y
It copies code onto the app dir, skips same-file copies, and does not import
agent_state.json into an existing SQLite ledger. Runtime files (.env, sqlite,
llm_health.json, agent_state.json, logs, trade logs) are never overwritten.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --local-only) LOCAL_ONLY=1 ;;
        --source) SOURCE_DIR="$2"; shift ;;
        --app-dir) DEFAULT_APP="$2"; shift ;;
        --web-dir) DEFAULT_WEB="$2"; shift ;;
        --service) DEFAULT_SERVICE="$2"; shift ;;
        --migrate) RUN_MIGRATE=1; DRY_RUN_MIGRATE_ONLY=0 ;;
        --skip-migrate) RUN_MIGRATE=0; DRY_RUN_MIGRATE_ONLY=0 ;;
        --skip-verify) SKIP_VERIFY=1 ;;
        --skip-restart) SKIP_RESTART=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        --skip-pip) SKIP_PIP=1 ;;
        --migrate-dry-run) DRY_RUN_MIGRATE_ONLY=1; RUN_MIGRATE=0 ;;
        -h|--help) usage; exit 0 ;;
        *) error "Unknown option: $1 (use -h for help)" ;;
    esac
    shift
done

if [[ -n "$SOURCE_DIR" ]]; then
    [[ -d "$SOURCE_DIR" ]] || error "Source dir not found: $SOURCE_DIR"
    SRC="$(cd "$SOURCE_DIR" && pwd)"
fi

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
    update_deploy.sh verify_ledger_deploy.sh write_deploy_sha.sh
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
[[ "$MISSING" -gt 0 ]] && error "$MISSING required file(s) missing from $SRC — sync repo first"

if [[ "$DRY_RUN" -eq 1 ]]; then
    section "Dry run"
    info "Source: $SRC"
    info "App:    $APP_DIR"
    if [[ "$IN_PLACE" -eq 1 ]]; then
        info "In-place: source and app dir are the same; file copies will be skipped"
    else
        info "Would copy code files from the source tree into the app dir"
    fi
    info "Runtime files are never overwritten: .env, agenttrade.sqlite3, llm_health.json, agent_state.json, token_usage.json, config.json, bucket_tags.json, trade_log.jsonl, performance_history.jsonl, *.log"
    if [[ "$RUN_MIGRATE" -eq 1 ]]; then
        info "Would run a first-time SQLite import. An existing ledger is preserved."
    elif [[ "$DRY_RUN_MIGRATE_ONLY" -eq 1 ]]; then
        info "Would print the migration plan only"
    else
        info "SQLite migration: not run. Pass --migrate only for a first-time JSON import."
    fi
    info "No files were written."
    exit 0
fi

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

if [[ "$SKIP_PIP" -eq 1 ]]; then
    info "Skipped venv/pip (--skip-pip)"
elif [[ ! -d "$VENV" ]]; then
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

# Live host state. A source tree must never replace these, even in-place.
is_runtime_rel() {
    local base
    base="$(basename "$1")"
    case "$base" in
        .env|agent_state.json|llm_health.json|token_usage.json|config.json|bucket_tags.json|trade_log.jsonl|performance_history.jsonl|trading_agent.log|cron.log)
            return 0
            ;;
        *.log|*.jsonl|*.sqlite3|*.sqlite3-*)
            return 0
            ;;
    esac
    return 1
}

copy_file() {
    local rel="$1"
    local src="$SRC/$rel"
    local dest="$APP_DIR/$rel"
    if is_runtime_rel "$rel"; then
        info "  · $rel left untouched (runtime state)"
        return 0
    fi
    if [[ ! -f "$src" ]]; then
        warn "  missing in source: $rel"
        return 0
    fi
    if [[ -e "$dest" ]]; then
        local src_real dest_real
        src_real="$(readlink -f "$src")"
        dest_real="$(readlink -f "$dest")"
        if [[ "$src_real" == "$dest_real" ]]; then
            info "  · $rel unchanged (source and destination are the same file)"
            return 0
        fi
    fi
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    info "  ✓ $rel"
}

for f in "${UPDATE_PY[@]}"; do copy_file "$f"; done
for f in "${UPDATE_AGENTS[@]}"; do copy_file "$f"; done
for f in "${UPDATE_AGENTTRADE[@]}"; do copy_file "$f"; done
for f in "${UPDATE_SCRIPTS[@]}"; do
    [[ -f "$SRC/$f" ]] && copy_file "$f" && chmod +x "$APP_DIR/$f" 2>/dev/null || true
done

if [[ -f "$SRC/requirements.txt" ]]; then
    copy_file "requirements.txt"
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
        success "js/at-*.js → $WEB_DIR/js/"
    fi
fi

success "Files deployed"

# One commit, both filenames. Do not hand-edit only one of them.
if [[ -f "$APP_DIR/write_deploy_sha.sh" ]]; then
    if bash "$APP_DIR/write_deploy_sha.sh" "$APP_DIR" "${DEPLOY_SHA:-}" "$SRC"; then
        success "DEPLOY_SHA.txt and DEPLOY_SHA both set to $(awk 'NF { print $1; exit }' "$APP_DIR/DEPLOY_SHA.txt")"
    else
        warn "Could not write DEPLOY_SHA.txt and DEPLOY_SHA together. Re-run: bash write_deploy_sha.sh $APP_DIR <sha>"
    fi
fi
if [[ "$LOCAL_ONLY" -eq 0 && -d "$WEB_DIR" && -f "$APP_DIR/DEPLOY_SHA.txt" ]]; then
    cp "$APP_DIR/DEPLOY_SHA.txt" "$WEB_DIR/DEPLOY_SHA.txt"
    cp "$APP_DIR/DEPLOY_SHA.txt" "$WEB_DIR/DEPLOY_SHA"
fi

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
if [[ "$SKIP_PIP" -eq 1 ]]; then
    info "Skipped pip install (--skip-pip)"
elif [[ -f "$APP_DIR/requirements.txt" ]]; then
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

if [[ "$RUN_MIGRATE" -eq 0 && "$DRY_RUN_MIGRATE_ONLY" -eq 0 ]]; then
    info "SQLite migration not run. An existing ledger is left as-is. Opt in with --migrate for a first-time JSON import."
else
    info "Migration dry-run:"
    "$PY" -m agenttrade.migrate_state --dry-run || warn "Dry-run reported issues"
    echo ""

    if [[ "$DRY_RUN_MIGRATE_ONLY" -eq 1 ]]; then
        info "Stopping after dry-run (--migrate-dry-run)"
    elif [[ "$RUN_MIGRATE" -eq 1 ]] && confirm "Run first-time migration? Existing ledgers are preserved." "n"; then
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
    chk "dashboard HTML" "grep -q 'Pre-Cycle Health' $WEB_DIR/index.html"
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
