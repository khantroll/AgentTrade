# Cursor Prompt — AgentTrade Recovery Completion Pass

Work from this AgentTrade v3 recovery tree. Preserve the current trading strategy, broker behavior, deterministic risk sizing, reconciliation gate, bucket allocation rules, LLM routing, and dashboard behavior. This is a recovery/hardening pass, not a strategy redesign.

The project now has a SQLite v4 ledger with durable `funnel_events` and `cycle_artifacts`. `cycle.py` persists `UNIVERSE -> CANDIDATE -> DECISION -> RISK -> ORDER`; dashboard publishing prefers that SQLite data. Manual stop overrides are SQLite-backed, and daily trade count prefers SQLite fills. Existing tests pass.

Complete these three items:

1. Finish the SQLite transition.
   - Treat Alpaca as broker truth and SQLite as AgentTrade durable truth.
   - Treat `agent_state.json` only as a generated dashboard projection/cache; no trading/risk/config decision may depend on it when equivalent SQLite/live data exists.
   - Audit every Python read/write of `agent_state.json`, especially `account_sync.py`, `pnl_attribution.py`, `llm_attribution.py`, `rationale_attribution.py`, `margin_correction.py`, `performance_history.py`, `agent_config.py`, and config-server action paths.
   - Replace authoritative JSON reads with SQLite queries or live Alpaca data. Keep compatibility fallbacks only where necessary, clearly marked and non-authoritative.
   - Do not move secrets into SQLite. Keep credentials in the existing environment/config mechanism.
   - Preserve `bucket_tags.json` unless/until you add a tested SQLite replacement; do not silently change bucket ownership semantics.

2. Finish decision/funnel observability.
   - Verify every symbol can be traced by `cycle_run_id` across screener/universe, research candidate, LLM decision/pre-skip, deterministic risk approval/block reason, submitted order, Alpaca order/fill, and eventual outcome.
   - Avoid duplicate/ambiguous rows on retries. Add indexes or uniqueness/idempotency controls where appropriate.
   - Ensure pre-analysis SKIPs, risk rejections, buy-lock blocks, pause/halt blocks, and execution failures have explicit machine-readable reasons.
   - Make dashboard funnel counts and detail tables derive from SQLite first. Keep static JSON as projection only.
   - Ensure cycle artifacts needed for dashboard reconstruction (universes, screener sources/pipeline counts, token usage, rebalance state) survive deletion of `agent_state.json`.

3. Add production-path testing and prove recovery integrity.
   - Add a smoke test that imports `agent.py`, `cycle.py`, and every `agents/*` module with third-party/broker/network calls mocked.
   - Add cycle tests for: reconciliation pass/fail; stale consecutive-loss pause clearing; SQLite open-order synchronization; protective sell visibility; manual stop persistence; buy-lock behavior; risk rejection; deterministic sizing ignoring LLM qty/notional; execution failure; and a no-network dashboard rebuild from SQLite after deleting/renaming `agent_state.json`.
   - Add a test proving funnel replay for one synthetic symbol from UNIVERSE through ORDER/BLOCKED outcome.
   - Use a temporary SQLite DB for tests; never mutate the included recovered `agenttrade.sqlite3` fixture.
   - Run the complete suite, compileall, and a dependency/import audit.

Important recovery constraints:
- Do not delete legacy compatibility paths until tests demonstrate equivalent SQLite/live behavior.
- Do not change thresholds, trading schedules, bucket percentages, stop/take-profit policy, LLM prompts, or risk policy merely to make tests pass.
- Do not make live Alpaca calls during tests.
- Do not overwrite or expose API credentials.
- If you discover another missing historical file or an ambiguity that could change trading behavior, stop and document it instead of inventing strategy logic.

At completion, update `README.md` and `V3_RECOVERY_NOTES.md` to reflect the real architecture, list any remaining legacy JSON dependencies, report test results, and summarize changed files. The desired end state is: Alpaca = broker truth, SQLite = durable application truth, dashboard JSON = disposable projection.
