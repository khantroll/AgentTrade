"""Record strategy/sentiment/signal intelligence into SQLite."""

from __future__ import annotations

from agenttrade import db as ledger
from signal_attribution import compute_reddit_trends


def record_candidates(cycle_run_id: int, candidates: list, strategy_name: str = "research") -> None:
    for c in candidates or []:
        ledger.insert_strategy_signal(cycle_run_id, {
            "strategy_name": strategy_name,
            "symbol": c.get("ticker"),
            "signal": "CANDIDATE",
            "confidence": c.get("calibrated_confidence") or c.get("confidence"),
            "score": c.get("total_score") or c.get("score"),
            "reason": c.get("rationale") or c.get("reason"),
            **c,
        })


def record_decisions(cycle_run_id: int, decisions: list, strategy_name: str = "analysis") -> None:
    for d in decisions or []:
        ledger.insert_strategy_signal(cycle_run_id, {
            "strategy_name": strategy_name,
            "symbol": d.get("ticker"),
            "signal": str(d.get("action", "SKIP")).upper(),
            "confidence": d.get("calibrated_confidence") or d.get("confidence"),
            "score": d.get("total_score") or d.get("score"),
            "reason": d.get("rationale"),
            **d,
        })


def record_reddit_sentiment(cycle_run_id: int, universe: list, sources: dict) -> None:
    """Record Reddit intelligence with actual scores (Tier 3)."""
    intel = (sources or {}).get("reddit_intelligence") or {}
    if not intel and int((sources or {}).get("reddit") or 0) <= 0:
        return

    for sym in universe or []:
        sym_u = str(sym).upper()
        detail = intel.get(sym_u) or intel.get(sym)
        if detail:
            ledger.insert_sentiment_score(cycle_run_id, {
                "symbol": sym_u,
                "source": "reddit_vader",
                "score": detail.get("avg_sentiment"),
                "confidence": detail.get("quality_score"),
                "summary": (
                    f"Reddit: {detail.get('mention_count', 0)} mentions, "
                    f"quality={detail.get('quality_score')}, "
                    f"subs={','.join(detail.get('subreddits') or [])}"
                ),
                "reddit_detail": detail,
            })
            for mention in detail.get("mentions") or []:
                ledger.insert_reddit_mention(cycle_run_id, mention)
            _record_reddit_trends(sym_u, detail)
        elif int((sources or {}).get("reddit") or 0) > 0:
            ledger.insert_sentiment_score(cycle_run_id, {
                "symbol": sym_u,
                "source": "reddit_vader",
                "score": None,
                "confidence": None,
                "summary": f"Included via Reddit VADER screener ({sources.get('reddit')} hits)",
            })


def _record_reddit_trends(symbol: str, current_detail: dict) -> None:
    """Persist multi-period sentiment trends."""
    history = []
    for row in ledger.get_reddit_trends(symbol=symbol, limit=200):
        history.append({
            "captured_at": row.get("captured_at"),
            "avg_sentiment": row.get("avg_sentiment"),
            "quality_score": row.get("quality_avg"),
        })
    history.append({
        "captured_at": ledger.utc_now(),
        "avg_sentiment": current_detail.get("avg_sentiment"),
        "quality_score": current_detail.get("quality_score"),
    })
    for trend in compute_reddit_trends(symbol, history):
        ledger.upsert_reddit_sentiment_trend(symbol, trend["period_days"], trend)


def record_signal_attributions(cycle_run_id: int, attribution_map: dict) -> int:
    count = 0
    for sym, row in (attribution_map or {}).items():
        ledger.insert_signal_attribution(cycle_run_id, {
            "symbol": sym,
            "total_score": row.get("total_score"),
            "components": row.get("components"),
            "pipelines": row.get("pipelines"),
            **row,
        })
        count += 1
    return count


def attach_attribution_to_candidates(candidates: list, attribution_map: dict) -> list:
    out = []
    for c in candidates or []:
        sym = str(c.get("ticker") or "").upper()
        attr = attribution_map.get(sym) or {}
        merged = dict(c)
        merged["total_score"] = attr.get("total_score")
        merged["signal_components"] = attr.get("components")
        merged["signal_attribution"] = attr
        out.append(merged)
    return out
