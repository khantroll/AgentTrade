# AI Trading Agent

A multi-agent paper trading system powered by Claude + Alpaca with a **dynamic stock screener** — no static watchlist.

---

## Truth model

| Layer | Role |
|---|---|
| **Alpaca** | Broker truth — cash, positions, open orders, fills |
| **SQLite (`agenttrade.sqlite3`)** | Durable AgentTrade truth — cycles, funnel, artifacts, flags, lots, risk events |
| **`agent_state.json`** | Disposable dashboard projection/cache, generated after publish |

Credentials stay in `config.json` / `.env`. They are never stored in SQLite.

`bucket_tags.json` remains the bucket-ownership file (not migrated).

---

## Architecture

```
Scheduler (market-hours cycles)
    │
    ▼
SQLite init + reconciliation gate (Alpaca vs ledger)
    │  fail → halt/pause, no research/orders
    ▼
Position review / optional hard rebalance (protective sells)
    │
    ▼
screener.py — Dynamic Universe Builder
  Pipeline 1: Momentum (yFinance)
  Pipeline 2: Alpaca movers
  Pipeline 3: News sentiment (optional)
  Pipeline 4: Congress trades (optional)
  Pipeline 5: Reddit VADER (optional)
    │ universe[]  persisted as UNIVERSE funnel rows
    ▼
Agent 1: Research — LLM picks when the parse is valid
    │  failed or empty → candidates from universe order (source=screener)
    │  ok with picks → LLM candidates (source=research)
    ▼
Agent 2: Analysis — LLM BUY/SKIP, or deterministic annotate on screener fallback
    │ decisions[]  DECISION rows (pre-skips have skip_reason)
    ▼
Bucket risk gate + deterministic risk manager
    │  strips LLM qty/notional; ATR/flat stop; cash/pause/buy-lock
    │ approved[]  RISK APPROVED / BLOCKED
    ▼
Agent 4: Execution — Alpaca orders (brackets when stop+TP exist)
    │ ORDER rows (placed / blocked / failed / broker_rejected)
    ▼
Ledger sync + dashboard publish (JSON projection only)
```

Every symbol in a cycle is traceable by `cycle_run_id` through
`UNIVERSE → CANDIDATE → DECISION → RISK → ORDER|BLOCKED`.

---

## Files

| File | Purpose |
|---|---|
| `agent.py` | Daemon entry — schedule + config server |
| `cycle.py` | One full trading cycle |
| `screener.py` | Dynamic universe builder |
| `agents/` | Research, analysis, risk, execution, position review |
| `agenttrade/db.py` | SQLite ledger (schema v4 + funnel uniqueness) |
| `agenttrade/reconciliation.py` | Mandatory Alpaca/SQLite gate |
| `agenttrade/risk.py` | Deterministic sizing (ignores LLM qty/notional) |
| `agenttrade/publish.py` | Dashboard state from SQLite + Alpaca |
| `dashboard.html` | Browser dashboard |
| `config.example.json` | Sanitized config template (no secrets) |
| `bucket_tags.json` | Symbol → bucket ownership |
| `agent_state.json` | Generated projection only — safe to delete and rebuild |
| `trading_agent.log` | Cycle log |

---

## Quick Start

### 1. Get API keys

**Required:**
- **Alpaca** (paper trading) — free at https://alpaca.markets → Paper Trading → API Keys
- At least one LLM key: Anthropic, OpenAI, Google Gemini, Mistral, DeepSeek, or Groq.

Supported `LLM_MODE` values include `tiered`, `dual`, `claude_haiku`, `claude_sonnet`, `gpt4o_mini`, `gpt4o`, `gemini_flash`, `gemini_pro`, `mistral_small`, `mistral_large`, `deepseek_chat`, `deepseek_reasoner`, `groq_llama`, and `groq_qwen`.

**Optional (unlock more screener pipelines):**
- **NewsAPI** — free at https://newsapi.org (100 req/day free tier) → enables news sentiment pipeline
- **Quiver Quantitative** — free at https://api.quiverquant.com (500 req/day) → enables Congressional trades pipeline

### 2. Install & configure

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and fill in your keys
```

### 3. Test the screener standalone first

```bash
python screener.py
```

You'll see the pipelines run and print a universe of 20–50 stocks. No Anthropic or Alpaca calls happen here — it's safe to run any time.

### 4. Run the full agent

```bash
python agent.py
```

The agent runs one full cycle immediately, then schedules itself for market hours:
- 9:35am ET (5 min after open)
- 11:30am ET
- 1:30pm ET
- 3:00pm ET

### 5. View the dashboard

Open `dashboard.html` in any browser. The dashboard can load a generated `agent_state.json` projection, but live `/state` and `/account/live` rebuild from SQLite first (Alpaca when `?live=1`). Deleting `agent_state.json` does not destroy cycle funnel, universes, or ledger history.

---

## Remaining JSON files

`bucket_tags.json` is still the bucket-ownership source. Credentials stay in `config.json` / `.env`. `agent_state.json` is a generated projection. `performance_history.jsonl`, `trade_log.jsonl`, and LLM router JSON files are auxiliary logs, not broker or risk authority.

Publish refuses to replace `agent_state.json` or the public copy when the serialized UTF-8 payload is larger than `AGENT_STATE_MAX_BYTES` (default **8 MiB**). The previous projection files stay in place. This check does not delete or rewrite the SQLite ledger. A multi-gigabyte `agenttrade.sqlite3` that OOMs the host is a separate ops follow-up and is not handled here.

---

## Tests

```bash
python3 -m compileall -q .
python3 -m pytest -q
```

v3.2 recovery validation: **46 passed**, `compileall` clean, recovered `agenttrade.sqlite3` hash unchanged. Tests use a temporary SQLite file and do not call Alpaca.

See `V3_RECOVERY_NOTES.md` for provenance, remaining fallbacks, and the changed-file list.

---

## Routing, research failure, and bucket defaults

LLM research is a narrator and a fallback, not the only way into a trade.

- A schema-valid research object with picks (`research_status=ok`) is still used. The research prompt for that call includes the screener evidence from the same universe (ranks, pipeline hits, `signal_strength`, `signal_mix`).
- If research **fails** (provider error, 429, parse failure, budget exhaustion, model-not-found, no healthy model) or returns a **valid empty** set, and the screener already built a universe, entries come from that universe (`source=screener`) ordered by `signal_strength`. Analysis for those names is deterministic; risk and execution gates are unchanged.
- `failed` and `empty` stay distinct on `research_status` in the cycle log, SQLite artifact, and dashboard. An empty candidate table is not how a broken research gate is reported.
- Protective exits (position review and hard rebalance) run even when the token budget blocks LLM calls.
- Tiered research keeps walking the provider list until `LLM_TIERED_FANOUT` (or `LLM_TIERED_VALID_PARSES`) valid JSON parses, or the list is exhausted. HTTP 200 with prose, a code fence, or `{}` is not success. A 429 backs the provider off (`LLM_RATE_LIMIT_COOLDOWN_MINUTES`, default 120) and tries the next tier. Model-not-found / HTTP 404 suppresses that model for `LLM_MODEL_NOT_FOUND_SUPPRESS_HOURS` (default 24).
- Repo Groq defaults are `GROQ_QWEN_MODEL=qwen/qwen3.8-27b` and `GROQ_LLAMA_MODEL=openai/gpt-oss-120b`. `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` 404 on the current host.
- Built-in bucket defaults are Growth 45% / Dividend 25% / Swing 20% / Crypto 10% (`CRYPTO_MAX_ALLOCATION`, default `0.10`). Override with `GROWTH_ALLOCATION`, `DIVIDEND_ALLOCATION`, `SWING_ALLOCATION`, and `CRYPTO_MAX_ALLOCATION`. A recommended tiered list is in `config.example.json` (`groq_qwen,gemini_flash,mistral_small` for research).

`signal_strength` is the capped raw conviction (a one-pipeline name stays weak). `signal_mix` is the relative composition and can sum to 100. `total_score` is the strength alias and `components` is the mix alias, so older readers follow the split. Reddit raw points are capped at one full-rank Congress hit (`REDDIT_RAW_CAP`, weight 3 × 10 = 30) before that mix is computed, which keeps Reddit below the combined congress + news + movers + momentum ceiling (80).

`run_cycle.sh` and `monitor.sh` export `.env` with `set -a` before Python so import-time settings such as `CRYPTO_MAX_ALLOCATION` are visible. `run_cycle.sh` uses `flock -E 75` so a busy lock exits 0 and a failed cycle keeps its own exit code.

Not in this pass: cash-ledger / held-set / no-average-down controls, drawdown or max-invested limits, or ATR / 0.5% equity sizing.

## Configuration

In `agent.py`:

| Variable | Default | Description |
|---|---|---|
| `MAX_POSITION_PCT` | `0.08` | Max 8% of portfolio per trade |
| `MAX_DAILY_TRADES` | `5` | Hard cap on orders per day |
| `STOP_LOSS_PCT` | `0.05` | Auto stop-loss 5% below entry |
| `TAKE_PROFIT_PCT` | `0.10` | Auto take-profit 10% above entry |

In `screener.py`:

| Variable | Default | Description |
|---|---|---|
| `MIN_PRICE` | `5.0` | Skip stocks below $5 (penny stocks) |
| `MAX_PRICE` | `500.0` | Skip stocks above $500 |
| `MIN_AVG_VOL` | `500,000` | Liquidity filter |
| `max_universe` | `40` | Max tickers passed to research agent |

---

## How the Screener Weights Work

Each pipeline contributes to a shared score per ticker:

```
Congress buys  ×3.0  ← rarest, highest conviction
News mentions  ×2.0  ← fresh catalyst
Alpaca movers  ×2.0  ← price action confirmed
Momentum score ×1.0  ← technical foundation
```

If NVDA appears in all four pipelines it scores ~8× higher than a stock only in the momentum screen. Tickers appearing in multiple pipelines are the agent's highest-conviction candidates.

Reddit's ensemble weight is 2.5, between news and Congress. A separate raw-point cap (`REDDIT_RAW_CAP` in `signal_attribution.py`) stops Reddit quality, sentiment, and mention bonuses from exceeding a full-rank Congress hit (30 points) before composition is normalized. That cap is what keeps one noisy Reddit run from drowning congress, news, movers, and momentum.

---

## ⚠️ Important Warnings

- **Always start with `ALPACA_PAPER=true`** — never risk real money until you've watched the agent run paper trades for weeks
- AI trading agents can and do lose money — this is an experiment
- The Congressional trades data is disclosed with a delay (45 days legally) — treat it as one signal, not a guarantee
- Past performance in paper trading ≠ real trading performance (slippage, spreads, market impact all matter)
- This is not financial advice

---

## Going Further

- **Add earnings calendar** — skip trading stocks reporting earnings within 3 days (huge volatility risk)
- **Add Reddit sentiment** — r/wallstreetbets mentions via Reddit API
- **Add backtesting** — run screener + agent logic against historical data before going live
- **Add Telegram alerts** — get notified on every trade via a Telegram bot
- **Track P&L over time** — log to SQLite and chart performance week-over-week


# NVIDIA NIM (OpenAI-compatible)
NVIDIA_API_KEY=
NIM_API_KEY=
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_LLAMA_MODEL=meta/llama-3.1-70b-instruct
NVIDIA_QWEN_MODEL=qwen/qwen3-235b-a22b
NVIDIA_DEEPSEEK_MODEL=deepseek-ai/deepseek-r1
# Add nvidia_llama/nvidia_qwen/nvidia_deepseek to tiered lists when desired.
