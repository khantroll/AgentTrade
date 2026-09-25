"""Gemini research replies: preamble and ```json fences must still parse."""

import json

from llm_router import _parse_json, _sanitize_llm_json_text

RAW = """{
  "selected": [
    {"ticker": "AAPL", "reason": "Momentum above the 20-day average", "confidence": 0.82},
    {"ticker": "MSFT", "reason": "Cloud growth with steady RSI", "confidence": 0.74}
  ]
}"""


def _tickers(text):
    parsed = _parse_json(text)
    assert isinstance(parsed, dict)
    return [row["ticker"] for row in parsed["selected"]]


def test_raw_json_unchanged():
    parsed = _parse_json(RAW)
    assert parsed == json.loads(RAW)
    assert _tickers(RAW) == ["AAPL", "MSFT"]
    assert json.loads(_sanitize_llm_json_text(RAW)) == json.loads(RAW)


def test_preamble_before_json():
    text = "Here is the JSON requested:\n" + RAW
    assert _tickers(text) == ["AAPL", "MSFT"]
    assert json.loads(_sanitize_llm_json_text(text))["selected"][0]["confidence"] == 0.82


def test_preamble_with_trailing_prose_braces():
    text = "Here is the JSON requested:\n" + RAW + "\nHope this helps (confidence is in {0,1}).\n"
    assert _tickers(text) == ["AAPL", "MSFT"]


def test_fenced_json():
    text = "```json\n" + RAW + "\n```"
    assert _tickers(text) == ["AAPL", "MSFT"]
    assert json.loads(_sanitize_llm_json_text(text))["selected"][1]["ticker"] == "MSFT"


def test_preamble_fence_and_trailing_prose_with_braces():
    text = (
        "Here is the JSON requested:\n"
        "```json\n" + RAW + "\n```\n"
        "Hope this helps (confidence is in {0,1}).\n"
    )
    assert _tickers(text) == ["AAPL", "MSFT"]


def test_preamble_schema_example_then_real_json():
    text = (
        'Here is the JSON requested (shape {"selected": [...]}):\n'
        + RAW
    )
    assert _tickers(text) == ["AAPL", "MSFT"]


def test_single_quotes_and_trailing_comma_still_parse():
    text = "{'selected': [{'ticker': 'NVDA', 'reason': 'gpu', 'confidence': 0.9},]}"
    assert _tickers(text) == ["NVDA"]
