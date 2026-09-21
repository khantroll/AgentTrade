from analysis_funnel import partition_for_analysis, trim_research_selections


def test_trim_sorts_and_caps_preserving_original_rows():
    rows = [
        {"ticker": "aapl", "confidence": 0.60},
        {"ticker": "AAPL", "confidence": 0.80},
        {"ticker": "msft", "confidence": 0.70},
    ]
    out = trim_research_selections(rows, 2)
    # Historical contract: confidence sort/cap only; no normalization or de-duplication.
    assert [r["ticker"] for r in out] == ["AAPL", "msft"]
    assert out[0]["confidence"] == 0.80


def test_partition_preserves_historical_skip_reasons():
    rows = [
        {"ticker": "A", "confidence": 0.90},
        {"ticker": "B", "confidence": 0.80},
        {"ticker": "C", "confidence": 0.70},
        {"ticker": "D", "confidence": 0.40},
    ]
    skipped, analyze = partition_for_analysis(rows, 0.55, 2, "Growth")
    assert [r["ticker"] for r in analyze] == ["A", "B"]
    reasons = {r["ticker"]: r["rationale"] for r in skipped}
    assert reasons["C"] == "Below top-2 analysis cutoff (conf 0.70)"
    assert reasons["D"] == "Low research confidence (0.40)"
    assert all(r["action"] == "SKIP" for r in skipped)
    assert all(r["bucket"] == "Growth" for r in skipped)
