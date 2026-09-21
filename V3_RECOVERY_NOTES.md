# AgentTrade Recovery v3.2 Notes

Date: 2026-08-27

## Restored original source

- `cycle.py` restored from the identical July 5/6 recovered copies (`cycle 5.py`, `cycle 6.py`, `cycle 8.py`), SHA-256 source family prefix `65c14f43c673`.
- `agents/research.py` restored from five byte-identical recovered copies, SHA-256 `f24c332059e6627ccc16309edcb3957939afa7a6079c7972a04b6c1ab2a9e632`.
- `analysis_funnel.py` restored verbatim from three independently supplied byte-identical copies, SHA-256 `7f84a607623a3504d0568fd372a0002f573dab38bb173dd55ebc2d66d66c37e7`. v3.2 adds machine-readable `skip_reason` / `blocked_reason` fields (`low_research_confidence`, `below_analysis_cutoff`) **in addition to** the historical rationale strings. Sort/cap/rationale text is unchanged.
- `agents/risk.py` was missing from `agents/` even though `cycle.py`, `agents/__init__.py`, `install.sh`, and `backtest_engine.py` import it. v3.2 first restored the older five-brake sizer from `tests/risk.py` (5327 bytes). A later recovered copy was then installed: SHA-256 `0201340ad66fcbf046457cd9e33a7e63fcf07b37a089b83b0da002d167f15392` (4142 bytes). That version is bucket guardrails only and defers executable sizing to `agenttrade/risk.py` (Tier 2). `tests/risk.py` remains the older compatibility copy. `agenttrade/risk.py` was not changed.

## Truth model

| Layer | Role |
|---|---|
| **Alpaca** | Broker truth — cash, positions, open orders, fills |
| **SQLite (`agenttrade.sqlite3`)** | Durable AgentTrade application truth — cycles, funnel, artifacts, flags, lots, risk events, buy-lock, manual stops |
| **`agent_state.json`** | Disposable dashboard projection/cache. Safe to delete and rebuild from SQLite + Alpaca. |

Secrets remain in `config.json` / `.env` only. They are never stored in SQLite.

## Recovery hardening (schema v4 + v3.2)

- Funnel rows are unique on `(cycle_run_id, stage, symbol, bucket)` and upserted so retries do not duplicate.
- Cycle persists `UNIVERSE → CANDIDATE → DECISION → RISK → ORDER`, plus `BLOCKED` for reconciliation failures.
- Pre-analysis SKIPs, risk rejections, buy-locks, pause/halt, execution failures, and Alpaca HTTP rejections emit machine-readable `blocked_reason` / `skip_reason` codes.
- Dashboard publishing prefers SQLite funnel + artifacts; empty live snapshots are filled from the ledger instead of calling Alpaca.
- Manual stop overrides persist in SQLite `system_flags`.
- Buy-lock continuity persists in SQLite `BUY_LOCK_STATE` (JSON file is fallback only).
- Startup daily-trade count prefers SQLite fills.
- Authoritative JSON reads in attribution, margin correction, health check, config-server `/state` `/status` `/config/mode`, and position-review stops now prefer SQLite/Alpaca.
- Alpaca open-order sync stores broker-synced rows with `cycle_run_id` NULL (not `0`) so `PRAGMA foreign_keys=ON` does not reject the insert. Visibility is by open status, not cycle id.

## Remaining legacy JSON / file dependencies (intentional)

| File | Role | Authoritative? |
|---|---|---|
| `bucket_tags.json` | Symbol → bucket ownership | **Yes** (not migrated; semantics unchanged) |
| `config.json` / `.env` | API keys and runtime config | **Yes** (must not move to SQLite) |
| `agent_state.json` | Dashboard projection cache | No — generated; JSON reads are labeled non-authoritative fallbacks |
| `performance_history.jsonl` | Cycle chart history | Auxiliary log; seed prefers SQLite snapshot |
| `trade_log.jsonl` | Fill/meta log | Auxiliary; SQLite `fills` / lots exist |
| `token_usage.json`, `llm_health.json` | LLM router local counters | Process-local, not trading truth |
| screener cache files | Universe cache | Cache only |

Compatibility JSON fallbacks were not deleted.

## Production-path tests

- `tests/conftest.py` — temp SQLite via `AGENTTRADE_DB_PATH`; session hash-guard on recovered `agenttrade.sqlite3`
- `tests/test_smoke_imports.py` — import `agent.py`, `cycle.py`, every `agents.*` module with missing third-party packages stubbed
- `tests/test_production_paths.py` — reconciliation pass/fail, stale pause clear, Alpaca open-order sync, protective stops, manual stops, buy-lock, deterministic risk rejection, LLM sizing ignored, execution failure, pause block, dashboard rebuild with JSON removed, funnel replay UNIVERSE→ORDER and UNIVERSE→BLOCKED, funnel idempotency
- `tests/test_import_audit.py` — static local-import closure including `agents.risk`

Tests never open the recovered archive fixture for writes.

## Validation (2026-08-27)

Host `/usr/bin/python3` is the macOS Xcode stub. Validation used standalone CPython 3.12.12 (no live Alpaca, no LLM, no mutation of recovered SQLite).

```
python3 -m compileall -q .     # exit 0
python3 -m pytest -q           # 46 passed
```

Local import audit: 75 first-party `.py` files; `agents/risk.py` present. Third-party/optional imports observed: `anthropic`, `dotenv`, `flask`, `flask_cors`, `openai`, `pytest`, `requests`, `schedule`, `vaderSentiment`, `yfinance`.

Recovered `agenttrade.sqlite3` SHA-256 unchanged:

`c2841e3c33719229f643746130b4f64f10c564626aa56d1e607bcd97bb39c1b6`

## Changed files (v3.2)

- `agents/risk.py` (later recovered 4142-byte two-tier gate; SHA-256 `0201340ad66f…`)
- `agenttrade/db.py`
- `agenttrade/publish.py`
- `agenttrade/risk.py`
- `account_sync.py`
- `agent_config.py`
- `analysis_funnel.py`
- `buy_lock.py`
- `config_server.py`
- `cycle.py`
- `health_check.py`
- `margin_correction.py`
- `performance_history.py`
- `pnl_attribution.py`
- `agents/analysis.py`
- `agents/execution.py`
- `agents/position_review.py`
- `tests/conftest.py`
- `tests/test_smoke_imports.py`
- `tests/test_production_paths.py`
- `tests/test_import_audit.py`
- `README.md`
- `V3_RECOVERY_NOTES.md`

## Ambiguities left untouched

- `agents/risk.py` still globally returns no approvals when `buy_lock.active` is true (any scoped lock sets `active`). Cycle also tags per-symbol `symbol_lock:*` reasons. Changing that would alter buy-lock aggression vs the recovered later gate; it was restored as-is.
- `performance_history.jsonl` and `trade_log.jsonl` were not converted into SQLite-only stores.
- `period_summary.py` and `report_export.py` remain reconstructions from v3 (not byte-for-byte originals).
- `alpaca_client.py` still has a UTF-8 BOM; it compiles, and the import audit reads with `utf-8-sig`. Not stripped in this pass to avoid an unrelated source rewrite.
