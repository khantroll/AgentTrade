"""
FIFO Lot Ledger for AgentTrade.

Converts raw broker fills into:
  trade_lots          — open/closed buy positions with cost-basis
  realized_lot_matches — FIFO cost-basis matches for each sell fill
  completed_trades    — one record per sell event with definitive realized P&L

Design principles
-----------------
* All mutations run inside a single SQLite transaction per fill.
* Processing is idempotent — the same fill_id never produces duplicate rows.
* Realized P&L is calculated only from matched lots (long-only FIFO).
* Consecutive-loss logic uses completed_trades, not raw fills.
* No LLM involvement in any P&L calculation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from agenttrade import db as _db

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def process_fill_into_lot_ledger(fill_id: int) -> dict:
    """
    Process a single fill (by its fills.id) into the lot ledger.

    Buy fill  → creates a new OPEN trade_lot.
    Sell fill → FIFO-matches against oldest open lots, writes realized_lot_matches,
                reduces/closes matched lots, then calls rebuild_completed_trades for
                that fill so a completed_trade row is upserted.

    Returns a result dict:
        ok              bool
        action          'buy_lot_created' | 'sell_matched' | 'already_processed'
                        | 'skipped_no_fill' | 'skipped_zero_qty'
        fill_id         int
        lots_created    int
        matches_created int
        realized_pl     float   (sell side only)
        unmatched_qty   float   (> 0 signals a data anomaly)
        message         str
    """
    _db.init_db()
    fill = _db.get_fill_by_id(fill_id)
    if not fill:
        return _result(False, "skipped_no_fill", fill_id, message=f"Fill {fill_id} not found")

    side = str(fill.get("side") or "").lower()
    qty = float(fill.get("qty") or 0)
    price = float(fill.get("price") or 0)
    symbol = fill.get("symbol") or ""
    filled_at = fill.get("filled_at") or ""

    if qty <= 0 or price <= 0:
        return _result(False, "skipped_zero_qty", fill_id,
                       message=f"Fill {fill_id} has invalid qty={qty} price={price}")

    if _db.lot_already_processed_for_fill(fill_id, side):
        return _result(True, "already_processed", fill_id,
                       message=f"Fill {fill_id} already in lot ledger (idempotent skip)")

    if side == "buy":
        return _process_buy_fill(fill, fill_id, symbol, qty, price, filled_at)
    elif side == "sell":
        return _process_sell_fill(fill, fill_id, symbol, qty, price, filled_at)
    else:
        return _result(False, "skipped_unknown_side", fill_id,
                       message=f"Unknown side '{side}' for fill {fill_id}")


def rebuild_completed_trades(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """
    Aggregate realized_lot_matches into completed_trades.

    One completed_trade is created per unique close_fill_id (= one per sell event).
    Any existing completed_trade whose source_match_ids overlap is first deleted
    so the rebuild is idempotent.

    Returns summary dict: {'ok', 'created', 'skipped', 'errors', 'message'}
    """
    _db.init_db()
    conn_ctx = _db.get_connection()

    clauses: list[str] = []
    params: list = []
    if start_date:
        clauses.append("closed_at >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("closed_at <= ?")
        params.append(end_date)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with conn_ctx as conn:
        rows = conn.execute(
            f"""
            SELECT
                rlm.close_fill_id,
                rlm.symbol,
                rlm.asset_class,
                rlm.bucket,
                MIN(rlm.opened_at) AS opened_at,
                rlm.closed_at,
                SUM(rlm.qty)       AS qty,
                SUM(rlm.entry_price * rlm.qty) / SUM(rlm.qty) AS avg_entry,
                rlm.exit_price,
                SUM(rlm.realized_pl) AS realized_pl,
                rlm.close_reason,
                GROUP_CONCAT(rlm.id) AS match_ids
            FROM realized_lot_matches rlm
            {where}
            GROUP BY rlm.close_fill_id
            ORDER BY rlm.closed_at ASC
            """,
            params,
        ).fetchall()

    created = 0
    skipped = 0
    errors = 0

    for row in rows:
        try:
            match_ids_str = row["match_ids"] or ""
            total_pl = round(float(row["realized_pl"] or 0), 4)
            total_qty = round(float(row["qty"] or 0), 6)
            avg_entry = round(float(row["avg_entry"] or 0), 6)
            avg_exit = round(float(row["exit_price"] or 0), 6)
            realized_pl_pct = (
                round((avg_exit - avg_entry) / avg_entry * 100, 4)
                if avg_entry > 1e-9 else None
            )
            # Idempotency: remove stale completed_trade for this fill
            with _db.get_connection() as conn:
                conn.execute(
                    "DELETE FROM completed_trades WHERE source_match_ids=?",
                    (match_ids_str,),
                )
                conn.execute(
                    """
                    INSERT INTO completed_trades
                        (symbol, asset_class, bucket, opened_at, closed_at,
                         qty, avg_entry_price, avg_exit_price, realized_pl,
                         realized_pl_pct, close_reason, source_match_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["symbol"], row["asset_class"], row["bucket"],
                        row["opened_at"], row["closed_at"],
                        total_qty, avg_entry, avg_exit, total_pl,
                        realized_pl_pct, row["close_reason"], match_ids_str,
                    ),
                )
            created += 1
        except Exception as exc:
            log.error("[Ledger] rebuild_completed_trades error for fill %s: %s",
                      row["close_fill_id"], exc)
            errors += 1

    return {
        "ok": errors == 0,
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "message": f"Rebuilt {created} completed trades ({errors} errors)",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _process_buy_fill(fill: dict, fill_id: int, symbol: str,
                      qty: float, price: float, filled_at: str) -> dict:
    asset_class = None
    bucket: Optional[str] = None
    # Try to pull bucket from bucket_tags if available
    try:
        import agent_config as _cfg
        bucket = (_cfg.bucket_manager._load_tags() or {}).get(symbol)
    except Exception:
        pass

    try:
        lot_id = _db.insert_trade_lot(
            symbol=symbol,
            asset_class=asset_class,
            bucket=bucket,
            source_fill_id=fill_id,
            alpaca_order_id=fill.get("alpaca_order_id"),
            opened_at=filled_at,
            original_qty=qty,
            entry_price=price,
            raw_json=fill.get("raw_json"),
        )
        log.info("[Ledger] BUY lot created: %s x%.4f @ %.4f (lot_id=%d fill_id=%d)",
                 symbol, qty, price, lot_id, fill_id)
        return _result(True, "buy_lot_created", fill_id,
                       lots_created=1, message=f"Created lot {lot_id} for {symbol}")
    except Exception as exc:
        log.error("[Ledger] Failed to create lot for fill %d: %s", fill_id, exc)
        return _result(False, "error", fill_id, message=str(exc))


def _process_sell_fill(fill: dict, fill_id: int, symbol: str,
                       qty: float, price: float, filled_at: str) -> dict:
    """FIFO-match a sell fill against open lots. All ops in one transaction."""
    open_lots = _db.get_open_lots_for_symbol(symbol)

    if not open_lots:
        msg = f"No open lots for {symbol} — sell fill {fill_id} has no cost basis"
        log.error("[Ledger] %s", msg)
        _db.insert_risk_event(
            None, "critical", "LEDGER_UNMATCHED_SELL",
            f"{symbol}: sell fill {fill_id} qty={qty} — no open lots",
        )
        return _result(False, "unmatched_sell", fill_id,
                       unmatched_qty=qty, message=msg)

    remaining = qty
    matches_created = 0
    unmatched_qty = 0.0

    try:
        with _db.get_connection() as conn:
            conn.execute("BEGIN")
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                for lot in open_lots:
                    if remaining <= 1e-9:
                        break
                    lot_remaining = float(lot["remaining_qty"])
                    lot_id = lot["id"]
                    entry_price = float(lot["entry_price"])
                    opened_at = lot["opened_at"]
                    lot_bucket = lot.get("bucket")
                    lot_asset_class = lot.get("asset_class")

                    take = min(remaining, lot_remaining)
                    realized_pl = round((price - entry_price) * take, 4)
                    realized_pl_pct = round((price - entry_price) / entry_price * 100, 4) if entry_price > 1e-9 else None
                    new_remaining = round(lot_remaining - take, 9)
                    new_status = "CLOSED" if new_remaining <= 1e-9 else "OPEN"

                    # Insert lot match
                    conn.execute(
                        """
                        INSERT INTO realized_lot_matches
                            (symbol, asset_class, bucket, open_lot_id, close_fill_id,
                             opened_at, closed_at, qty, entry_price, exit_price,
                             realized_pl, realized_pl_pct, close_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (symbol, lot_asset_class, lot_bucket, lot_id, fill_id,
                         opened_at, filled_at, round(take, 9), entry_price, price,
                         realized_pl, realized_pl_pct, "sell"),
                    )
                    # Update lot
                    conn.execute(
                        "UPDATE trade_lots SET remaining_qty=?, status=? WHERE id=?",
                        (new_remaining, new_status, lot_id),
                    )
                    matches_created += 1
                    remaining = round(remaining - take, 9)
                    log.debug("[Ledger] Matched %.4f of lot %d (%s) → P/L $%.2f",
                              take, lot_id, symbol, realized_pl)

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            if remaining > 1e-9:
                unmatched_qty = remaining
                _db.insert_risk_event(
                    None, "critical", "LEDGER_UNMATCHED_SELL",
                    f"{symbol}: sell fill {fill_id} has {remaining:.4f} unmatched qty (no cost basis)",
                )
                log.error("[Ledger] Unmatched sell qty %.4f for %s fill %d",
                          remaining, symbol, fill_id)

    except Exception as exc:
        log.error("[Ledger] Error matching sell fill %d (%s): %s", fill_id, symbol, exc)
        return _result(False, "error", fill_id, message=str(exc))

    # Build completed_trade record for this sell event
    rebuild_completed_trades()

    total_pl = _get_total_pl_for_fill(fill_id)
    log.info("[Ledger] SELL matched: %s x%.4f @ %.4f → P/L $%.2f (%d matches, fill_id=%d)",
             symbol, qty, price, total_pl, matches_created, fill_id)

    return _result(
        ok=unmatched_qty <= 1e-9,
        action="sell_matched",
        fill_id=fill_id,
        matches_created=matches_created,
        realized_pl=total_pl,
        unmatched_qty=unmatched_qty,
        message=f"Matched {matches_created} lots for {symbol}, P/L ${total_pl:+.2f}"
                + (f", UNMATCHED {unmatched_qty:.4f}" if unmatched_qty > 1e-9 else ""),
    )


def _get_total_pl_for_fill(fill_id: int) -> float:
    with _db.get_connection() as conn:
        row = conn.execute(
            "SELECT SUM(realized_pl) as total FROM realized_lot_matches WHERE close_fill_id=?",
            (fill_id,),
        ).fetchone()
    return round(float(row["total"] or 0), 4) if row else 0.0


def _result(
    ok: bool,
    action: str,
    fill_id: int,
    *,
    lots_created: int = 0,
    matches_created: int = 0,
    realized_pl: float = 0.0,
    unmatched_qty: float = 0.0,
    message: str = "",
) -> dict:
    return {
        "ok": ok,
        "action": action,
        "fill_id": fill_id,
        "lots_created": lots_created,
        "matches_created": matches_created,
        "realized_pl": realized_pl,
        "unmatched_qty": unmatched_qty,
        "message": message,
    }
