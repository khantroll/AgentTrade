"""Structured signal score breakdown per candidate (Tier 3).

Two different numbers are published for every symbol:

* ``signal_mix`` — relative composition of which signals contributed.
  Components are scaled to sum to 100. A one-pipeline name is 100% that
  pipeline even when the hit is weak.
* ``signal_strength`` — conviction magnitude from the raw points. Weak and
  strong names are not forced onto the same 100-point total.

``components`` remains an alias of ``signal_mix`` and ``total_score`` remains
an alias of ``signal_strength`` so older readers keep working. New readers
should prefer the explicit names via ``resolve_signal_mix`` and
``resolve_signal_strength``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# Pipeline weights mirror screener merge (reddit remains first-class at 2.5
# on the ensemble merge). Attribution raw points use the same weights, then
# the Reddit cap below stops quality bonuses from outrunning the other pipelines.
PIPELINE_WEIGHTS = {
    "momentum": 1.0,
    "news_sentiment": 2.0,
    "congress": 3.0,
    "reddit_sentiment": 2.5,
    "technicals": 2.0,
    "fundamentals": 1.5,
    "volume": 1.0,
}

# One full-rank pipeline hit contributes ``weight * PIPELINE_HIT_SCALE``.
PIPELINE_HIT_SCALE = 10.0

# Alpaca movers is its own screener pipeline (merge weight 2.0) even though
# attribution stores that hit on the momentum component.
MOVERS_PIPELINE_WEIGHT = 2.0

# Full-rank raw points for the pipelines Reddit must not overwhelm.
# congress 30 + news 20 + movers 20 + momentum 10 = 80.
PRIMARY_PIPELINE_FULL_RANK = {
    "congress": PIPELINE_WEIGHTS["congress"] * PIPELINE_HIT_SCALE,
    "news_sentiment": PIPELINE_WEIGHTS["news_sentiment"] * PIPELINE_HIT_SCALE,
    "movers": MOVERS_PIPELINE_WEIGHT * PIPELINE_HIT_SCALE,
    "momentum": PIPELINE_WEIGHTS["momentum"] * PIPELINE_HIT_SCALE,
}

# Reddit bound, applied to raw ``reddit_sentiment`` points BEFORE mix
# normalization. A full-rank Congress hit is the ceiling (weight 3 * 10 = 30).
# Uncapped Reddit quality bonuses (quality * 8 + sentiment * 12 + mentions)
# can reach hundreds of points and drown congress, news, movers, and momentum.
# After this cap, Reddit can match Congress at best and stays strictly below
# the combined primary-pipeline ceiling (80).
REDDIT_RAW_CAP = PRIMARY_PIPELINE_FULL_RANK["congress"]

# Reference magnitude for gates that need a 0–1 weakness/support scale.
# A name at this strength has a full-rank hit on every primary pipeline plus
# Reddit at its cap: 80 + 30 = 110. Strength is not clamped to this value.
STRENGTH_REFERENCE = sum(PRIMARY_PIPELINE_FULL_RANK.values()) + REDDIT_RAW_CAP

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
    """Scale raw points to a composition that sums to ``cap`` (the mix)."""
    total_raw = sum(raw.values())
    if total_raw <= 0:
        return {k: 0.0 for k in COMPONENT_KEYS}
    scale = cap / total_raw
    return {k: round(raw.get(k, 0.0) * scale, 1) for k in COMPONENT_KEYS}


def _apply_reddit_cap(raw: dict[str, float]) -> float:
    """Cap Reddit raw points in place. Returns the pre-cap value."""
    before = _float(raw.get("reddit_sentiment"))
    raw["reddit_sentiment"] = round(min(before, REDDIT_RAW_CAP), 4)
    return before


def resolve_signal_strength(row: Optional[dict]) -> Optional[float]:
    """Conviction magnitude for ranking and gates.

    Prefers ``signal_strength``. Legacy rows that only stored ``total_score``
    still resolve: new writers put strength in that field, older rows stored
    the mix sum (often 100).
    """
    if not isinstance(row, dict):
        return None
    if row.get("signal_strength") is not None:
        return _float(row.get("signal_strength"))
    if row.get("total_score") is not None:
        return _float(row.get("total_score"))
    return None


def resolve_signal_mix(row: Optional[dict]) -> dict[str, float]:
    """Relative composition. Prefers ``signal_mix``, then ``components``."""
    if not isinstance(row, dict):
        return {}
    for key in ("signal_mix", "components", "signal_components"):
        mix = row.get(key)
        if isinstance(mix, dict) and mix:
            return {str(k): _float(v) for k, v in mix.items()}
    return {}


def attribution_view(row: Optional[dict]) -> dict:
    """Normalize a stored attribution row, including ``raw_json`` payloads.

    ``total_score`` is rewritten to strength and ``components`` to mix so
    callers that still read the old names follow the split.
    """
    merged = dict(row or {})
    raw_payload = merged.get("raw_json")
    parsed = None
    if isinstance(raw_payload, str) and raw_payload:
        try:
            parsed = json.loads(raw_payload)
        except json.JSONDecodeError:
            parsed = None
    elif isinstance(raw_payload, dict):
        parsed = raw_payload
    if isinstance(parsed, dict):
        for key in (
            "signal_strength",
            "signal_mix",
            "total_score",
            "components",
            "pipelines",
            "reddit_detail",
            "raw_components",
        ):
            current = merged.get(key)
            incoming = parsed.get(key)
            if current in (None, {}, []) and incoming not in (None, {}, []):
                merged[key] = incoming
    strength = resolve_signal_strength(merged)
    mix = resolve_signal_mix(merged)
    merged["signal_strength"] = strength
    merged["signal_mix"] = mix
    if strength is not None:
        merged["total_score"] = strength
    if mix:
        merged["components"] = mix
    return merged


def rank_symbols_by_strength(
    scored: dict,
    attribution: Optional[dict],
    limit: int,
) -> list[str]:
    """Order symbols by signal strength, then ensemble merge score.

    ``scored`` is the screener merge magnitude (including the dividend-quality
    boost). It breaks ties and covers symbols that have no attribution row.
    """
    attribution = attribution or {}

    def sort_key(sym: str):
        strength = resolve_signal_strength(attribution.get(sym) or {})
        if strength is None:
            strength = _float(scored.get(sym))
        return (-float(strength), -_float(scored.get(sym)), sym)

    ordered = sorted((scored or {}).keys(), key=sort_key)
    if limit is None:
        return ordered
    return ordered[: max(0, int(limit))]


def build_screener_evidence(universe: list, sources: Optional[dict] = None) -> list[dict]:
    """Compact per-ticker ensemble facts for the research prompt.

    Ranks follow ``universe`` order. Strength and mix come from the attribution
    built in the same screener call when it is present.
    """
    sources = sources or {}
    attribution = sources.get("attribution") if isinstance(sources.get("attribution"), dict) else {}
    membership = sources.get("pipeline_membership") if isinstance(sources.get("pipeline_membership"), dict) else {}
    rows = []
    for index, raw in enumerate(universe or []):
        ticker = str(raw or "").upper().strip()
        if not ticker:
            continue
        attr = attribution.get(ticker) or attribution.get(raw) or {}
        pipelines = attr.get("pipelines") if isinstance(attr.get("pipelines"), dict) else {}
        if not pipelines:
            fallback = membership.get(ticker) or membership.get(raw) or {}
            pipelines = fallback if isinstance(fallback, dict) else {}
        hits = {}
        for name, meta in pipelines.items():
            if isinstance(meta, dict):
                hits[str(name)] = {
                    "rank": meta.get("rank"),
                    "list_size": meta.get("list_size"),
                    "weight": meta.get("weight"),
                }
            else:
                hits[str(name)] = meta
        rows.append({
            "ticker": ticker,
            "screener_rank": index + 1,
            "signal_strength": resolve_signal_strength(attr),
            "signal_mix": resolve_signal_mix(attr),
            "pipeline_hits": hits,
        })
    return rows


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
        raw[key] += weight * _rank_bonus(rank, size) * PIPELINE_HIT_SCALE

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

    reddit_uncapped = _apply_reddit_cap(raw)
    # Mix is composition (sums to ~100). Strength is the capped raw magnitude
    # and is intentionally not rescaled onto 100.
    signal_mix = _normalize_components(raw)
    signal_strength = round(sum(raw.values()), 1)

    return {
        "symbol": sym,
        "signal_strength": signal_strength,
        "signal_mix": signal_mix,
        "total_score": signal_strength,
        "components": signal_mix,
        "raw_components": {k: round(_float(raw.get(k)), 4) for k in COMPONENT_KEYS},
        "reddit_raw_uncapped": round(reddit_uncapped, 4),
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
