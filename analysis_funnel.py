"""analysis_funnel.py — Pure research→analysis funnel splits (no LLM / config imports)."""


def partition_for_analysis(
    candidates: list,
    min_confidence: float,
    max_analysis: int,
    bucket_name: str,
) -> tuple[list, list]:
    """
    Split research candidates into skip decisions (no LLM) and to_analyze (LLM).

    Returns (skip_decisions, to_analyze).
    """
    skip_decisions = []
    qualified = []

    for candidate in candidates:
        ticker = candidate["ticker"]
        conf = float(candidate.get("confidence", 0.5))
        if conf < min_confidence:
            skip_decisions.append({
                "ticker": ticker,
                "action": "SKIP",
                "rationale": f"Low research confidence ({conf:.2f})",
                "skip_reason": "low_research_confidence",
                "blocked_reason": "low_research_confidence",
                "bucket": bucket_name,
            })
            continue
        qualified.append(candidate)

    qualified.sort(key=lambda c: float(c.get("confidence", 0.5)), reverse=True)
    to_analyze = qualified[:max_analysis]
    deferred = qualified[max_analysis:]

    for candidate in deferred:
        conf = float(candidate.get("confidence", 0.5))
        skip_decisions.append({
            "ticker": candidate["ticker"],
            "action": "SKIP",
            "rationale": f"Below top-{max_analysis} analysis cutoff (conf {conf:.2f})",
            "skip_reason": "below_analysis_cutoff",
            "blocked_reason": "below_analysis_cutoff",
            "bucket": bucket_name,
        })

    return skip_decisions, to_analyze


def trim_research_selections(selected: list, top_n: int) -> list:
    """Keep highest-confidence research picks up to top_n."""
    return sorted(
        selected,
        key=lambda s: float(s.get("confidence", 0)),
        reverse=True,
    )[:top_n]
