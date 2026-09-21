"""Signal effectiveness CLI report (Tier 4)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from agenttrade import db as ledger
from signal_performance import compute_signal_scorecards

log = logging.getLogger(__name__)

HORIZONS = (1, 3, 7, 30)


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_signal_report(days: int = 30) -> dict:
    ledger.init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    scorecards = compute_signal_scorecards(max_signals=500)
    cards = scorecards.get("scorecards") or {}

    if not cards:
        return {"ok": False, "message": "insufficient data", "days": days}

    outcomes = ledger.get_signal_outcomes_for_scorecard(limit=1000)
    recent = [o for o in outcomes if True]  # outcomes table lacks date filter on join; use all

    def _filter_component(key: str, min_weight: float = 0.0) -> list:
        out = []
        for o in recent:
            try:
                import json as _json
                comps = _json.loads(o.get("components_json") or "{}")
            except Exception:
                comps = {}
            if _float(comps.get(key)) > min_weight:
                out.append(o)
        return out

    def _stats(rows: list) -> dict:
        if not rows:
            return {"count": 0, "win_rate_pct": None, "avg_return_pct": None, "avg_drawdown_pct": None}
        wins = sum(1 for r in rows if _float(r.get("price_change_pct")) > 0)
        rets = [_float(r.get("price_change_pct")) for r in rows]
        dds = [_float(r.get("max_drawdown_pct")) for r in rows]
        n = len(rows)
        return {
            "count": n,
            "win_rate_pct": round(100 * wins / n, 1),
            "avg_return_pct": round(sum(rets) / n, 2),
            "avg_drawdown_pct": round(sum(dds) / n, 2),
        }

    reddit_only = _stats(_filter_component("reddit_sentiment", 10))
    reddit_momentum_rows = []
    for o in recent:
        try:
            comps = json.loads(o.get("components_json") or "{}")
        except json.JSONDecodeError:
            comps = {}
        if _float(comps.get("reddit_sentiment")) > 5 and _float(comps.get("momentum")) > 5:
            reddit_momentum_rows.append(o)
    reddit_momentum = _stats(reddit_momentum_rows)

    high_conf = []
    low_conf = []
    for o in recent:
        c = _float(o.get("calibrated_confidence"))
        if c >= 0.65:
            high_conf.append(o)
        elif c > 0:
            low_conf.append(o)

    horizon_returns = {}
    with ledger.get_connection() as conn:
        for h in HORIZONS:
            rows = conn.execute(
                """
                SELECT AVG(price_change_pct) AS avg_ret, AVG(max_drawdown_pct) AS avg_dd, COUNT(*) AS cnt
                FROM signal_outcomes WHERE horizon_days=?
                """,
                (h,),
            ).fetchone()
            if rows and rows["cnt"]:
                horizon_returns[f"{h}d"] = {
                    "avg_return_pct": round(_float(rows["avg_ret"]), 2),
                    "avg_drawdown_pct": round(_float(rows["avg_dd"]), 2),
                    "count": rows["cnt"],
                }
            else:
                horizon_returns[f"{h}d"] = "insufficient data"

    sources = list(cards.items())
    best = max(sources, key=lambda x: x[1].get("win_rate_pct") or 0, default=(None, {}))
    worst = min(sources, key=lambda x: x[1].get("win_rate_pct") or 100, default=(None, {}))

    return {
        "ok": True,
        "days": days,
        "best_signal_source": {"name": best[0], **best[1]} if best[0] else "insufficient data",
        "worst_signal_source": {"name": worst[0], **worst[1]} if worst[0] else "insufficient data",
        "reddit_only": reddit_only,
        "reddit_plus_momentum": reddit_momentum,
        "reddit_plus_fundamentals": _stats(_filter_component("fundamentals", 5)),
        "high_confidence": _stats(high_conf),
        "low_confidence": _stats(low_conf),
        "combined_signals": cards.get("combined_signals", "insufficient data"),
        "scorecards_by_source": cards,
        "horizon_returns": horizon_returns,
        "reddit_note": "Reddit weights unchanged — measurement only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signal effectiveness report")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build_signal_report(days=args.days)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        if not report.get("ok"):
            print(report.get("message", "insufficient data"))
            return 1
        print(f"Signal report ({args.days}d)")
        best = report.get("best_signal_source") or {}
        print(f"  Best source: {best.get('name')} — {best.get('win_rate_pct')}% win, {best.get('avg_gain_pct')}% avg")
        ro = report.get("reddit_only") or {}
        print(f"  Reddit-only: {ro.get('win_rate_pct')}% win ({ro.get('count')} signals)")
        for h, v in (report.get("horizon_returns") or {}).items():
            print(f"  {h}: {v}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
