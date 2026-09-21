"""Structured signal score breakdown per candidate (Tier 3)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# Pipeline weights mirror screener merge (reddit remains first-class at 2.5)
PIPELINE_WEIGHTS = {
    "momentum": 1.0,
    "movers": 2.0,
    "news_sentiment": 2.0,
    "congress": 3.0,
    "reddit_sentiment": 2.5,
    "technicals": 2.0,
    "fundamentals": 1.5,
    "volume": 1.0,
}

COMPONENT_KEYS = (
    "reddit_sentiment",
    "news_sentiment",
    "momentum",
    "volume",
    "congress",
    "fundamentals",
    "technicals",
)


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rank_bonus(rank: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return max(0.5, 1.0 - (rank / total) * 0.5)


def _normalize_components(raw: dict[str, float], cap: float = 100.0) -> dict[str, float]:
    total_raw = sum(raw.values())
    if total_raw <= 0:
        return {k: 0.0 for k in COMPONENT_KEYS}
    scale = cap / total_raw
    return {k: round(raw.get(k, 0.0) * scale, 1) for k in COMPONENT_KEYS}


def build_component_scores(
    symbol: str,
    pipeline_hits: dict[str, dict],
    reddit_detail: Optional[dict] = None,
    tv_ratings: Optional[dict] = None,
    momentum_meta: Optional[dict] = None,
) -> dict:
    """
    Build structured attribution for one symbol.

    pipeline_hits: {pipeline_label: {rank, list_size, weight}}
    reddit_detail: enriched Reddit intelligence for symbol
    tv_ratings: {symbol: tv_score -1..1}
    """
    sym = symbol.upper()
    raw: dict[str, float] = {k: 0.0 for k in COMPONENT_KEYS}

    label_map = {
        "Momentum": "momentum",
        "Alpaca Movers": "momentum",
        "News Sentiment": "news_sentiment",
        "Congress Trades": "congress",
        "Reddit VADER": "reddit_sentiment",
        "TradingView": "technicals",
        "Dividend Quality": "fundamentals",
    }

    for label, meta in (pipeline_hits or {}).items():
        key = label_map.get(label, label.lower().replace(" ", "_"))
        if key not in raw:
            continue
        rank = int(meta.get("rank", 0))
        size = int(meta.get("list_size", 1))
        weight = _float(meta.get("weight"), PIPELINE_WEIGHTS.get(key, 1.0))
        raw[key] += weight * _rank_bonus(rank, size) * 10

    if reddit_detail:
        quality = _float(reddit_detail.get("quality_score"))
        sentiment = _float(reddit_detail.get("avg_sentiment"))
        mention_boost = min(_float(reddit_detail.get("mention_count")) * 0.5, 5.0)
        raw["reddit_sentiment"] += quality * 8 + sentiment * 12 + mention_boost

    if tv_ratings and sym in tv_ratings:
        tv = _float(tv_ratings[sym])
        raw["technicals"] += max(0.0, (tv + 1) / 2) * 15

    if momentum_meta and sym in momentum_meta:
        mm = momentum_meta[sym]
        raw["momentum"] += _float(mm.get("momentum_score")) * 12
        raw["volume"] += _float(mm.get("volume_score")) * 10

    components = _normalize_components(raw)
    total_score = round(min(100.0, sum(components.values())), 1)

    return {
        "symbol": sym,
        "total_score": total_score,
        "components": components,
        "pipelines": pipeline_hits,
        "reddit_detail": reddit_detail,
    }


def build_universe_attributions(
    universe: list[str],
    pipeline_membership: dict[str, dict],
    reddit_intelligence: Optional[dict] = None,
    tv_ratings: Optional[dict] = None,
    momentum_meta: Optional[dict] = None,
) -> dict[str, dict]:
    """Attribution map for every symbol in universe."""
    reddit_intel = reddit_intelligence or {}
    out = {}
    for sym in universe or []:
        sym_u = sym.upper()
        out[sym_u] = build_component_scores(
            sym_u,
            pipeline_membership.get(sym_u, {}),
            reddit_detail=reddit_intel.get(sym_u),
            tv_ratings=tv_ratings,
            momentum_meta=momentum_meta,
        )
    return out


def load_subreddit_weights() -> dict[str, float]:
    """Configurable subreddit weights (env JSON or defaults)."""
    defaults = {
        "wallstreetbets": 1.0,
        "stocks": 1.5,
        "investing": 2.0,
        "securityanalysis": 3.0,
        "valueinvesting": 2.5,
        "stockmarket": 1.3,
        "dividends": 2.0,
        "dividendinvesting": 2.2,
        "bogleheads": 2.0,
    }
    raw = os.getenv("SUBREDDIT_WEIGHTS", "")
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                defaults.update({str(k).lower(): float(v) for k, v in overrides.items()})
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("[SignalAttribution] Invalid SUBREDDIT_WEIGHTS JSON — using defaults")
    return defaults


def score_reddit_mention(
    *,
    upvotes: int,
    comment_count: int,
    post_age_hours: float,
    subreddit: str,
    vader_compound: float,
    author_karma: float = 0.0,
) -> dict:
    """Mention quality scoring for Reddit posts."""
    import math

    weights = load_subreddit_weights()
    sub_w = weights.get(str(subreddit).lower(), 1.0)

    upvote_score = math.log10(max(upvotes, 1) + 1) * 2.0
    engagement_score = math.log10(max(comment_count, 0) + 1) * 1.5
    age = max(post_age_hours, 0.25)
    velocity_score = min(upvotes / age, 50.0) / 10.0
    karma_bonus = min(math.log10(max(author_karma, 1) + 1), 2.0) * 0.5

    quality = (
        upvote_score + engagement_score + velocity_score + karma_bonus
    ) * sub_w * max(vader_compound, 0.05)

    return {
        "upvote_score": round(upvote_score, 3),
        "engagement_score": round(engagement_score, 3),
        "velocity_score": round(velocity_score, 3),
        "subreddit_weight": sub_w,
        "quality_score": round(quality, 3),
        "engagement_velocity": round(velocity_score, 3),
    }


def classify_sentiment_trend(current: float, prior: float) -> str:
    """Classify interest trend from period-over-period sentiment."""
    if prior <= 0 and current > 0:
        return "accelerating"
    delta = current - prior
    if delta >= 0.05:
        return "accelerating"
    if delta <= -0.05:
        return "fading"
    return "stable"


def compute_reddit_trends(symbol: str, history_rows: list, periods: tuple = (1, 3, 7, 14, 30)) -> list[dict]:
    """
    Compute multi-period Reddit sentiment trends from historical mention rows.
    history_rows: list of {captured_at, vader_compound, quality_score}
    """
    if not history_rows:
        return []

    from datetime import datetime, timezone

    def _parse(ts: str) -> datetime:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    parsed = []
    for r in history_rows:
        parsed.append({
            "ts": _parse(r.get("captured_at") or r.get("created_at", "")),
            "sentiment": _float(r.get("vader_compound") or r.get("avg_sentiment")),
            "quality": _float(r.get("quality_score")),
        })
    parsed.sort(key=lambda x: x["ts"])

    trends = []
    for days in periods:
        cutoff = now.timestamp() - days * 86400
        window = [p for p in parsed if p["ts"].timestamp() >= cutoff]
        if not window:
            continue
        avg_sent = sum(p["sentiment"] for p in window) / len(window)
        qual_avg = sum(p["quality"] for p in window) / len(window)
        prior_cutoff = now.timestamp() - days * 2 * 86400
        prior = [p for p in parsed if prior_cutoff <= p["ts"].timestamp() < cutoff]
        prior_avg = sum(p["sentiment"] for p in prior) / len(prior) if prior else avg_sent
        trends.append({
            "symbol": symbol.upper(),
            "period_days": days,
            "avg_sentiment": round(avg_sent, 4),
            "mention_count": len(window),
            "quality_avg": round(qual_avg, 3),
            "trend_class": classify_sentiment_trend(avg_sent, prior_avg),
        })
    return trends
