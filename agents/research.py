"""Agent 1: Research — LLM screening of screener universe."""

import json
import logging
import time
from datetime import datetime

import agent_config as cfg
from analysis_funnel import trim_research_selections
from buckets import Bucket
from llm_router import active_mode, budget_exhausted, query_research
from market_data import (
    compact_market_data_rows,
    fetch_crypto_data,
    fetch_stock_data,
    is_crypto_bucket,
)

log = logging.getLogger(__name__)


def research_agent(tickers: list, bucket: Bucket) -> list:
    log.info("[%s/Research] Screening %d tickers (mode=%s)...", bucket.name, len(tickers), active_mode())

    if budget_exhausted():
        log.warning("[%s/Research] Token budget exhausted — skipping.", bucket.name)
        return []

    market_data = []
    fetcher = fetch_crypto_data if is_crypto_bucket(bucket) else fetch_stock_data
    for ticker in tickers:
        data = fetcher(ticker)
        if data:
            market_data.append(data)
        time.sleep(0.25)

    if bucket.mode == "crypto":
        strategy_note = (
            "Focus on liquid crypto pairs only. Favor trend confirmation, 7d/30d momentum, "
            "controlled RSI, and avoid overextended spikes."
        )
        asset_label = "crypto pairs"
        analyst_role = "crypto market research analyst"
    elif bucket.mode == "dividend":
        strategy_note = "Focus on: dividend yield ≥ 2%, payout ratio < 85%, upcoming ex-dividend dates, positive EPS."
        asset_label = "stocks"
        analyst_role = "stock research analyst"
    elif bucket.mode == "price_range":
        strategy_note = f"Focus on: stocks ${bucket.min_price}–${bucket.max_price} with strong momentum + volume spikes."
        asset_label = "stocks"
        analyst_role = "stock research analyst"
    else:
        strategy_note = "Focus on: RSI 40–65, price above SMA-20 and SMA-50, strong revenue growth, reasonable P/E."
        asset_label = "stocks"
        analyst_role = "stock research analyst"

    prompt = f"""You are a {analyst_role}. Today is {datetime.now().strftime('%Y-%m-%d')}.
Bucket: {bucket.name} ({bucket.mode}) — {strategy_note}

Market data for {len(market_data)} {asset_label}:
{json.dumps(compact_market_data_rows(market_data), separators=(",", ":"))}

Select the TOP {cfg.RESEARCH_TOP_N} most promising {asset_label} for this bucket strategy.
Respond ONLY with valid JSON — no other text:
{{
  "selected": [
    {{
      "ticker": "AAPL or BTC/USD",
      "reason": "One sentence reason specific to the {bucket.name} strategy",
      "confidence": 0.75
    }}
  ]
}}

Return up to {cfg.RESEARCH_TOP_N} entries, ranked by confidence (highest first)."""

    result = query_research(prompt, agent_tag=f"{bucket.name}_research")
    if not result:
        log.error("[%s/Research] No result from LLM router.", bucket.name)
        return []

    selected = trim_research_selections(result.get("selected", []), cfg.RESEARCH_TOP_N)
    log.info(
        "[%s/Research] Selected: %s (dual_agree flags: %s)",
        bucket.name,
        [s["ticker"] for s in selected],
        [s.get("dual_agree") for s in selected],
    )
    return selected
