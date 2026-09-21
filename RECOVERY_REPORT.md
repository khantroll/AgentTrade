# AgentTrade Recovery Report

Recovery sources:
- `update-20260626-133940.zip` — June 26 checkpoint
- `Archive.zip` — later live `/opt/trading-agent` tree (files dated through July 6, 2026)

## Recovery decision

The canonical tree is based on the later live archive. It preserves later working changes and restores `agenttrade/performance.py` from the June 26 checkpoint because the live tree still imports that module but the file was absent.

Disposable/runtime material was omitted from this canonical package: `__pycache__`, `.pyc`, Syncthing temporary files, audit logs, cron log, and historical `agent_state.backup.*` snapshots.

The live `config.json` was NOT included because it contained real API credentials. A sanitized `config.example.json` is included instead.

## June 26 synchronization fixes found in live code

1. Pause state: IMPLEMENTED.
   - Consecutive-loss state is centralized through `get_consecutive_loss_status()`.
   - Stale consecutive-loss pauses can be auto-cleared when deterministic count is below threshold or the completed-trades ledger is incomplete.
   - Published dashboard state is refreshed from the ledger.

2. Alpaca open orders -> SQLite: IMPLEMENTED.
   - `agenttrade.db.sync_open_orders_from_alpaca()` upserts broker orders by Alpaca order ID.
   - `agenttrade.publish` invokes synchronization before publishing state.
   - Broker-side protective stop prices are extracted from open orders for dashboard/risk display.

3. `long_market_value`: IMPLEMENTED.
   - Verification prefers Alpaca/account snapshot values when present.
   - It falls back to summing position market values (or derived qty * price) when the account field is absent.

4. Authentication/environment preservation: PARTIAL/DEPLOYMENT-SENSITIVE.
   - Paper/live endpoint handling remains in `agent_config.py`.
   - Service/deployment files are present, but no secret-bearing runtime environment is included in this recovery archive.

## Additional later changes found

The live tree contains later work beyond the checkpoint, including scoped post-sell buy locks, stronger pending-sell checks, backtest/strategy-mode components, account synchronization, agreement/confidence/attribution modules, dashboard/settings assets, deployment scripts, and tests.

## Known gaps

- `period_summary.py` is referenced by `performance_history.py`, `backtest_engine.py`, and `tests/test_period_summary.py`, but is not present in either supplied archive.
- `report_export.py` is imported by `config_server.py`, but is not present in either supplied archive.
- The supplied test `tests/test_period_summary.py` therefore cannot collect successfully until `period_summary.py` is recovered/reconstructed.
- This package has been syntax-compiled, but it has not been run against Alpaca or external LLM APIs because credentials/network/runtime dependencies are intentionally absent.

## Security action required

The supplied live archive contained unredacted API credentials in `config.json`. Rotate/revoke all credentials that were present there before reusing this recovered codebase. Do not copy the old `config.json` into a new deployment.

## 2026-08-27 reconstruction pass

The two source modules missing from both supplied recovery archives were reconstructed from preserved contracts and call sites:

- `period_summary.py` — reconstructed from `tests/test_period_summary.py`, `performance_history.py`, `backtest_engine.py`, and the Reports dashboard schema. Provides max drawdown, FIFO trade statistics, SPY benchmark retrieval (yfinance with Alpaca fallback), and period-summary assembly.
- `report_export.py` — reconstructed from the Flask export routes and the browser-side CSV fallback in `dashboard.html`. Server exports now mirror the existing dashboard CSV structure.

Validation performed after reconstruction:

- Existing period-summary unit tests: PASS.
- Added report-export contract tests: PASS.
- Full local pytest suite: **14 passed**.
- `compileall` across the recovered tree: PASS.
- Imports of `period_summary`, `report_export`, `performance_history`, and `backtest_engine`: PASS.

These two files are reconstructions, not byte-for-byte recovery of the deleted originals. Their behavior is intentionally constrained to the interfaces and output contracts preserved by the surviving code/tests.

## v3 amendment — 2026-08-27

Recovered original July `cycle.py` and `agents/research.py` from independently supplied copies. In v3.1, `analysis_funnel.py` was also restored verbatim from three independently supplied byte-identical historical copies (SHA-256 `7f84a607623a3504d0568fd372a0002f573dab38bb173dd55ebc2d66d66c37e7`), replacing the temporary v3 reconstruction. The v3 SQLite funnel/cycle-artifact hardening and manual-stop/daily-trade SQLite work are retained. See `V3_RECOVERY_NOTES.md` for provenance and validation.
