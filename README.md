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

Supported `LLM_MODE` values include `tiered`, `dual`, `claude_haiku`, `claude_sonnet`, `gpt4o_mini`, `gpt4o`, `gemini_flash`, `gemini_pro`, `mistral_small`, `mistral_large`, `deepseek_chat`, `deepseek_reasoner`, `groq_llama`, `groq_qwen`, `nvidia_llama`, `nvidia_qwen`, `nvidia_deepseek`, and `openrouter_free`.

Optional cheap failover (off until a real key is set; paper defaults do not require these):

```
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=openrouter/free
NVIDIA_API_KEY=
NIM_API_KEY=
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
LLM_TIERED_AUTO_FAILOVER=1
```

`OPENROUTER_MODEL=openrouter/free` is OpenRouter's free-model router (OpenAI-compatible, $0). NVIDIA NIM uses the same chat-completions shape. With `LLM_TIERED_AUTO_FAILOVER=1` (the default), a real key appends that provider after the explicit tier list. Placeholder values such as `<SET_NVIDIA_API_KEY>` do not enable it. Set `LLM_TIERED_AUTO_FAILOVER=0` to keep the explicit list exclusive. You can also name `openrouter_free` or `nvidia_llama` yourself in `LLM_TIERED_RESEARCH_MODELS` / `LLM_TIERED_ANALYSIS_MODELS`.

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
- If research did return names but analysis **fails** (every model errors, or returns prose / invalid JSON / an empty object / no BUY/SELL/HOLD/SKIP), that failure is not a model opinion. The decision is a fail-closed SKIP (`analysis_path=fail_closed`). Signal strength is kept for diagnostics and does not authorize a BUY after an invoked analysis LLM fails. A schema-valid model BUY, SELL, HOLD, or SKIP is still the decision, including a real multi-model SKIP. Tiered analysis walks the configured list until `LLM_TIERED_ANALYSIS_FANOUT` (or `LLM_TIERED_FANOUT`) valid decisions; an unusable body gets one strict retry, then the next model, and is not marked healthy. Tier 2 still sizes the order. Milestone C cash, held-name, average-down, drawdown, and max-invested gates are unchanged. Protective sells stay on position review and do not wait on this path.
- `failed` and `empty` stay distinct on `research_status` in the cycle log, SQLite artifact, and dashboard. An empty candidate table is not how a broken research gate is reported.
- Protective exits (position review and hard rebalance) run even when the token budget blocks LLM calls.
- Tiered research keeps walking the provider list until `LLM_TIERED_FANOUT` (or `LLM_TIERED_VALID_PARSES`) valid JSON parses, or the list is exhausted. HTTP 200 with prose, a code fence, or `{}` is not success. A decision object embedded in reasoning prose or in the `reasoning` field is a real parse. Prose with no JSON object is not, and an analysis that was already invoked still fail-closes to SKIP. An unusable body from one provider does not stop the walk: the next ready provider is tried, and `invalid_parse` is the result only when every attempt was unusable. One provider's cooldown does not drop the others. The research gate returns `no_models_available` only when every explicit tier and every enabled failover is missing a key, cooling, or model-suppressed. Model-not-found / HTTP 404, and HTTP 410 / end-of-life, suppress that model for `LLM_MODEL_NOT_FOUND_SUPPRESS_HOURS` (default 24) and do not cool the whole provider. A 410 is a config error: set `NVIDIA_LLAMA_MODEL` (or the matching model env) to a current id. A message that merely contains the letters "rate" (for example "separate") is not a 429.
- Rate-limit benches are not one 2-hour timer. A provider wait of a few seconds (`Retry-After`, "try again in 5s", Groq TPM) is used as-is, capped by `LLM_SHORT_RATE_LIMIT_CAP_SECONDS` (default 900). An unspecified HTTP 429 such as Mistral "rate limit exceeded" uses `LLM_RATE_LIMIT_COOLDOWN_MINUTES` (default 120). A cooldown saved before that split (reason `rate-limit (429)` or `rate/capacity/token-limit error`, with no `cooldown_class`) is clamped to the short cap the next time health is loaded, so a deploy is not stuck behind a 7200s bench written by the old code. A bench written by the current policy stores `cooldown_class` and is not shortened, including a fresh unspecified 429. An HTTP 429 that mentions quota or billing (Gemini "check your plan and billing details") uses `LLM_QUOTA_COOLDOWN_MINUTES` (default 60), not 24 hours. HTTP 402 / insufficient balance / billing disabled, with no 429, uses `LLM_BILLING_COOLDOWN_HOURS` (default 6) so a dead bill is not hammered and the provider is not dark for a full day. Auth failures stay at 24 hours.
- Repo Groq defaults are `GROQ_QWEN_MODEL=qwen/qwen3.8-27b` and `GROQ_LLAMA_MODEL=openai/gpt-oss-120b`. `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` 404 on the current host.
- Built-in bucket defaults are Growth 45% / Dividend 25% / Swing 20% / Crypto 10% (`CRYPTO_MAX_ALLOCATION`, default `0.10`). Override with `GROWTH_ALLOCATION`, `DIVIDEND_ALLOCATION`, `SWING_ALLOCATION`, and `CRYPTO_MAX_ALLOCATION`. A recommended tiered list is in `config.example.json` (`groq_qwen,gemini_flash,mistral_small` for research). That list stays the primary walk. A real `OPENROUTER_API_KEY` or `NVIDIA_API_KEY` is appended after it when `LLM_TIERED_AUTO_FAILOVER` is on, so a Groq or Mistral 429 does not end research while the failover is still ready. Analysis that has already invoked an LLM still fail-closes to SKIP when every model fails or returns invalid JSON; that path does not become a screener BUY. Research-stage failure still may use the screener universe.

`signal_strength` is the capped raw conviction (a one-pipeline name stays weak). `signal_mix` is the relative composition and can sum to 100. `total_score` is the strength alias and `components` is the mix alias, so older readers follow the split. Reddit raw points are capped at one full-rank Congress hit (`REDDIT_RAW_CAP`, weight 3 × 10 = 30) before that mix is computed, which keeps Reddit below the combined congress + news + movers + momentum ceiling (80).

`run_cycle.sh` and `monitor.sh` export `.env` with `set -a` before Python so import-time settings such as `CRYPTO_MAX_ALLOCATION` are visible. `run_cycle.sh` uses `flock -E 75` so a busy lock exits 0 and a failed cycle keeps its own exit code.

## Milestone C risk guards

Approvals in one cycle share a cash ledger and a global held set. Tier 2 still sizes the order. These checks only decide whether another buy may be opened.

**Cash.** Each approved buy reserves its deterministic notional. The next approval, including one in a later bucket, sees the reduced cash and is refused with `insufficient_cash` when the remainder cannot fund it. Margin accounts (`ALLOW_MARGIN`) keep the existing cash exception.

Usable cash for the entry gate is Alpaca cash, minus AgentTrade's own resting buy notional, minus `RESERVE_CASH_PCT` of portfolio value (default 10%). That result must still clear `MIN_CASH_RESERVE` (default $5,000). An open buy from another app on the shared Alpaca account (for example a CryptoAgent `HYPE/USD` order) is not AgentTrade reserved cash. Alpaca often removes that hold from `cash` while leaving it inside `equity`; the equity-mismatch alarm ignores a gap that the foreign open buy explains. AgentTrade orders (`agenttrade-` client id, a strategy name, or a broker id in the order ledger) are still reserved. On a book where 10% of portfolio value already exceeds cash minus $5,000, buys stay blocked after the foreign order is left out. That reserve and the $5,000 floor are unchanged. When the floor blocks buys, the cycle log and the dashboard say so and show usable cash, cash, the reserve dollars, and `MIN_CASH_RESERVE`. The thresholds themselves are not changed.

**Held names are global.** A symbol already in the portfolio, or approved earlier in the same cycle, is not opened again in another bucket (`cross_bucket_duplicate`). A holding with no bucket tag is `already_held`.

**No implicit average-down.** Room under the position-size cap is not permission to add. The paper default is no automatic adds:

| Situation | Result |
|---|---|
| Same bucket, unrealized P&L ≤ 0, `ALLOW_POSITION_ADDS` false (default) | `average_down_blocked` |
| Same bucket, unrealized P&L > 0, adds disabled | `add_not_allowed` |
| `ALLOW_POSITION_ADDS=true` and P&L ≤ 0, `ALLOW_AVERAGE_DOWN` false (default) | `average_down_blocked` |
| Adds enabled, and either P&L > 0 or `ALLOW_AVERAGE_DOWN=true` | add allowed unless market value is at least 90% of the bucket max (`position_near_max`) |

`ALLOW_AVERAGE_DOWN` does nothing while adds are disabled. Both default to false.

**Stress gates.** `MAX_ACCOUNT_DRAWDOWN_PCT` (default `0.10`) blocks new buys with `drawdown_pause` when equity is at least that far below the high-water mark (`high_water_equity` on the cycle snapshot, otherwise the `HIGH_WATER_EQUITY` flag). `MAX_INVESTED_PCT` (default `0.90`) blocks new buys with `max_invested` when long market value plus notionals already reserved this cycle would reach that fraction of equity. Set either value to `0` to turn that risk-gate check off. Protective sells are unchanged. ATR / 0.5% equity sizing is unchanged.

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


# NVIDIA NIM (OpenAI-compatible). Base URL only — the client appends /chat/completions.
# A saved URL that already ends in /chat/completions is used as-is.
NVIDIA_API_KEY=
NIM_API_KEY=
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
# meta/llama-3.1-70b-instruct is retired on hosted NIM (HTTP 410 since 2026-08-26).
# A config that still names that id is rewritten to the default below.
# Set NVIDIA_LLAMA_MODEL to any other current model id to override.
NVIDIA_LLAMA_MODEL=nvidia/nemotron-3-super-120b-a12b
# nvidia/nemotron-3-super-120b-a12b reasons by default (NVIDIA NIM docs:
# chat_template_kwargs.enable_thinking, default true). JSON calls send
# enable_thinking false unless LLM_ENABLE_THINKING=1, and raise max_tokens to
# LLM_REASONING_JSON_MAX_TOKENS (default 1024). The match is the model id
# ("nemotron", plus LLM_THINKING_MODELS), not the vendor. A host that rejects
# chat_template_kwargs is retried once without it.
NVIDIA_QWEN_MODEL=qwen/qwen3-235b-a22b
NVIDIA_DEEPSEEK_MODEL=deepseek-ai/deepseek-r1

# OpenRouter free/cheap failover. Blank key = disabled. No secret belongs in git.
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=openrouter/free
# 1 appends openrouter_free and nvidia_llama after the explicit tier list when those keys are real.
LLM_TIERED_AUTO_FAILOVER=1
