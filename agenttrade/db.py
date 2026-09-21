"""SQLite ledger — authoritative trading state for AgentTrade."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4

_DB_INITIALIZED = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_run_id INTEGER,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL,
    account_id TEXT,
    equity REAL,
    cash REAL,
    buying_power REAL,
    portfolio_value REAL,
    long_market_value REAL,
    short_market_value REAL,
    margin_used REAL DEFAULT 0,
    raw_json TEXT,
    FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_run_id INTEGER,
    captured_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_entry_price REAL,
    market_price REAL,
    market_value REAL,
    unrealized_pl REAL,
    unrealized_plpc REAL,
    side TEXT DEFAULT 'long',
    source TEXT NOT NULL,
    raw_json TEXT,
    FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_run_id INTEGER,
    alpaca_order_id TEXT UNIQUE,
    submitted_at TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    notional REAL,
    order_type TEXT,
    time_in_force TEXT,
    limit_price REAL,
    stop_price REAL,
    status TEXT,
    strategy_name TEXT,
    raw_json TEXT,
    FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    alpaca_order_id TEXT,
    alpaca_fill_id TEXT UNIQUE,
    filled_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    commission REAL DEFAULT 0,
    raw_json TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS strategy_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_run_id INTEGER,
    created_at TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal TEXT NOT NULL,
    confidence REAL,
    score REAL,
    reason TEXT,
    raw_json TEXT,
    FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
);

CREATE TABLE IF NOT EXISTS sentiment_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_run_id INTEGER,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL,
    confidence REAL,
    summary TEXT,
    raw_json TEXT,
    FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_run_id INTEGER,
    created_at TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_type TEXT NOT NULL,
    symbol TEXT,
    message TEXT NOT NULL,
    raw_json TEXT,
    FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_run_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0,
    differences_json TEXT,
    message TEXT,
    FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
);

CREATE TABLE IF NOT EXISTS system_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db_path() -> str:
    import agent_config as cfg

    override = os.getenv("AGENTTRADE_DB_PATH")
    if override:
        return override
    return os.path.join(cfg.APP_DIR, "agenttrade.sqlite3")


def db_available() -> bool:
    try:
        path = get_db_path()
        if not os.path.isfile(path):
            return _DB_INITIALIZED
        return True
    except Exception:
        return False


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    path = get_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create schema if missing. Fail-safe: raises on error."""
    global _DB_INITIALIZED
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
        _apply_migrations(conn)
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = int(row["v"] or 0) if row else 0
        if current < SCHEMA_VERSION:
            conn.execute(
                "INSERT OR REPLACE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
    _DB_INITIALIZED = True
    log.info("[DB] Initialized SQLite ledger at %s (WAL)", get_db_path())


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Non-destructive schema upgrades."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(account_snapshots)").fetchall()}
    if "multiplier" not in cols:
        conn.execute("ALTER TABLE account_snapshots ADD COLUMN multiplier REAL")

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS signal_attributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_run_id INTEGER,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        total_score REAL,
        components_json TEXT NOT NULL,
        pipelines_json TEXT,
        raw_json TEXT,
        FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
    );

    CREATE TABLE IF NOT EXISTS reddit_mentions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_run_id INTEGER,
        captured_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        subreddit TEXT NOT NULL,
        post_id TEXT,
        upvotes INTEGER,
        comment_count INTEGER,
        post_age_hours REAL,
        author_karma REAL,
        vader_compound REAL,
        quality_score REAL,
        engagement_velocity REAL,
        raw_json TEXT,
        FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
    );

    CREATE TABLE IF NOT EXISTS reddit_sentiment_trends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        period_days INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        avg_sentiment REAL,
        mention_count INTEGER,
        quality_avg REAL,
        trend_class TEXT,
        raw_json TEXT
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_reddit_trends_sym_period
        ON reddit_sentiment_trends(symbol, period_days, captured_at);

    CREATE TABLE IF NOT EXISTS signal_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_run_id INTEGER,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        decision TEXT,
        price REAL,
        raw_confidence REAL,
        calibrated_confidence REAL,
        agreement_score REAL,
        total_score REAL,
        components_json TEXT,
        explainability_json TEXT,
        raw_json TEXT,
        FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
    );

    CREATE TABLE IF NOT EXISTS signal_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_snapshot_id INTEGER NOT NULL,
        horizon_days INTEGER NOT NULL,
        evaluated_at TEXT NOT NULL,
        price_at_signal REAL,
        price_at_eval REAL,
        price_change_pct REAL,
        max_gain_pct REAL,
        max_drawdown_pct REAL,
        raw_json TEXT,
        FOREIGN KEY (signal_snapshot_id) REFERENCES signal_snapshots(id)
    );

    CREATE INDEX IF NOT EXISTS idx_signal_snapshots_symbol ON signal_snapshots(symbol);
    CREATE INDEX IF NOT EXISTS idx_signal_outcomes_snapshot ON signal_outcomes(signal_snapshot_id);

    CREATE TABLE IF NOT EXISTS backtest_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        strategy_name TEXT,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        initial_cash REAL NOT NULL,
        final_equity REAL,
        total_return_pct REAL,
        max_drawdown_pct REAL,
        win_rate REAL,
        profit_factor REAL,
        sharpe_like REAL,
        notes TEXT,
        raw_json TEXT
    );

    CREATE TABLE IF NOT EXISTS backtest_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backtest_run_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        entry_date TEXT NOT NULL,
        exit_date TEXT,
        entry_price REAL NOT NULL,
        exit_price REAL,
        qty REAL NOT NULL,
        stop_price REAL,
        take_profit_price REAL,
        pnl REAL,
        pnl_pct REAL,
        exit_reason TEXT,
        signal_score REAL,
        reddit_score REAL,
        confidence REAL,
        raw_json TEXT,
        FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(id)
    );

    CREATE TABLE IF NOT EXISTS hypothetical_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_run_id INTEGER,
        created_at TEXT NOT NULL,
        strategy_name TEXT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty REAL,
        notional REAL,
        price REAL,
        stop_price REAL,
        take_profit_price REAL,
        mode TEXT NOT NULL,
        raw_json TEXT,
        FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
    );

    CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(backtest_run_id);
    CREATE INDEX IF NOT EXISTS idx_signal_snapshots_created ON signal_snapshots(created_at);

    CREATE TABLE IF NOT EXISTS trade_lots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        asset_class TEXT,
        bucket TEXT,
        source_fill_id INTEGER,
        alpaca_order_id TEXT,
        opened_at TEXT NOT NULL,
        side TEXT NOT NULL DEFAULT 'buy',
        original_qty REAL NOT NULL,
        remaining_qty REAL NOT NULL,
        entry_price REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        raw_json TEXT,
        FOREIGN KEY (source_fill_id) REFERENCES fills(id)
    );

    CREATE TABLE IF NOT EXISTS realized_lot_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        asset_class TEXT,
        bucket TEXT,
        open_lot_id INTEGER NOT NULL,
        close_fill_id INTEGER NOT NULL,
        opened_at TEXT NOT NULL,
        closed_at TEXT NOT NULL,
        qty REAL NOT NULL,
        entry_price REAL NOT NULL,
        exit_price REAL NOT NULL,
        realized_pl REAL NOT NULL,
        realized_pl_pct REAL,
        close_reason TEXT,
        raw_json TEXT,
        FOREIGN KEY (open_lot_id) REFERENCES trade_lots(id),
        FOREIGN KEY (close_fill_id) REFERENCES fills(id)
    );

    CREATE TABLE IF NOT EXISTS completed_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        asset_class TEXT,
        bucket TEXT,
        opened_at TEXT NOT NULL,
        closed_at TEXT NOT NULL,
        qty REAL NOT NULL,
        avg_entry_price REAL NOT NULL,
        avg_exit_price REAL NOT NULL,
        realized_pl REAL NOT NULL,
        realized_pl_pct REAL,
        close_reason TEXT,
        source_match_ids TEXT,
        raw_json TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_trade_lots_symbol_status
        ON trade_lots(symbol, status);
    CREATE INDEX IF NOT EXISTS idx_realized_lot_matches_closed_at
        ON realized_lot_matches(closed_at);
    CREATE INDEX IF NOT EXISTS idx_completed_trades_closed_at
        ON completed_trades(closed_at);

    CREATE TABLE IF NOT EXISTS funnel_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_run_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        stage TEXT NOT NULL,
        symbol TEXT,
        bucket TEXT,
        status TEXT,
        reason TEXT,
        raw_json TEXT,
        FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id)
    );

    CREATE INDEX IF NOT EXISTS idx_funnel_events_cycle_stage
        ON funnel_events(cycle_run_id, stage, id);
    CREATE INDEX IF NOT EXISTS idx_funnel_events_symbol
        ON funnel_events(symbol, cycle_run_id);
    """)
    _migrate_funnel_idempotency(conn)
    conn.executescript("""

    CREATE TABLE IF NOT EXISTS cycle_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_run_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        artifact_key TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY (cycle_run_id) REFERENCES cycle_runs(id),
        UNIQUE(cycle_run_id, artifact_key)
    );
    CREATE INDEX IF NOT EXISTS idx_realized_lot_matches_fill
        ON realized_lot_matches(close_fill_id);
    """)


def _migrate_funnel_idempotency(conn: sqlite3.Connection) -> None:
    """Coerce NULL symbol/bucket to '' and unique-index funnel rows per cycle/stage."""
    try:
        conn.execute("UPDATE funnel_events SET symbol='' WHERE symbol IS NULL")
        conn.execute("UPDATE funnel_events SET bucket='' WHERE bucket IS NULL")
        conn.execute(
            """
            DELETE FROM funnel_events
            WHERE id NOT IN (
                SELECT MAX(id) FROM funnel_events
                GROUP BY cycle_run_id, stage, IFNULL(symbol, ''), IFNULL(bucket, '')
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_funnel_events_idempotent
                ON funnel_events(cycle_run_id, stage, symbol, bucket)
            """
        )
    except sqlite3.Error as e:
        log.warning("[DB] funnel idempotency migration skipped: %s", e)


def _ensure_db() -> None:
    if not _DB_INITIALIZED:
        init_db()


# ── Cycle runs ───────────────────────────────────────────────────────────────

def start_cycle_run(mode: str) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO cycle_runs(started_at, status, mode) VALUES (?, ?, ?)",
            (utc_now(), "running", mode),
        )
        return int(cur.lastrowid)


def finish_cycle_run(cycle_run_id: int, status: str, notes: str = "") -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE cycle_runs SET finished_at=?, status=?, notes=? WHERE id=?",
            (utc_now(), status, notes, cycle_run_id),
        )


# ── System flags ─────────────────────────────────────────────────────────────

def get_system_flag(key: str, default: Optional[str] = None) -> Optional[str]:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM system_flags WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_system_flag(key: str, value: str) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO system_flags(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(value), utc_now()),
        )


def get_json_flag(key: str, default: Any = None) -> Any:
    raw = get_system_flag(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def set_json_flag(key: str, value: Any) -> None:
    set_system_flag(key, json.dumps(value))


_BUY_LOCK_KEYS = (
    "last_sell_at",
    "last_sell_symbols",
    "symbol_locks",
    "daily_sells_today",
    "daily_sells_date",
    "daily_sells_placed_today",
    "daily_sells_placed_date",
    "emergency_rebalance_at",
    "processed_fill_ids",
)


def get_buy_lock_state() -> dict:
    """Durable buy-lock tracking (SQLite system_flags)."""
    value = get_json_flag("BUY_LOCK_STATE", {})
    return value if isinstance(value, dict) else {}


def set_buy_lock_state(ctx: dict) -> None:
    payload = {}
    src = ctx or {}
    for key in _BUY_LOCK_KEYS:
        if key in src:
            payload[key] = src[key]
    set_json_flag("BUY_LOCK_STATE", payload)


def get_manual_stop_prices() -> dict:
    value = get_json_flag("MANUAL_STOP_PRICES", {})
    return value if isinstance(value, dict) else {}


def set_manual_stop_price(symbol: str, stop_price: float) -> dict:
    stops = get_manual_stop_prices()
    stops[str(symbol).upper()] = round(float(stop_price), 4)
    set_json_flag("MANUAL_STOP_PRICES", stops)
    return stops


def trading_halted() -> bool:
    return get_system_flag("TRADING_HALTED", "false").lower() in ("1", "true", "yes")


def trading_paused() -> bool:
    return get_system_flag("TRADING_PAUSED", "false").lower() in ("1", "true", "yes")


def get_pause_reason() -> Optional[str]:
    return get_system_flag("PAUSE_REASON")


def get_halt_reason() -> Optional[str]:
    return get_system_flag("HALT_REASON")


def increment_ignored_llm_sizing() -> int:
    """Bump counter for dashboard (Tier 2)."""
    current = int(get_system_flag("IGNORED_LLM_SIZING_COUNT", "0") or "0")
    new_val = current + 1
    set_system_flag("IGNORED_LLM_SIZING_COUNT", str(new_val))
    return new_val


def get_ignored_llm_sizing_count() -> int:
    try:
        return int(get_system_flag("IGNORED_LLM_SIZING_COUNT", "0") or "0")
    except ValueError:
        return 0


# ── Snapshots ─────────────────────────────────────────────────────────────────

def insert_account_snapshot(cycle_run_id: int, source: str, account: dict) -> int:
    _ensure_db()
    captured = utc_now()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO account_snapshots(
                cycle_run_id, captured_at, source, account_id, equity, cash, buying_power,
                portfolio_value, long_market_value, short_market_value, margin_used, multiplier, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                captured,
                source,
                account.get("id"),
                _f(account.get("equity") or account.get("portfolio_value")),
                _f(account.get("cash")),
                _f(account.get("buying_power")),
                _f(account.get("portfolio_value") or account.get("equity")),
                _f(account.get("long_market_value")),
                _f(account.get("short_market_value")),
                _f(account.get("initial_margin") or account.get("maintenance_margin")),
                _f(account.get("multiplier")) or 1.0,
                json.dumps(account),
            ),
        )
        return int(cur.lastrowid)


def insert_positions_snapshot(cycle_run_id: int, source: str, positions: list) -> int:
    """Alias for insert_positions (Tier 1 API)."""
    return insert_positions(cycle_run_id, source, positions)


def insert_order(cycle_run_id: int, order: dict, strategy_name: str = "") -> None:
    """Record a submitted order."""
    record_submitted_order(cycle_run_id, order, strategy_name=strategy_name)


def insert_fill(cycle_run_id: int, fill: dict) -> None:
    """Insert a single fill row."""
    insert_fills_from_alpaca(cycle_run_id, [fill])


def insert_positions(cycle_run_id: int, source: str, positions: list) -> int:
    _ensure_db()
    captured = utc_now()
    count = 0
    with get_connection() as conn:
        for p in positions or []:
            qty = _f(p.get("qty"))
            side = "short" if qty < 0 else "long"
            conn.execute(
                """
                INSERT INTO positions(
                    cycle_run_id, captured_at, symbol, qty, avg_entry_price, market_price,
                    market_value, unrealized_pl, unrealized_plpc, side, source, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_run_id,
                    captured,
                    p.get("symbol"),
                    qty,
                    _f(p.get("avg_entry_price")),
                    _f(p.get("current_price") or p.get("market_price")),
                    _f(p.get("market_value")),
                    _f(p.get("unrealized_pl")),
                    _f(p.get("unrealized_plpc")),
                    side,
                    source,
                    json.dumps(p),
                ),
            )
            count += 1
    return count


def insert_orders(cycle_run_id: int, open_orders: list, source: str = "alpaca") -> int:
    _ensure_db()
    count = 0
    with get_connection() as conn:
        for o in open_orders or []:
            oid = o.get("id") or o.get("order_id")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO orders(
                        cycle_run_id, alpaca_order_id, submitted_at, symbol, side, qty, notional,
                        order_type, time_in_force, limit_price, stop_price, status, strategy_name, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cycle_run_id,
                        oid,
                        o.get("submitted_at") or o.get("created_at"),
                        o.get("symbol"),
                        o.get("side"),
                        _f(o.get("qty")),
                        _f(o.get("notional")),
                        o.get("type") or o.get("order_type"),
                        o.get("time_in_force"),
                        _f(o.get("limit_price")),
                        _f(o.get("stop_price")),
                        o.get("status"),
                        o.get("strategy_name"),
                        json.dumps(o),
                    ),
                )
                count += 1
            except sqlite3.IntegrityError:
                pass
    return count


def record_submitted_order(cycle_run_id: int, order: dict, strategy_name: str = "") -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO orders(
                cycle_run_id, alpaca_order_id, submitted_at, symbol, side, qty, notional,
                order_type, time_in_force, limit_price, stop_price, status, strategy_name, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                order.get("order_id") or order.get("id"),
                utc_now(),
                order.get("ticker") or order.get("symbol"),
                order.get("side", "buy"),
                _f(order.get("shares") or order.get("qty")),
                _f(order.get("notional_usd") or order.get("notional")),
                order.get("type"),
                order.get("time_in_force"),
                _f(order.get("limit_price")),
                _f(order.get("stop_price")),
                order.get("status", "placed"),
                strategy_name or order.get("strategy_name", ""),
                json.dumps(order),
            ),
        )


def insert_fills_from_alpaca(cycle_run_id: int, fills: list) -> int:
    _ensure_db()
    added = 0
    with get_connection() as conn:
        for f in fills or []:
            fill_id = f.get("id") or f.get("order_id")
            if not fill_id:
                continue
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO fills(
                        alpaca_order_id, alpaca_fill_id, filled_at, symbol, side, qty, price, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f.get("order_id"),
                        fill_id,
                        f.get("filled_at") or f.get("submitted_at") or f.get("transaction_time") or utc_now(),
                        f.get("ticker") or f.get("symbol"),
                        f.get("side"),
                        _f(f.get("shares") or f.get("qty")),
                        _f(f.get("price")),
                        json.dumps(f),
                    ),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass
    return added


def insert_strategy_signal(cycle_run_id: int, row: dict) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO strategy_signals(
                cycle_run_id, created_at, strategy_name, symbol, signal, confidence, score, reason, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                utc_now(),
                row.get("strategy_name", "unknown"),
                row.get("symbol") or row.get("ticker"),
                row.get("signal", "HOLD"),
                row.get("confidence"),
                row.get("score"),
                row.get("reason") or row.get("rationale"),
                json.dumps(row),
            ),
        )


def insert_sentiment_score(cycle_run_id: int, row: dict) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sentiment_scores(
                cycle_run_id, created_at, symbol, source, score, confidence, summary, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                utc_now(),
                row.get("symbol") or row.get("ticker"),
                row.get("source", "reddit"),
                row.get("score"),
                row.get("confidence"),
                row.get("summary"),
                json.dumps(row),
            ),
        )


def insert_risk_event(cycle_run_id: Optional[int], severity: str, event_type: str, message: str,
                      symbol: str = "", raw: Optional[dict] = None) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO risk_events(cycle_run_id, created_at, severity, event_type, symbol, message, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cycle_run_id, utc_now(), severity, event_type, symbol, message, json.dumps(raw or {})),
        )


def insert_reconciliation_run(cycle_run_id: int, started_at: str, finished_at: str,
                              status: str, passed: bool, differences: dict, message: str) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO reconciliation_runs(
                cycle_run_id, started_at, finished_at, status, passed, differences_json, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cycle_run_id, started_at, finished_at, status, 1 if passed else 0,
             json.dumps(differences), message),
        )
        return int(cur.lastrowid)


# ── Queries ───────────────────────────────────────────────────────────────────

def get_latest_account_snapshot() -> Optional[dict]:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_latest_positions() -> list:
    _ensure_db()
    with get_connection() as conn:
        snap = conn.execute("SELECT cycle_run_id FROM account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if not snap:
            return []
        rows = conn.execute(
            "SELECT * FROM positions WHERE cycle_run_id=? ORDER BY symbol",
            (snap["cycle_run_id"],),
        ).fetchall()
        return [dict(r) for r in rows]


_OPEN_ORDER_STATUSES = (
    "open", "new", "accepted", "pending_new", "partially_filled", "placed",
)


def get_latest_open_orders() -> list:
    """Currently-open orders, including Alpaca-synced rows (cycle_run_id NULL)."""
    _ensure_db()
    placeholders = ",".join("?" * len(_OPEN_ORDER_STATUSES))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM orders
            WHERE LOWER(COALESCE(status, '')) IN ({placeholders})
            ORDER BY id DESC
            """,
            _OPEN_ORDER_STATUSES,
        ).fetchall()
    seen: set = set()
    out = []
    for r in rows:
        d = dict(r)
        oid = d.get("alpaca_order_id") or f"row:{d.get('id')}"
        if oid in seen:
            continue
        seen.add(oid)
        out.append(d)
    out.sort(key=lambda x: (str(x.get("symbol") or ""), str(x.get("side") or "")))
    return out


def sync_open_orders_from_alpaca(open_orders: list) -> dict:
    """
    Upsert Alpaca open orders into the `orders` table so broker-side stop
    and limit orders are always visible in the local ledger.

    Uses INSERT OR REPLACE so existing rows (same alpaca_order_id) get
    their status / stop_price / qty refreshed from the live Alpaca data.

    Returns a summary dict with 'upserted' and 'skipped' counts.
    """
    _ensure_db()
    upserted = 0
    skipped = 0
    # cycle_run_id NULL = broker-synced (not a trading cycle). Do not use 0:
    # foreign_keys=ON and cycle_runs.id starts at 1.

    with get_connection() as conn:
        for o in open_orders or []:
            oid = o.get("id") or o.get("order_id") or o.get("alpaca_order_id")
            if not oid:
                skipped += 1
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO orders(
                        cycle_run_id, alpaca_order_id, submitted_at, symbol, side,
                        qty, notional, order_type, time_in_force,
                        limit_price, stop_price, status, strategy_name, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(alpaca_order_id) DO UPDATE SET
                        status     = excluded.status,
                        stop_price = excluded.stop_price,
                        qty        = excluded.qty,
                        raw_json   = excluded.raw_json
                    """,
                    (
                        None,
                        str(oid),
                        o.get("submitted_at") or o.get("created_at"),
                        o.get("symbol"),
                        o.get("side"),
                        _f(o.get("qty")),
                        _f(o.get("notional")),
                        o.get("type") or o.get("order_type"),
                        o.get("time_in_force"),
                        _f(o.get("limit_price")),
                        _f(o.get("stop_price")),
                        o.get("status"),
                        o.get("strategy_name"),
                        json.dumps(o),
                    ),
                )
                upserted += 1
            except Exception as _ex:
                log.debug("[DB] sync_open_orders_from_alpaca: skipped %s — %s", oid, _ex)
                skipped += 1

    return {"upserted": upserted, "skipped": skipped}


def extract_stop_prices_from_orders(open_orders: list) -> dict:
    """
    Build a {symbol: stop_price} map from a list of open Alpaca orders.
    Only stop-type sell orders (stop, stop_limit, trailing_stop) contribute.
    When multiple stop orders exist for the same symbol, the highest
    (most protective) stop price is retained.
    """
    stop_prices: dict[str, float] = {}
    stop_types = {"stop", "stop_limit", "trailing_stop"}
    for o in open_orders or []:
        side = str(o.get("side") or "").lower()
        order_type = str(o.get("type") or o.get("order_type") or "").lower()
        if side != "sell" or order_type not in stop_types:
            continue
        sym = o.get("symbol") or ""
        sp = _f(o.get("stop_price") or o.get("trail_price") or o.get("hwm"))
        if sym and sp and sp > 0:
            if sym not in stop_prices or sp > stop_prices[sym]:
                stop_prices[sym] = round(sp, 4)
    return stop_prices


def get_latest_reconciliation() -> Optional[dict]:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["differences"] = json.loads(out.get("differences_json") or "{}")
        except json.JSONDecodeError:
            out["differences"] = {}
        return out


def get_recent_risk_events(limit: int = 20) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_strategy_signals(limit: int = 50) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_sentiment_scores(limit: int = 50) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM sentiment_scores ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_cycle_run() -> Optional[dict]:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM cycle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def get_closed_trade_outcomes(limit: int = 50) -> list:
    """Backward-compatible alias for closed trade list."""
    return get_realized_closed_trades(limit=limit)


def get_realized_closed_trades(limit: int = 50) -> list:
    """FIFO match buy/sell fills from SQLite; newest closed trades first."""
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, side, qty, price, filled_at
            FROM fills
            WHERE qty IS NOT NULL AND price IS NOT NULL
            ORDER BY filled_at ASC
            """,
        ).fetchall()

    from collections import deque

    lots: dict[str, deque] = {}
    closed: list[dict] = []

    for r in rows:
        sym = r["symbol"]
        if not sym:
            continue
        side = str(r["side"] or "").lower()
        qty = _f(r["qty"]) or 0.0
        price = _f(r["price"]) or 0.0
        if qty <= 0 or price <= 0:
            continue

        if side == "buy":
            lots.setdefault(sym, deque()).append({"qty": qty, "price": price, "time": r["filled_at"]})
        elif side == "sell":
            remaining = qty
            while remaining > 1e-9 and lots.get(sym):
                lot = lots[sym][0]
                take = min(remaining, lot["qty"])
                pl = (price - lot["price"]) * take
                closed.append({
                    "symbol": sym,
                    "qty": round(take, 6),
                    "buy_price": lot["price"],
                    "sell_price": price,
                    "realized_pl": round(pl, 2),
                    "sell_time": r["filled_at"],
                    "buy_time": lot.get("time"),
                })
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    lots[sym].popleft()

    closed.sort(key=lambda x: x.get("sell_time") or "", reverse=True)
    return closed[:limit]


# ── Tier 3: Signal intelligence ───────────────────────────────────────────────

def insert_signal_attribution(cycle_run_id: int, row: dict) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_attributions(
                cycle_run_id, created_at, symbol, total_score, components_json, pipelines_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                utc_now(),
                row.get("symbol"),
                _f(row.get("total_score")),
                json.dumps(row.get("components") or {}),
                json.dumps(row.get("pipelines") or {}),
                json.dumps(row),
            ),
        )
        return int(cur.lastrowid)


def insert_reddit_mention(cycle_run_id: int, row: dict) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO reddit_mentions(
                cycle_run_id, captured_at, symbol, subreddit, post_id, upvotes, comment_count,
                post_age_hours, author_karma, vader_compound, quality_score, engagement_velocity, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                utc_now(),
                row.get("symbol"),
                row.get("subreddit"),
                row.get("post_id"),
                row.get("upvotes"),
                row.get("comment_count"),
                _f(row.get("post_age_hours")),
                _f(row.get("author_karma")),
                _f(row.get("vader_compound")),
                _f(row.get("quality_score")),
                _f(row.get("engagement_velocity")),
                json.dumps(row),
            ),
        )


def upsert_reddit_sentiment_trend(symbol: str, period_days: int, row: dict) -> None:
    _ensure_db()
    captured = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO reddit_sentiment_trends(
                symbol, period_days, captured_at, avg_sentiment, mention_count,
                quality_avg, trend_class, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                period_days,
                captured,
                _f(row.get("avg_sentiment")),
                int(row.get("mention_count") or 0),
                _f(row.get("quality_avg")),
                row.get("trend_class"),
                json.dumps(row),
            ),
        )


def insert_signal_snapshot(cycle_run_id: int, row: dict) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_snapshots(
                cycle_run_id, created_at, symbol, signal_type, decision, price,
                raw_confidence, calibrated_confidence, agreement_score, total_score,
                components_json, explainability_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                utc_now(),
                row.get("symbol"),
                row.get("signal_type", "CANDIDATE"),
                row.get("decision"),
                _f(row.get("price")),
                _f(row.get("raw_confidence")),
                _f(row.get("calibrated_confidence")),
                _f(row.get("agreement_score")),
                _f(row.get("total_score")),
                json.dumps(row.get("components") or {}),
                json.dumps(row.get("explainability") or {}),
                json.dumps(row),
            ),
        )
        return int(cur.lastrowid)


def insert_signal_outcome(signal_snapshot_id: int, row: dict) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO signal_outcomes(
                signal_snapshot_id, horizon_days, evaluated_at, price_at_signal, price_at_eval,
                price_change_pct, max_gain_pct, max_drawdown_pct, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_snapshot_id,
                int(row.get("horizon_days") or 0),
                row.get("evaluated_at") or utc_now(),
                _f(row.get("price_at_signal")),
                _f(row.get("price_at_eval")),
                _f(row.get("price_change_pct")),
                _f(row.get("max_gain_pct")),
                _f(row.get("max_drawdown_pct")),
                json.dumps(row),
            ),
        )


def get_recent_signal_attributions(limit: int = 50) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM signal_attributions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["components"] = json.loads(d.pop("components_json") or "{}")
        except json.JSONDecodeError:
            d["components"] = {}
        out.append(d)
    return out


def get_signal_attribution_for_symbol(symbol: str, limit: int = 5) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM signal_attributions
            WHERE symbol=? ORDER BY id DESC LIMIT ?
            """,
            (symbol.upper(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_reddit_trends(symbol: str = "", limit: int = 100) -> list:
    _ensure_db()
    with get_connection() as conn:
        if symbol:
            rows = conn.execute(
                """
                SELECT * FROM reddit_sentiment_trends
                WHERE symbol=? ORDER BY captured_at DESC LIMIT ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reddit_sentiment_trends ORDER BY captured_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_signal_snapshots(limit: int = 200, pending_outcomes: bool = False) -> list:
    _ensure_db()
    with get_connection() as conn:
        if pending_outcomes:
            rows = conn.execute(
                """
                SELECT s.* FROM signal_snapshots s
                LEFT JOIN signal_outcomes o ON o.signal_snapshot_id = s.id AND o.horizon_days = 7
                WHERE o.id IS NULL AND s.price IS NOT NULL AND s.price > 0
                ORDER BY s.id ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signal_snapshots ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["components"] = json.loads(d.pop("components_json") or "{}")
        except json.JSONDecodeError:
            d["components"] = {}
        try:
            d["explainability"] = json.loads(d.pop("explainability_json") or "{}")
        except json.JSONDecodeError:
            d["explainability"] = {}
        out.append(d)
    return out


def get_signal_outcomes_for_scorecard(limit: int = 500) -> list:
    """Join snapshots + 7d outcomes for scorecard computation."""
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.symbol, s.components_json, s.calibrated_confidence, s.agreement_score,
                   o.horizon_days, o.price_change_pct, o.max_gain_pct, o.max_drawdown_pct
            FROM signal_outcomes o
            JOIN signal_snapshots s ON s.id = o.signal_snapshot_id
            ORDER BY o.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_source_accuracy_stats() -> dict:
    """Historical win rate by component source from evaluated outcomes."""
    rows = get_signal_outcomes_for_scorecard(limit=1000)
    stats: dict[str, dict] = {}
    for r in rows:
        try:
            components = json.loads(r.get("components_json") or "{}")
        except json.JSONDecodeError:
            components = {}
        pct = _f(r.get("price_change_pct")) or 0.0
        profitable = pct > 0
        for source, weight in components.items():
            if not weight:
                continue
            bucket = stats.setdefault(source, {"count": 0, "wins": 0, "return_sum": 0.0})
            bucket["count"] += 1
            bucket["wins"] += 1 if profitable else 0
            bucket["return_sum"] += pct
    result = {}
    for source, b in stats.items():
        n = b["count"] or 1
        result[source] = {
            "count": b["count"],
            "win_rate_pct": round(100 * b["wins"] / n, 1),
            "avg_return_pct": round(b["return_sum"] / n, 2),
        }
    return result


# ── Tier 4: Backtest & validation ─────────────────────────────────────────────

def start_backtest_run(
    strategy_name: str | None,
    start_date: str,
    end_date: str,
    initial_cash: float,
) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO backtest_runs(
                started_at, strategy_name, start_date, end_date, initial_cash
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (utc_now(), strategy_name, start_date, end_date, initial_cash),
        )
        return int(cur.lastrowid)


def finish_backtest_run(run_id: int, metrics: dict) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE backtest_runs SET
                finished_at=?, final_equity=?, total_return_pct=?, max_drawdown_pct=?,
                win_rate=?, profit_factor=?, sharpe_like=?, notes=?, raw_json=?
            WHERE id=?
            """,
            (
                utc_now(),
                _f(metrics.get("final_equity")),
                _f(metrics.get("total_return_pct")),
                _f(metrics.get("max_drawdown_pct")),
                _f(metrics.get("win_rate")),
                _f(metrics.get("profit_factor")),
                _f(metrics.get("sharpe_like")),
                metrics.get("notes"),
                json.dumps(metrics),
                run_id,
            ),
        )


def insert_backtest_trade(run_id: int, trade: dict) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO backtest_trades(
                backtest_run_id, symbol, entry_date, exit_date, entry_price, exit_price, qty,
                stop_price, take_profit_price, pnl, pnl_pct, exit_reason,
                signal_score, reddit_score, confidence, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                trade.get("symbol"),
                trade.get("entry_date"),
                trade.get("exit_date"),
                _f(trade.get("entry_price")),
                _f(trade.get("exit_price")),
                _f(trade.get("qty")),
                _f(trade.get("stop_price")),
                _f(trade.get("take_profit_price")),
                _f(trade.get("pnl")),
                _f(trade.get("pnl_pct")),
                trade.get("exit_reason"),
                _f(trade.get("signal_score")),
                _f(trade.get("reddit_score")),
                _f(trade.get("confidence")),
                json.dumps(trade),
            ),
        )
        return int(cur.lastrowid)


def get_latest_backtest_run() -> Optional[dict]:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def get_backtest_trades(run_id: int) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE backtest_run_id=? ORDER BY entry_date",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_signal_snapshots_in_range(
    start_date: str,
    end_date: str,
    strategy_name: str | None = None,
    decision: str | None = "BUY",
) -> list:
    _ensure_db()
    start_ts = start_date[:10] + "T00:00:00+00:00"
    end_ts = end_date[:10] + "T23:59:59+00:00"
    with get_connection() as conn:
        q = """
            SELECT s.*, c.mode AS cycle_mode
            FROM signal_snapshots s
            LEFT JOIN cycle_runs c ON c.id = s.cycle_run_id
            WHERE s.created_at >= ? AND s.created_at <= ?
        """
        params: list = [start_ts, end_ts]
        if decision:
            q += " AND UPPER(COALESCE(s.decision, '')) = ?"
            params.append(decision.upper())
        q += " ORDER BY s.created_at ASC"
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["components"] = json.loads(d.pop("components_json") or "{}")
        except (json.JSONDecodeError, KeyError):
            d["components"] = {}
        out.append(d)
    return out


def get_cycle_run(cycle_run_id: int) -> Optional[dict]:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM cycle_runs WHERE id=?", (cycle_run_id,)).fetchone()
        return dict(row) if row else None


def get_cycle_signals(cycle_run_id: int) -> dict:
    """All Tier 3/4 data for one cycle (replay)."""
    _ensure_db()
    with get_connection() as conn:
        strategies = conn.execute(
            "SELECT * FROM strategy_signals WHERE cycle_run_id=? ORDER BY id",
            (cycle_run_id,),
        ).fetchall()
        sentiments = conn.execute(
            "SELECT * FROM sentiment_scores WHERE cycle_run_id=? ORDER BY id",
            (cycle_run_id,),
        ).fetchall()
        attributions = conn.execute(
            "SELECT * FROM signal_attributions WHERE cycle_run_id=? ORDER BY id",
            (cycle_run_id,),
        ).fetchall()
        snapshots = conn.execute(
            "SELECT * FROM signal_snapshots WHERE cycle_run_id=? ORDER BY id",
            (cycle_run_id,),
        ).fetchall()
        recons = conn.execute(
            "SELECT * FROM reconciliation_runs WHERE cycle_run_id=? ORDER BY id DESC LIMIT 1",
            (cycle_run_id,),
        ).fetchone()
        risks = conn.execute(
            "SELECT * FROM risk_events WHERE cycle_run_id=? ORDER BY id",
            (cycle_run_id,),
        ).fetchall()
    return {
        "strategy_signals": [dict(r) for r in strategies],
        "sentiment_scores": [dict(r) for r in sentiments],
        "signal_attributions": [dict(r) for r in attributions],
        "signal_snapshots": [dict(r) for r in snapshots],
        "reconciliation": dict(recons) if recons else None,
        "risk_events": [dict(r) for r in risks],
    }


def insert_funnel_event(
    cycle_run_id: int,
    stage: str,
    *,
    symbol: str = "",
    bucket: str = "",
    status: str = "",
    reason: str = "",
    payload: Optional[dict] = None,
) -> int:
    """Persist one decision-funnel event. Idempotent per cycle/stage/symbol/bucket."""
    _ensure_db()
    sym = str(symbol or "").upper()
    bkt = str(bucket or "")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO funnel_events(
                cycle_run_id, created_at, stage, symbol, bucket, status, reason, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cycle_run_id, stage, symbol, bucket) DO UPDATE SET
                created_at=excluded.created_at,
                status=excluded.status,
                reason=excluded.reason,
                raw_json=excluded.raw_json
            """,
            (
                cycle_run_id, utc_now(), str(stage).upper(),
                sym, bkt,
                status or "", reason or "", json.dumps(payload or {}),
            ),
        )
        return int(cur.lastrowid or 0)


def insert_funnel_events(cycle_run_id: int, stage: str, rows: list, *, bucket: str = "") -> int:
    count = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        insert_funnel_event(
            cycle_run_id, stage,
            symbol=row.get("ticker") or row.get("symbol") or "",
            bucket=row.get("bucket") or bucket,
            status=str(row.get("status") or row.get("action") or ""),
            reason=str(
                row.get("blocked_reason")
                or row.get("skip_reason")
                or row.get("reason")
                or row.get("rationale")
                or row.get("error")
                or ""
            ),
            payload=row,
        )
        count += 1
    return count


def upsert_cycle_artifact(cycle_run_id: int, artifact_key: str, payload: Any) -> None:
    """Persist cycle-level JSON that should survive cache-file loss."""
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO cycle_artifacts(cycle_run_id, created_at, artifact_key, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cycle_run_id, artifact_key) DO UPDATE SET
                created_at=excluded.created_at, payload_json=excluded.payload_json
            """,
            (cycle_run_id, utc_now(), artifact_key, json.dumps(payload)),
        )


def get_cycle_artifacts(cycle_run_id: int) -> dict:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT artifact_key, payload_json FROM cycle_artifacts WHERE cycle_run_id=?",
            (cycle_run_id,),
        ).fetchall()
    out = {}
    for row in rows:
        try:
            out[row["artifact_key"]] = json.loads(row["payload_json"] or "null")
        except (TypeError, json.JSONDecodeError):
            out[row["artifact_key"]] = None
    return out


def get_cycle_funnel(cycle_run_id: int) -> dict:
    """Return persisted funnel rows grouped into dashboard-compatible stages."""
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM funnel_events WHERE cycle_run_id=? ORDER BY id",
            (cycle_run_id,),
        ).fetchall()
    grouped = {
        "universe": [], "candidates": [], "decisions": [],
        "risk": [], "orders": [], "blocked": [],
    }
    stage_map = {
        "UNIVERSE": "universe", "CANDIDATE": "candidates", "DECISION": "decisions",
        "RISK": "risk", "ORDER": "orders", "BLOCKED": "blocked",
    }
    for row in rows:
        d = dict(row)
        try:
            payload = json.loads(d.get("raw_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        payload.setdefault("ticker", d.get("symbol"))
        payload.setdefault("bucket", d.get("bucket") or None)
        if d.get("status") and not payload.get("status"):
            payload["status"] = d.get("status")
        if d.get("reason"):
            payload.setdefault("blocked_reason", d.get("reason"))
            payload.setdefault("skip_reason", d.get("reason"))
            payload.setdefault("reason", d.get("reason"))
        stage = str(d.get("stage") or "").upper()
        key = stage_map.get(stage)
        if key:
            grouped[key].append(payload)
        if stage == "RISK" and str(payload.get("risk_status") or payload.get("status") or "").upper() == "BLOCKED":
            grouped["blocked"].append(payload)
        elif stage == "ORDER" and str(payload.get("status") or "").lower() in ("blocked", "failed"):
            grouped["blocked"].append(payload)
        elif stage == "DECISION" and str(payload.get("action") or payload.get("status") or "").upper() == "SKIP":
            grouped["blocked"].append(payload)
    return grouped


def get_latest_funnel() -> dict:
    """Return funnel + cycle artifacts for the most recent cycle that has them."""
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT cycle_run_id FROM funnel_events ORDER BY cycle_run_id DESC, id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"cycle_run_id": None, "funnel": {}, "artifacts": {}}
    cid = int(row["cycle_run_id"])
    return {"cycle_run_id": cid, "funnel": get_cycle_funnel(cid), "artifacts": get_cycle_artifacts(cid)}


def positions_as_dashboard(rows: Optional[list] = None) -> list:
    """Normalize SQLite position rows to the dashboard/Alpaca-like shape."""
    out = []
    for p in rows or get_latest_positions():
        if not isinstance(p, dict):
            continue
        qty = _f(p.get("qty")) or 0.0
        price = _f(p.get("current_price") or p.get("market_price"))
        mv = _f(p.get("market_value"))
        if mv is None and price is not None:
            mv = qty * price
        row = dict(p)
        row["symbol"] = p.get("symbol") or p.get("ticker") or ""
        row["ticker"] = row["symbol"]
        row["qty"] = qty
        row["avg_entry_price"] = _f(p.get("avg_entry_price"))
        row["current_price"] = price
        row["market_price"] = price
        row["market_value"] = mv
        row["unrealized_pl"] = _f(p.get("unrealized_pl"))
        row["unrealized_plpc"] = _f(p.get("unrealized_plpc"))
        out.append(row)
    return out


def account_row_as_snapshot(row: Optional[dict] = None) -> dict:
    """Map a SQLite account_snapshots row to the Alpaca-like account dict."""
    row = row or get_latest_account_snapshot() or {}
    if not row:
        return {}
    return {
        "id": row.get("account_id"),
        "equity": row.get("equity"),
        "cash": row.get("cash"),
        "buying_power": row.get("buying_power"),
        "portfolio_value": row.get("portfolio_value") or row.get("equity"),
        "long_market_value": row.get("long_market_value"),
        "short_market_value": row.get("short_market_value"),
        "multiplier": row.get("multiplier") if row.get("multiplier") is not None else 1.0,
    }


def _bucket_tags_from_file() -> dict:
    try:
        from buckets import BucketManager
        return BucketManager()._load_tags() or {}
    except Exception:
        return {}


def _attribution_map(rows: list) -> dict:
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or row.get("ticker") or "").upper()
        if not sym or sym in out:
            continue
        comps = row.get("components")
        if comps is None:
            try:
                comps = json.loads(row.get("components_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                comps = {}
        out[sym] = {
            "symbol": sym,
            "total_score": row.get("total_score"),
            "components": comps or {},
            "pipelines": row.get("pipelines"),
        }
    return out


def load_application_state() -> dict:
    """
    SQLite-first application state for attribution, health, and dashboard rebuild.

    bucket_tags.json remains the bucket-ownership source. agent_state.json is not
    consulted here — callers may layer a non-authoritative JSON fallback.
    """
    _ensure_db()
    ledger_data = get_dashboard_ledger()
    funnel_pack = get_latest_funnel()
    artifacts = funnel_pack.get("artifacts") or {}
    funnel = funnel_pack.get("funnel") or {}
    positions = positions_as_dashboard(ledger_data.get("positions") or [])
    acct_row = ledger_data.get("account_snapshot") or {}
    tags = _bucket_tags_from_file()
    if tags:
        for pos in positions:
            if not pos.get("bucket"):
                pos["bucket"] = tags.get(pos.get("symbol"))
    attr_rows = ledger_data.get("signal_attributions") or []
    attr_map = _attribution_map(attr_rows)
    open_orders = ledger_data.get("open_orders") or get_latest_open_orders()
    broker_stops = extract_stop_prices_from_orders(open_orders)
    manual_stops = get_manual_stop_prices()
    cycle = ledger_data.get("cycle_run") or get_latest_cycle_run() or {}
    return {
        "ledger_source": "sqlite",
        "broker_source": "alpaca",
        "state_cache_role": "projection_only",
        "positions": positions,
        "open_positions": positions,
        "open_orders": open_orders,
        "portfolio_value": acct_row.get("portfolio_value") or acct_row.get("equity"),
        "equity": acct_row.get("equity"),
        "cash": acct_row.get("cash"),
        "buying_power": acct_row.get("buying_power"),
        "bucket_tags": tags,
        "token_usage": artifacts.get("token_usage") or {},
        "universes": artifacts.get("universes") or {},
        "screener_sources": artifacts.get("screener_sources") or {},
        "rebalance": artifacts.get("rebalance") or {},
        "hard_rebalance": artifacts.get("hard_rebalance") or {},
        "buy_lock": artifacts.get("buy_lock") or {},
        "llm_mode": artifacts.get("llm_mode") or cycle.get("mode"),
        "last_run": artifacts.get("last_run") or cycle.get("finished_at") or cycle.get("started_at"),
        "trade_candidates": funnel.get("candidates") or [],
        "decisions": funnel.get("decisions") or [],
        "last_orders": funnel.get("orders") or [],
        "blocked_ideas": funnel.get("blocked") or [],
        "signal_attributions": attr_rows,
        "position_signal_breakdown": {
            sym: {"total_score": row.get("total_score"), "components": row.get("components")}
            for sym, row in attr_map.items()
        },
        "stop_prices": {**broker_stops, **manual_stops},
        "funnel_cycle_run_id": funnel_pack.get("cycle_run_id"),
        "funnel_source": "sqlite" if funnel_pack.get("cycle_run_id") else None,
        "daily_trades": count_fills_today(),
    }


def load_json_state_fallback() -> dict:
    """NON-AUTHORITATIVE: dashboard projection cache only."""
    try:
        import agent_config as cfg
        path = cfg.STATE_FILE
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def insert_hypothetical_trade(cycle_run_id: int, trade: dict) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO hypothetical_trades(
                cycle_run_id, created_at, strategy_name, symbol, side, qty, notional,
                price, stop_price, take_profit_price, mode, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_run_id,
                utc_now(),
                trade.get("strategy_name"),
                trade.get("symbol"),
                trade.get("side", "buy"),
                _f(trade.get("qty")),
                _f(trade.get("notional")),
                _f(trade.get("price")),
                _f(trade.get("stop_price")),
                _f(trade.get("take_profit_price")),
                trade.get("mode", "shadow_mode"),
                json.dumps(trade),
            ),
        )


def get_hypothetical_trades(limit: int = 50) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM hypothetical_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_safety_block_counts(days: int = 30) -> dict:
    """Count risk events by block type for dashboard."""
    _ensure_db()
    cutoff = utc_now()[:10]  # simplified; filter in Python for portability
    from datetime import datetime, timedelta, timezone
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT event_type, COUNT(*) AS cnt
            FROM risk_events
            WHERE created_at >= ?
            GROUP BY event_type
            """,
            (cut,),
        ).fetchall()
        recon_fail = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM reconciliation_runs
            WHERE passed=0 AND started_at >= ?
            """,
            (cut,),
        ).fetchone()
    counts = {r["event_type"]: r["cnt"] for r in rows}
    return {
        "reconciliation_failed": int(recon_fail["cnt"]) if recon_fail else 0,
        "order_rejected_no_margin": counts.get("ORDER_REJECTED_NO_MARGIN", 0),
        "reconciliation_failed_events": counts.get("RECONCILIATION_FAILED", 0),
        "consecutive_loss_pause": counts.get("CONSECUTIVE_LOSS_PAUSE", 0),
        "position_size_rejected": counts.get("POSITION_SIZE_REJECTED", 0),
        "trading_halted": 1 if trading_halted() else 0,
        "trading_paused": 1 if trading_paused() else 0,
        "by_event_type": counts,
    }


def get_cycle_runs_by_mode(mode: str, limit: int = 100) -> list:
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM cycle_runs WHERE mode=? ORDER BY id DESC LIMIT ?",
            (mode, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_dashboard_ledger() -> dict:
    """Dashboard payload from SQLite + flags."""
    acct = get_latest_account_snapshot()
    recon = get_latest_reconciliation()
    cycle = get_latest_cycle_run()
    positions = get_latest_positions()
    open_orders = get_latest_open_orders()
    cl_status = get_consecutive_loss_status(limit=20)
    return {
        "ledger_source": "sqlite",
        "broker_source": "alpaca",
        "account_snapshot": acct,
        "positions": positions,
        "open_orders": open_orders,
        "reconciliation": recon,
        "cycle_run": cycle,
        "trading_halted": trading_halted(),
        "trading_paused": trading_paused(),
        "halt_reason": get_halt_reason(),
        "pause_reason": get_pause_reason(),
        "consecutive_losses": cl_status.get("count", 0),
        "consecutive_loss_status": cl_status,
        "ledger_complete": cl_status.get("ledger_complete", False),
        "position_sizing_source": "deterministic",
        "risk_per_trade_pct": _risk_per_trade_pct(),
        "ignored_llm_sizing_count": get_ignored_llm_sizing_count(),
        "risk_events": get_recent_risk_events(10),
        "strategy_signals": get_recent_strategy_signals(20),
        "sentiment_scores": get_recent_sentiment_scores(20),
        "signal_attributions": get_recent_signal_attributions(30),
        "reddit_trends": get_reddit_trends(limit=50),
        "signal_snapshots": get_signal_snapshots(limit=30),
        "source_accuracy_stats": get_source_accuracy_stats(),
        "latest_backtest": get_latest_backtest_run(),
        "safety_blocks": get_safety_block_counts(30),
        "hypothetical_trades": get_hypothetical_trades(20),
    }


def _risk_per_trade_pct() -> float:
    try:
        import os
        return float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.005"))
    except ValueError:
        return 0.005


def get_consecutive_loss_status(limit: int = 20) -> dict:
    """
    Authoritative consecutive-loss status for pause enforcement and dashboard.

    Uses completed_trades only. When the ledger has no completed trades,
    returns ledger_complete=False — callers must not fall back to fills.
    """
    calculated_at = utc_now()
    try:
        ledger_complete = bool(get_completed_trades(limit=1))
    except Exception:
        ledger_complete = False

    if not ledger_complete:
        return {
            "count": 0,
            "trades": [],
            "latest_winner": None,
            "calculated_at": calculated_at,
            "ledger_complete": False,
            "source": "ledger_incomplete",
            "message": "ledger incomplete — run python -m agenttrade.rebuild_ledger",
        }

    detail = calculate_consecutive_losses(limit=limit)
    detail["ledger_complete"] = True
    detail["source"] = "completed_trades"
    detail["message"] = ""
    return detail


def _count_consecutive_losses_quick() -> int:
    """Return consecutive loss count from completed_trades only."""
    return get_consecutive_loss_status(limit=20).get("count", 0)


def count_fills_today() -> int:
    """Count distinct fills recorded in the SQLite fills table for today (UTC)."""
    _ensure_db()
    today = __import__("datetime").date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE filled_at >= ?",
            (today,),
        ).fetchone()
    return int(row[0]) if row else 0


def _f(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_wal_mode() -> bool:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return bool(row and str(row[0]).lower() == "wal")


# ─────────────────────────────────────────────────────────────────────────────
# Lot Ledger — CRUD helpers (used by agenttrade/ledger.py)
# ─────────────────────────────────────────────────────────────────────────────

def get_fill_by_id(fill_id: int) -> Optional[dict]:
    _ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM fills WHERE id=?", (fill_id,)).fetchone()
        return dict(row) if row else None


def get_open_lots_for_symbol(symbol: str) -> list:
    """Return OPEN buy lots for symbol ordered oldest-first (FIFO)."""
    _ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trade_lots
            WHERE symbol=? AND status='OPEN' AND side='buy' AND remaining_qty > 1e-9
            ORDER BY opened_at ASC, id ASC
            """,
            (symbol,),
        ).fetchall()
        return [dict(r) for r in rows]


def lot_already_processed_for_fill(fill_id: int, side: str) -> bool:
    """Idempotency guard — returns True if fill was already processed."""
    _ensure_db()
    with get_connection() as conn:
        if side == "buy":
            row = conn.execute(
                "SELECT 1 FROM trade_lots WHERE source_fill_id=? LIMIT 1", (fill_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM realized_lot_matches WHERE close_fill_id=? LIMIT 1", (fill_id,)
            ).fetchone()
        return row is not None


def insert_trade_lot(
    symbol: str,
    asset_class: Optional[str],
    bucket: Optional[str],
    source_fill_id: int,
    alpaca_order_id: Optional[str],
    opened_at: str,
    original_qty: float,
    entry_price: float,
    raw_json: Optional[str] = None,
) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_lots
                (symbol, asset_class, bucket, source_fill_id, alpaca_order_id,
                 opened_at, side, original_qty, remaining_qty, entry_price, status, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, 'buy', ?, ?, ?, 'OPEN', ?)
            """,
            (symbol, asset_class, bucket, source_fill_id, alpaca_order_id,
             opened_at, original_qty, original_qty, entry_price, raw_json),
        )
        return cur.lastrowid


def update_trade_lot(lot_id: int, remaining_qty: float, status: str) -> None:
    _ensure_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE trade_lots SET remaining_qty=?, status=? WHERE id=?",
            (remaining_qty, status, lot_id),
        )


def insert_realized_lot_match(
    symbol: str,
    asset_class: Optional[str],
    bucket: Optional[str],
    open_lot_id: int,
    close_fill_id: int,
    opened_at: str,
    closed_at: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    realized_pl: float,
    realized_pl_pct: Optional[float],
    close_reason: Optional[str] = None,
    raw_json: Optional[str] = None,
) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO realized_lot_matches
                (symbol, asset_class, bucket, open_lot_id, close_fill_id,
                 opened_at, closed_at, qty, entry_price, exit_price,
                 realized_pl, realized_pl_pct, close_reason, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, asset_class, bucket, open_lot_id, close_fill_id,
             opened_at, closed_at, qty, entry_price, exit_price,
             realized_pl, realized_pl_pct, close_reason, raw_json),
        )
        return cur.lastrowid


def insert_completed_trade(
    symbol: str,
    asset_class: Optional[str],
    bucket: Optional[str],
    opened_at: str,
    closed_at: str,
    qty: float,
    avg_entry_price: float,
    avg_exit_price: float,
    realized_pl: float,
    realized_pl_pct: Optional[float],
    close_reason: Optional[str],
    source_match_ids: Optional[str],
    raw_json: Optional[str] = None,
) -> int:
    _ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO completed_trades
                (symbol, asset_class, bucket, opened_at, closed_at,
                 qty, avg_entry_price, avg_exit_price, realized_pl, realized_pl_pct,
                 close_reason, source_match_ids, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, asset_class, bucket, opened_at, closed_at,
             qty, avg_entry_price, avg_exit_price, realized_pl, realized_pl_pct,
             close_reason, source_match_ids, raw_json),
        )
        return cur.lastrowid


def delete_completed_trades_for_fill(close_fill_id: int) -> None:
    """Remove completed_trades rows derived from a specific fill (for rebuild)."""
    _ensure_db()
    # Find match IDs for this fill
    with get_connection() as conn:
        match_ids = [
            str(r[0]) for r in conn.execute(
                "SELECT id FROM realized_lot_matches WHERE close_fill_id=?",
                (close_fill_id,),
            ).fetchall()
        ]
        if not match_ids:
            return
        # completed_trades stores source_match_ids as comma-separated string
        for mid in match_ids:
            conn.execute(
                "DELETE FROM completed_trades WHERE source_match_ids LIKE ?",
                (f"%{mid}%",),
            )


def get_completed_trades(
    limit: int = 100,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    order: str = "desc",
) -> list:
    _ensure_db()
    direction = "DESC" if order.lower() != "asc" else "ASC"
    params: list = []
    clauses: list[str] = []
    if start_date:
        clauses.append("closed_at >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("closed_at <= ?")
        params.append(end_date)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM completed_trades {where} ORDER BY closed_at {direction} LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def calculate_consecutive_losses(limit: int = 20) -> dict:
    """
    Deterministic consecutive loss streak from completed_trades.

    Rules:
    - Trades ordered closed_at DESC (newest first).
    - Multiple completed trades for the same symbol within a 60-second window
      are treated as a single aggregated event (handles multi-fill exits).
    - Count runs of negative realized_pl; stop at first non-negative.
    - Unrealized / open positions are never counted.
    """
    from datetime import datetime as _dt

    trades = get_completed_trades(limit=max(limit * 4, 40), order="desc")
    calculated_at = utc_now()

    if not trades:
        return {"count": 0, "trades": [], "latest_winner": None, "calculated_at": calculated_at}

    # --- Collapse same-symbol fills within 60-second window ---
    def _parse_ts(ts_str: str) -> _dt:
        try:
            s = (ts_str or "").replace("Z", "+00:00")
            return _dt.fromisoformat(s)
        except Exception:
            return _dt.min

    events: list[dict] = []
    for t in trades:
        t_ts = _parse_ts(t.get("closed_at", ""))
        sym = t.get("symbol", "")
        merged = False
        for ev in reversed(events):
            ev_ts = _parse_ts(ev.get("closed_at", ""))
            if ev.get("symbol") == sym and abs((t_ts - ev_ts).total_seconds()) <= 60:
                ev["realized_pl"] = round(ev["realized_pl"] + (t.get("realized_pl") or 0), 4)
                ev["qty"] = round(ev["qty"] + (t.get("qty") or 0), 6)
                ev["_source_ids"] = ev.get("_source_ids", []) + [t.get("id")]
                merged = True
                break
        if not merged:
            events.append({**t, "_source_ids": [t.get("id")]})

    # --- Count consecutive losing events newest-first ---
    count = 0
    streak_trades: list[dict] = []
    latest_winner: Optional[dict] = None
    for ev in events:
        pl = _f(ev.get("realized_pl")) or 0.0
        if pl < -0.01:
            count += 1
            streak_trades.append(ev)
            if count >= limit:
                break
        else:
            latest_winner = ev
            break

    return {
        "count": count,
        "trades": streak_trades,
        "latest_winner": latest_winner,
        "calculated_at": calculated_at,
    }
