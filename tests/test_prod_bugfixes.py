"""Regression tests for Gemini JSON parsing, rebalance log formatting, and state size."""

import json

import pytest

SAMPLE = {
    "selected": [
        {"ticker": "AAPL", "reason": "Strong momentum", "confidence": 0.82},
        {"ticker": "MSFT", "reason": "Steady trend", "confidence": 0.71},
    ]
}


def test_parse_raw_gemini_json_unchanged():
    from llm_router import _parse_json

    raw = json.dumps(SAMPLE)
    assert _parse_json(raw) == SAMPLE


def test_parse_strips_here_is_the_json_preamble():
    from llm_router import _parse_json

    text = "Here is the JSON requested\n" + json.dumps(SAMPLE)
    assert _parse_json(text) == SAMPLE


def test_parse_strips_json_code_fence():
    from llm_router import _parse_json

    text = "```json\n" + json.dumps(SAMPLE, indent=2) + "\n```"
    assert _parse_json(text) == SAMPLE


def test_parse_strips_preamble_and_fence_with_nested_objects():
    from llm_router import _parse_json

    text = "Here is the JSON requested\n```json\n" + json.dumps(SAMPLE, indent=2) + "\n```"
    parsed = _parse_json(text)
    assert [row["ticker"] for row in parsed["selected"]] == ["AAPL", "MSFT"]
    assert parsed["selected"][0]["confidence"] == 0.82


def test_parse_ignores_braces_on_the_preamble_line():
    from llm_router import _parse_json

    text = 'Here is the JSON requested {"selected": []}\n' + json.dumps(SAMPLE)
    assert _parse_json(text)["selected"][0]["ticker"] == "AAPL"


def test_parse_same_line_preamble_keeps_clean_object():
    from llm_router import _parse_json

    text = "Here is the JSON requested: " + json.dumps(SAMPLE)
    assert _parse_json(text) == SAMPLE


def test_parse_uppercase_fence_and_trailing_comma():
    from llm_router import _parse_json

    text = '```JSON\n{"selected": [{"ticker": "NVDA", "reason": "AI demand", "confidence": 0.9,}]}\n```'
    parsed = _parse_json(text)
    assert parsed["selected"][0]["ticker"] == "NVDA"
    assert parsed["selected"][0]["confidence"] == 0.9


def test_percent_format_comma_flag_is_unsupported_and_helper_is_not():
    from cycle import format_signed_drift

    with pytest.raises(ValueError, match="unsupported format character"):
        ("drift=$%+,.0f" % 1200.4)

    assert format_signed_drift(1200.4) == "+1,200"
    assert format_signed_drift(-8900) == "-8,900"
    assert format_signed_drift(0) == "+0"
    message = "  %s: target=%s%% current=%s%% drift=$%s → %s" % (
        "Growth",
        40,
        35,
        format_signed_drift(1200.4),
        "ADD",
    )
    assert message == "  Growth: target=40% current=35% drift=$+1,200 → ADD"


def test_projection_cap_defaults_to_8_mib(monkeypatch):
    monkeypatch.delenv("AGENT_STATE_MAX_BYTES", raising=False)
    from account_sync import DEFAULT_AGENT_STATE_MAX_BYTES, agent_state_max_bytes

    assert DEFAULT_AGENT_STATE_MAX_BYTES == 8 * 1024 * 1024
    assert agent_state_max_bytes() == 8 * 1024 * 1024


def test_publish_writes_small_projection(tmp_path):
    import agent_config as cfg
    from agenttrade.publish import publish_dashboard_state

    publish_dashboard_state({"trade_candidates": [{"ticker": "AAPL"}], "blocked_ideas": []})
    private = json.loads((tmp_path / "agent_state.json").read_text())
    public = json.loads((tmp_path / "public" / "agent_state.json").read_text())
    assert private == public
    assert private["trade_candidates"][0]["ticker"] == "AAPL"
    assert cfg.STATE_FILE.endswith("agent_state.json")


def test_publish_refuses_oversized_projection_and_leaves_sqlite(monkeypatch, tmp_path, _isolate_agenttrade_runtime):
    monkeypatch.setenv("AGENT_STATE_MAX_BYTES", "64")
    db_path = _isolate_agenttrade_runtime
    db_path.write_bytes(b"sqlite-sentinel")
    sentinel = '{"keep": true}'
    state_path = tmp_path / "agent_state.json"
    public = tmp_path / "public" / "agent_state.json"
    public.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(sentinel)
    public.write_text(sentinel)

    from agenttrade.publish import publish_dashboard_state

    publish_dashboard_state({"blob": "x" * 400, "trade_candidates": [{"ticker": "AAPL"}]})
    assert state_path.read_text() == sentinel
    assert public.read_text() == sentinel
    assert db_path.read_bytes() == b"sqlite-sentinel"
