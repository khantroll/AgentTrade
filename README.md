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

`agent_state.json` and the public dashboard copy are replaced only when the serialized payload is within `AGENT_STATE_MAX_BYTES` (default **8 MiB**, 8388608). Set that env var, or the same key in `config.json`, to change the cap. An oversized publish is refused and the previous projection is left in place; SQLite is not modified. Nested escape bloat previously grew this file to about 1.4GB. A multi-gigabyte `agenttrade.sqlite3` OOM is a separate host/ops follow-up and is not handled by this cap.

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
Agent 1: Research — LLM picks top N per bucket
    │ candidates[]  CANDIDATE rows
    ▼
Agent 2: Analysis — BUY or SKIP (no executable sizing)
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

---

## Tests

```bash
python3 -m compileall -q .
python3 -m pytest -q
```

v3.2 recovery validation: **46 passed**, `compileall` clean, recovered `agenttrade.sqlite3` hash unchanged. Tests use a temporary SQLite file and do not call Alpaca.

See `V3_RECOVERY_NOTES.md` for provenance, remaining fallbacks, and the changed-file list.

---

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
