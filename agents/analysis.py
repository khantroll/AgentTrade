"""Agent 2: Analysis — per-candidate buy/skip decisions."""

import json
import logging

import agent_config as cfg
from analysis_funnel import partition_for_analysis
from buckets import Bucket
from llm_router import active_mode, budget_exhausted, query_analysis
from market_data import (
    audit_log,
    compact_market_data_rows,
    fetch_crypto_data,
    fetch_stock_data,
    is_crypto_bucket,
)

log = logging.getLogger(__name__)


def analysis_agent(
    candidates: list,
    account: dict,
    positions: list,
    bucket: Bucket,
    attribution_map: dict = None,
) -> list:
    log.info("[%s/Analysis] Analyzing %d candidates (mode=%s)...", bucket.name, len(candidates), active_mode())

    if budget_exhausted():
        log.warning("[%s/Analysis] Token budget exhausted — skipping.", bucket.name)
        return [{
            "ticker": c.get("ticker"),
            "action": "SKIP",
            "skip_reason": "token_budget_exhausted",
            "blocked_reason": "token_budget_exhausted",
            "rationale": "Token budget exhausted",
            "bucket": bucket.name,
        } for c in (candidates or []) if c.get("ticker")]

    portfolio_value = float(account.get("portfolio_value", 10000))
    bucket_capital = cfg.bucket_manager.bucket_capital(bucket, portfolio_value)
    bucket_positions = cfg.bucket_manager.bucket_positions(bucket, positions)
    cash = float(account.get("cash", 0))
    held_tickers = [p["symbol"] for p in positions]
    max_pos_dollars = cfg.bucket_manager.max_position_dollars(bucket, portfolio_value)

    skip_decisions, analyze = partition_for_analysis(
        candidates,
        cfg.MIN_CONFIDENCE_FOR_ANALYSIS,
        cfg.MAX_ANALYSIS_CANDIDATES,
        bucket.name,
    )
    decisions = list(skip_decisions)
    log.info(
        "[%s/Analysis] Funnel: %d in → %d LLM analysis, %d pre-skips",
        bucket.name,
        len(candidates),
        len(analyze),
        len(skip_decisions),
    )

    for candidate in analyze:
        ticker = candidate["ticker"]
        data = fetch_crypto_data(ticker) if is_crypto_bucket(bucket) else fetch_stock_data(ticker)

        if is_crypto_bucket(bucket):
            order_instruction = (
                "Do NOT specify share count or dollar notional — sizing is computed by the risk engine. "
                "Recommend BUY or SKIP only."
            )
            response_schema = """{
  "decision": "BUY" or "SKIP",
  "confidence": <0.0-1.0>,
  "bull_case": "<why this could work>",
  "bear_case": "<why this could fail>",
  "key_risks": ["<risk bullets>"],
  "signal_summary": "<how screener/signals support this>",
  "reddit_summary": "<Reddit/crowd sentiment read, if relevant>",
  "why_now": "<timing rationale>",
  "rationale": "<one sentence summary>",
  "suggested_bucket": "<optional>",
  "stop_loss_price": <number or null, advisory only>,
  "take_profit_price": <number or null, advisory only>
}"""
        else:
            order_instruction = (
                "Do NOT specify share count, qty, or notional — sizing is computed by the risk engine. "
                "Recommend BUY or SKIP only."
            )
            response_schema = """{
  "decision": "BUY" or "SKIP",
  "confidence": <0.0-1.0>,
  "bull_case": "<why this could work>",
  "bear_case": "<why this could fail>",
  "key_risks": ["<risk bullets>"],
  "signal_summary": "<how screener/signals support this>",
  "reddit_summary": "<Reddit/crowd sentiment read, if relevant>",
  "why_now": "<timing rationale>",
  "rationale": "<one sentence summary>",
  "suggested_bucket": "<optional>",
  "stop_loss_price": <number or null, advisory only>,
  "take_profit_price": <number or null, advisory only>
}"""

        if cfg.STRATEGY_AGGRESSION == "conservative":
            buy_rules = "BUY only if confidence ≥ 0.70 AND RSI < 65 AND above SMA-20 AND cash available."
        elif cfg.STRATEGY_AGGRESSION == "aggressive":
            buy_rules = "Lean toward BUY. SKIP only if: already at position limit, no cash, RSI > 78, or clearly negative EPS."
        else:
            buy_rules = (
                "BUY if confidence ≥ 0.55 and cash available and not already at position limit. "
                "SKIP if RSI > 75 or deteriorating fundamentals."
            )

        attr = (attribution_map or {}).get(str(ticker).upper()) or candidate.get("signal_attribution") or {}
        components = attr.get("components") or candidate.get("signal_components") or {}
        signal_block = ""
        if components:
            signal_block = f"\nSignal breakdown (total {attr.get('total_score', '?')}): {json.dumps(components)}"
        reddit_detail = attr.get("reddit_detail") or {}
        if reddit_detail:
            signal_block += (
                f"\nReddit: {reddit_detail.get('mention_count', 0)} mentions, "
                f"sentiment={reddit_detail.get('avg_sentiment')}, "
                f"quality={reddit_detail.get('quality_score')}, "
                f"subs={reddit_detail.get('subreddits', [])}"
            )

        prompt = f"""You are a portfolio manager for the "{bucket.name}" bucket (aggression={cfg.STRATEGY_AGGRESSION.upper()}).

Bucket strategy: {bucket.mode}
Bucket capital:  ${bucket_capital:,.2f}
Max position:    ${max_pos_dollars:,.2f} ({bucket.max_position_pct * 100:.0f}% of bucket)
Stop-loss:       {bucket.stop_loss_pct * 100:.0f}% below entry
Take-profit:     {bucket.take_profit_pct * 100:.0f}% above entry
Available cash:  ${cash:,.2f}
Already holding: {held_tickers}
Positions in this bucket: {len(bucket_positions)} / {bucket.max_positions} max

Market data:
{json.dumps(compact_market_data_rows([data], limit=1)[0], separators=(",", ":"))}

Research note: {candidate.get('reason', '')} (confidence: {candidate.get('confidence', 0.5)})
{"Dual-model agreement on this pick: YES" if candidate.get('dual_agree') else ""}
{"For DIVIDEND bucket: weigh dividend yield, payout sustainability, ex-div date." if bucket.mode == "dividend" else ""}
{"For CRYPTO bucket: weigh 24/7 volatility, trend, liquidity, BTC/ETH beta, and slippage risk. Do not over-size." if bucket.mode == "crypto" else ""}
{signal_block}
{order_instruction}

Provide structured reasoning. Reddit/crowd sentiment is a first-class signal — summarize it in reddit_summary when relevant.

Respond ONLY with valid JSON — no other text:
{response_schema}"""

        result = query_analysis(prompt, agent_tag=f"{bucket.name}_analysis")
        if not result:
            log.error("[%s/Analysis] No result for %s.", bucket.name, ticker)
            decisions.append({
                "ticker": ticker,
                "action": "SKIP",
                "skip_reason": "llm_no_result",
                "blocked_reason": "llm_no_result",
                "rationale": "Analysis LLM returned no result",
                "bucket": bucket.name,
                "current_price": (data or {}).get("current_price") if data else None,
            })
            continue

        # Normalize LLM contract: decision → action; strip executable sizing
        if "decision" in result and "action" not in result:
            result["action"] = str(result["decision"]).upper()
        elif "action" not in result:
            result["action"] = "SKIP"

        from agenttrade.risk import strip_llm_sizing
        result = strip_llm_sizing(result)

        result["ticker"] = ticker
        result["current_price"] = data.get("current_price")
        result["bucket"] = bucket.name
        result["dual_agree"] = candidate.get("dual_agree", False)
        result["atr_pct"] = data.get("atr_pct")
        result["atr"] = data.get("atr14")
        result["signal_components"] = components
        result["total_score"] = attr.get("total_score")
        result["reddit_detail"] = reddit_detail
        if result.get("sentiment_summary") and not result.get("reddit_summary"):
            result["reddit_summary"] = result.get("sentiment_summary")
        if result.get("risks") and not result.get("key_risks"):
            result["key_risks"] = result.get("risks")
        if data.get("ohlcv"):
            result["ohlcv"] = data["ohlcv"]

        dual_status = result.get("dual_status", "")
        if dual_status == "conflict":
            says = {k[:-5]: v for k, v in result.items() if k.endswith("_says")}
            log.info("[%s/Analysis] %s: DUAL CONFLICT %s → SKIP", bucket.name, ticker, says)
        else:
            log.info(
                "[%s/Analysis] %s: %s (sizing=deterministic) %s",
                bucket.name,
                ticker,
                result.get("action", result.get("decision", "SKIP")),
                "✓ dual" if dual_status == "both_buy" else "",
            )

        audit_log(result.get("action", "SKIP"), result.get("ticker", "?"), prompt, result, result, data)
        decisions.append(result)

    return decisions
