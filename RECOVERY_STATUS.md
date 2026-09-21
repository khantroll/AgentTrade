# AgentTrade recovery status

This repository is being rebuilt from the last complete canonical AgentTrade archive preserved in ChatGPT Library.

## Canonical baseline

- Archive: `AgentTrade-Recovered-Canonical-v3.1.zip`
- Created: 2026-08-27
- SHA-256: `50f0b4f00570082c393a5b9d00f6160c81b3310dbc288a083683969caec9fb48`
- Recovery validation at creation: 18/18 tests passed and compileall passed.

The preserved archive contains the full recovered source tree, including `cycle.py`, `agents/research.py`, the `agents/*` pipeline, `agenttrade/*` SQLite/risk/reconciliation modules, dashboard/settings/API files, deployment scripts, and tests.

## Important limitation

This is the last **complete recoverable baseline**. Later Cursor/production edits made after v3.1 are not guaranteed to be present. Do not describe this repository as containing every September edit unless those changes are separately recovered and committed.

## Secrets and runtime state

Do not commit `.env`, production SQLite databases, logs, virtual environments, or generated runtime state.
