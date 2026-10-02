"""Milestone B: signal strength vs mix, Reddit raw cap, research evidence."""

from pathlib import Path

from buckets import Bucket
from signal_attribution import (
    PRIMARY_PIPELINE_FULL_RANK,
    REDDIT_RAW_CAP,
    STRENGTH_REFERENCE,
    build_component_scores,
    build_screener_evidence,
    rank_symbols_by_strength,
    resolve_signal_mix,
    resolve_signal_strength,
)


ROOT = Path(__file__).resolve().parents[1]


def test_strength_is_not_the_mix_sum():
    weak = build_component_scores(
        "AAA",
        {"Momentum": {"rank": 0, "list_size": 10, "weight": 1.0}},
    )
    strong = build_component_scores(
        "BBB",
        {
            "Momentum": {"rank": 0, "list_size": 10, "weight": 1.0},
            "News Sentiment": {"rank": 0, "list_size": 10, "weight": 2.0},
            "Congress Trades": {"rank": 0, "list_size": 10, "weight": 3.0},
        },
    )

    assert weak["signal_strength"] == 10.0
    assert weak["signal_strength"] != 100
    assert weak["total_score"] == weak["signal_strength"]
    assert sum(weak["signal_mix"].values()) == 100
    assert weak["components"] == weak["signal_mix"]
    assert weak["signal_mix"]["momentum"] == 100

    assert strong["signal_strength"] == 60.0
    assert strong["signal_strength"] != 100
    assert sum(strong["signal_mix"].values()) == 100
    assert strong["signal_strength"] > weak["signal_strength"]

    # Legacy readers of total_score / components follow the split.
    assert resolve_signal_strength({"total_score": 12}) == 12
    assert resolve_signal_strength({"signal_strength": 4, "total_score": 100}) == 4
    assert resolve_signal_mix({"components": {"news_sentiment": 100}})["news_sentiment"] == 100
    assert resolve_signal_mix({"signal_mix": {"congress": 40}, "components": {"congress": 100}})["congress"] == 40


def test_reddit_raw_points_cannot_overwhelm_primary_pipelines():
    assert REDDIT_RAW_CAP == PRIMARY_PIPELINE_FULL_RANK["congress"]
    assert REDDIT_RAW_CAP < sum(PRIMARY_PIPELINE_FULL_RANK.values())
    assert STRENGTH_REFERENCE == sum(PRIMARY_PIPELINE_FULL_RANK.values()) + REDDIT_RAW_CAP

    row = build_component_scores(
        "MEME",
        {
            "Reddit VADER": {"rank": 0, "list_size": 5, "weight": 2.5},
            "Congress Trades": {"rank": 0, "list_size": 5, "weight": 3.0},
            "News Sentiment": {"rank": 0, "list_size": 5, "weight": 2.0},
            "Alpaca Movers": {"rank": 0, "list_size": 5, "weight": 2.0},
            "Momentum": {"rank": 0, "list_size": 5, "weight": 1.0},
        },
        reddit_detail={
            "quality_score": 80,
            "avg_sentiment": 0.9,
            "mention_count": 400,
        },
    )
    assert row["reddit_raw_uncapped"] > REDDIT_RAW_CAP
    assert row["raw_components"]["reddit_sentiment"] <= REDDIT_RAW_CAP
    other = (
        row["raw_components"]["congress"]
        + row["raw_components"]["news_sentiment"]
        + row["raw_components"]["momentum"]
    )
    assert row["raw_components"]["reddit_sentiment"] < other
    # Mix may still say Reddit is a large share, but strength is the capped sum.
    assert row["signal_mix"]["reddit_sentiment"] < 100
    assert row["signal_strength"] == round(sum(row["raw_components"].values()), 1)
    assert row["signal_strength"] < 100 or row["raw_components"]["reddit_sentiment"] == REDDIT_RAW_CAP


def test_rank_and_fallback_use_strength():
    from agents.screener_fallback import screener_fallback_candidates

    attribution = {
        "WEAK": build_component_scores("WEAK", {"Momentum": {"rank": 5, "list_size": 10, "weight": 1.0}}),
        "STRONG": build_component_scores(
            "STRONG",
            {
                "Congress Trades": {"rank": 0, "list_size": 4, "weight": 3.0},
                "News Sentiment": {"rank": 0, "list_size": 4, "weight": 2.0},
            },
        ),
        "TIEB": {"signal_strength": 10.0, "signal_mix": {"momentum": 100}, "components": {"momentum": 100}},
        "TIEA": {"signal_strength": 10.0, "signal_mix": {"momentum": 100}, "components": {"momentum": 100}},
    }
    scored = {"WEAK": 50.0, "STRONG": 1.0, "TIEA": 3.0, "TIEB": 9.0}
    ranked = rank_symbols_by_strength(scored, attribution, limit=4)
    assert ranked[0] == "STRONG"
    assert ranked.index("TIEB") < ranked.index("TIEA")

    universe = ["WEAK", "TIEA", "TIEB", "STRONG"]
    rows = screener_fallback_candidates(universe, {"attribution": attribution}, "Growth", top_n=2)
    assert [r["ticker"] for r in rows] == ["STRONG", "TIEA"]
    assert rows[0]["screener_rank"] == 4
    assert rows[0]["signal_strength"] == attribution["STRONG"]["signal_strength"]

    tied = screener_fallback_candidates(
        ["TIEA", "TIEB"],
        {"attribution": attribution},
        "Growth",
        top_n=5,
    )
    assert [r["ticker"] for r in tied] == ["TIEA", "TIEB"]


def test_research_prompt_includes_screener_evidence(monkeypatch):
    import agents.research as research

    captured = {}

    def fake_query(prompt, agent_tag=None):
        captured["prompt"] = prompt
        captured["tag"] = agent_tag
        return {"selected": [{"ticker": "AAPL", "reason": "aligned with congress hit", "confidence": 0.8}]}

    monkeypatch.setattr(research, "query_research", fake_query)
    monkeypatch.setattr(research, "budget_exhausted", lambda: False)
    monkeypatch.setattr(research.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(research, "fetch_stock_data", lambda ticker: {"symbol": ticker, "current_price": 20})

    attr = build_component_scores(
        "AAPL",
        {"Congress Trades": {"rank": 0, "list_size": 3, "weight": 3.0}},
    )
    sources = {
        "attribution": {"AAPL": attr, "MSFT": {"signal_strength": 8, "signal_mix": {"momentum": 100}}},
        "pipeline_membership": {
            "MSFT": {"Momentum": {"rank": 1, "list_size": 8, "weight": 1.0}},
        },
    }
    bucket = Bucket(name="Growth", allocation_pct=0.45, mode="growth")
    outcome = research.research_agent(["MSFT", "AAPL"], bucket, sources=sources)

    assert outcome.status == "ok"
    prompt = captured["prompt"]
    assert "signal_strength" in prompt
    assert "signal_mix" in prompt
    assert "Congress Trades" in prompt
    assert "screener_rank" in prompt
    assert "AAPL" in prompt and "MSFT" in prompt
    evidence = build_screener_evidence(["MSFT", "AAPL"], sources)
    assert evidence[0]["ticker"] == "MSFT"
    assert evidence[0]["screener_rank"] == 1
    assert evidence[0]["pipeline_hits"]["Momentum"]["rank"] == 1
    assert evidence[1]["signal_strength"] == attr["signal_strength"]
    assert "do not invent pipeline hits" in prompt


def test_margin_weakness_follows_strength_not_mix():
    from margin_correction import _signal_weakness

    noisy = _signal_weakness("MEME", {
        "MEME": {
            "signal_strength": 10,
            "total_score": 10,
            "signal_mix": {"reddit_sentiment": 100},
            "components": {"reddit_sentiment": 100},
        },
    }, {})
    backed = _signal_weakness("FIRM", {
        "FIRM": {
            "signal_strength": 90,
            "signal_mix": {"congress": 60, "news_sentiment": 40},
            "components": {"congress": 60, "news_sentiment": 40},
        },
    }, {})
    assert noisy > backed
    assert _signal_weakness("NONE", {}, {}) == 0.55


def test_host_scripts_export_env_and_flock_exit_is_distinct():
    cycle = (ROOT / "run_cycle.sh").read_text()
    monitor = (ROOT / "monitor.sh").read_text()
    assert "set -a" in cycle
    assert 'source "$APP_DIR/.env"' in cycle
    python_cycle = cycle.index("from agent import run_trading_cycle")
    assert cycle.index("set -a") < python_cycle
    assert "-E 75" in cycle
    assert "flock busy" in cycle
    assert "not a flock skip" in cycle
    assert "set -a" in monitor
    assert 'source "$APP_DIR/.env"' in monitor
    assert monitor.index("set -a") < monitor.index("monitor_positions")
