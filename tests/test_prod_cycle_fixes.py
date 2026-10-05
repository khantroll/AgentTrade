"""Production fixes: research JSON recovery, Alpaca movers request, Chicago trade cap."""

import json
import logging
from datetime import datetime, timezone

import pytest


SAMPLE = {
    "selected": [
        {"ticker": "AAPL", "reason": "Strong momentum", "confidence": 0.82},
    ]
}

# 10:30 CT on 5 Oct 2026 is 15:30 UTC. Chicago is still CDT (UTC-5).
CYCLE_NOW = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)


def test_parse_strips_think_block_preamble_and_fence():
    from llm_router import _parse_json

    text = (
        "<think>\nThe screener likes AAPL. I should answer with JSON only.\n</think>\n"
        "Here is the JSON requested\n```json\n"
        + json.dumps(SAMPLE)
        + "\n```"
    )
    assert _parse_json(text) == SAMPLE


def test_parse_strips_unclosed_think_before_json():
    from llm_router import _parse_json

    text = (
        "<think>\nStill writing the rationale.\n"
        + json.dumps(SAMPLE)
    )
    parsed = _parse_json(text)
    assert parsed["selected"][0]["ticker"] == "AAPL"
    assert parsed["selected"][0]["confidence"] == 0.82


def test_parse_think_without_json_fails_closed():
    from llm_router import _parse_json

    assert _parse_json("<think>I cannot decide.</think>\nSorry, no picks.") is None
    assert _parse_json("<think>no object here") is None


def _json_mode_error(draft: str, code: str = "json_validate_failed") -> str:
    return json.dumps({
        "error": {
            "message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
            "type": "invalid_request_error",
            "code": code,
            "failed_generation": draft,
        }
    })


class _Resp:
    def __init__(self, status, body, usage=None):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self._usage = usage

    def json(self):
        if self.status_code < 400 and self._usage is not None:
            payload = json.loads(self.text)
            payload["usage"] = self._usage
            return payload
        return json.loads(self.text)


def test_json_mode_error_recovers_think_preamble_without_second_call(monkeypatch):
    import llm_router

    draft = (
        "<think>\nQwen reasoning before the object.\n</think>\n"
        "Here is the JSON requested:\n"
        + json.dumps({"selected": [{"ticker": "SOL/USD", "reason": "trend", "confidence": 0.7}]})
    )
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _Resp(400, _json_mode_error(draft))

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    monkeypatch.setattr(llm_router, "_record_usage", lambda *a, **k: None)
    text = llm_router._call_chat_endpoint(
        "mistral", "some-model", "pick json", 200, "research",
        "https://example.invalid/v1/chat/completions", "test-key",
    )
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    parsed = llm_router._parse_json(text)
    assert parsed["selected"][0]["ticker"] == "SOL/USD"
    assert parsed["selected"][0]["confidence"] == 0.7


def test_json_mode_error_retries_once_without_response_format(monkeypatch):
    import llm_router

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _Resp(400, _json_mode_error("<think>no json yet</think>"))
        body = {
            "choices": [{
                "message": {
                    "content": "<think>done</think>\n" + json_dumps_selected(),
                }
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }
        return _Resp(200, body, usage=body["usage"])

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    monkeypatch.setattr(llm_router, "_record_usage", lambda *a, **k: None)
    text = llm_router._call_chat_endpoint(
        "groq", "qwen/qwen3.8-27b", "pick json", 200, "research",
        "https://example.invalid/v1/chat/completions", "test-key",
    )
    assert len(calls) == 2
    assert "response_format" not in calls[1]
    assert "Output exactly one JSON object." in calls[1]["messages"][-1]["content"]
    assert llm_router._parse_json(text)["selected"][0]["ticker"] == "NVDA"


def json_dumps_selected():
    return json.dumps({"selected": [{"ticker": "NVDA", "reason": "demand", "confidence": 0.9}]})


def test_unrecoverable_json_mode_error_raises_after_one_retry(monkeypatch):
    import llm_router

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _Resp(400, _json_mode_error("I think we should buy NVDA but I will not emit JSON."))

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    monkeypatch.setattr(llm_router, "_record_usage", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="json_validate_failed"):
        llm_router._call_chat_endpoint(
            "groq", "qwen/qwen3.8-27b", "pick json", 200, "research",
            "https://example.invalid/v1/chat/completions", "test-key",
        )
    assert len(calls) == 2
    assert "response_format" not in calls[1]


def test_non_json_http_400_does_not_retry(monkeypatch):
    import llm_router

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _Resp(400, {"error": {"message": "model not found", "code": "model_not_found"}})

    monkeypatch.setattr(llm_router.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="model not found"):
        llm_router._call_chat_endpoint(
            "groq", "missing", "pick json", 200, "research",
            "https://example.invalid/v1/chat/completions", "test-key",
        )
    assert calls == [1]


def test_query_research_fails_closed_when_provider_cannot_parse(monkeypatch):
    import llm_router

    monkeypatch.setattr(llm_router, "_effective_mode", lambda: "groq_qwen")
    monkeypatch.setattr(llm_router, "_pick_model", lambda *a, **k: ("groq", "qwen-test"))
    monkeypatch.setattr(llm_router, "_provider_ready", lambda *a, **k: True)
    monkeypatch.setattr(llm_router, "_model_is_suppressed", lambda *a, **k: False)
    monkeypatch.setattr(llm_router, "_fallback_models", lambda *a, **k: [])
    monkeypatch.setattr(llm_router, "_classify_cooldown", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("groq HTTP 400: json_validate_failed")

    monkeypatch.setattr(llm_router, "_call_provider", boom)
    result = llm_router.query_research("pick", agent_tag="growth_research")
    assert result["research_status"] == "failed"
    assert result["selected"] == []
    assert result["research_reason"] == "research_failed"


def test_movers_request_uses_path_market_type_and_top_only(monkeypatch):
    import screener

    captured = {}

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "gainers": [
                    {"symbol": "NVDA", "price": 120.0, "change": 2, "percent_change": 1.5},
                    {"symbol": "SPY", "price": 500, "change": 1, "percent_change": 0.2},
                ],
                "losers": [],
                "market_type": "stocks",
            }

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = dict(params or {})
        captured["headers"] = headers
        return Resp()

    monkeypatch.setattr(screener, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(screener, "ALPACA_SECRET", "s")
    monkeypatch.setattr(screener.requests, "get", fake_get)
    assert screener.alpaca_movers_screen(top_n=15) == ["NVDA"]
    assert captured["url"].endswith("/v1beta1/screener/stocks/movers")
    assert captured["params"] == {"top": 15}
    assert "market_type" not in captured["params"]
    assert "feed" not in captured["params"]
    assert screener.movers_top_param(100) == 50
    assert screener.movers_top_param(0) == 1


def test_movers_data_plan_denial_is_one_log_line(monkeypatch, caplog):
    import screener

    class Resp:
        status_code = 403
        text = '{"message":"subscription does not permit querying recent SIP data"}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr(screener, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(screener, "ALPACA_SECRET", "s")
    monkeypatch.setattr(screener.requests, "get", lambda *a, **k: Resp())
    with caplog.at_level(logging.WARNING):
        assert screener.alpaca_movers_screen() == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "data plan" in warnings[0].lower()
    assert "HTTP 403" in warnings[0]


def test_movers_invalid_request_is_one_short_warning(monkeypatch, caplog):
    import screener

    class Resp:
        status_code = 400
        text = '{"message":"unexpected query parameter: market_type"}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr(screener, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(screener, "ALPACA_SECRET", "s")
    monkeypatch.setattr(screener.requests, "get", lambda *a, **k: Resp())
    with caplog.at_level(logging.WARNING):
        assert screener.alpaca_movers_screen() == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "HTTP 400" in warnings[0]
    assert "data plan" not in warnings[0].lower()


def test_recent_fills_keep_activity_id_separate_from_order_id(monkeypatch):
    import alpaca_client

    monkeypatch.setattr(alpaca_client, "alpaca_get", lambda endpoint: [{
        "id": "202610051500::act",
        "order_id": "ord-real",
        "symbol": "SOL/USD",
        "side": "buy",
        "qty": "1",
        "price": "140",
        "transaction_time": "2026-10-05T15:00:00Z",
    }])
    row = alpaca_client.get_recent_fills(5)[0]
    assert row["id"] == "202610051500::act"
    assert row["order_id"] == "ord-real"
    assert row["filled_at"] == "2026-10-05T15:00:00Z"


def test_chicago_day_collapses_partials_and_ignores_non_trades():
    from trading_day import count_trades_for_day

    fills = [
        {"id": "act-1", "order_id": "ord-1", "submitted_at": "2026-10-05T15:00:00Z", "status": "filled"},
        {"id": "act-2", "order_id": "ord-1", "submitted_at": "2026-10-05T15:01:00Z", "status": "filled"},
        {"id": "act-prev", "order_id": "ord-prev", "submitted_at": "2026-10-05T03:00:00Z", "status": "filled"},
    ]
    open_orders = [
        {
            "id": "ord-1",
            "status": "partially_filled",
            "side": "buy",
            "submitted_at": "2026-10-05T15:00:00Z",
        },
        {"id": "ord-cancel", "status": "canceled", "side": "buy", "submitted_at": "2026-10-05T15:10:00Z"},
        {"id": "ord-fail", "status": "rejected", "side": "buy", "submitted_at": "2026-10-05T15:11:00Z"},
        {"id": "ord-skip", "status": "skipped", "side": "buy", "submitted_at": "2026-10-05T15:12:00Z"},
        {
            "id": "ord-stop",
            "status": "new",
            "side": "sell",
            "type": "stop",
            "order_class": "bracket",
            "submitted_at": "2026-10-05T15:00:00Z",
        },
        {"id": "ord-new", "status": "accepted", "side": "buy", "submitted_at": "2026-10-05T15:20:00Z"},
    ]
    assert count_trades_for_day(fills, open_orders, now=CYCLE_NOW) == 2


def test_sqlite_fill_count_uses_chicago_day_and_distinct_orders():
    from agenttrade import db

    db.init_db()
    db.insert_fills_from_alpaca(0, [
        {
            "id": "act-1", "order_id": "order-aaa", "symbol": "AAPL", "side": "buy",
            "qty": 1, "price": 10, "transaction_time": "2026-10-05T15:00:00Z",
        },
        {
            "id": "act-2", "order_id": "order-aaa", "symbol": "AAPL", "side": "buy",
            "qty": 2, "price": 10.5, "transaction_time": "2026-10-05T15:05:00Z",
        },
        {
            "id": "act-3", "order_id": "order-bbb", "symbol": "MSFT", "side": "buy",
            "qty": 1, "price": 20, "transaction_time": "2026-10-05T16:00:00Z",
        },
        {
            "id": "act-4", "order_id": "order-ccc", "symbol": "SOL/USD", "side": "buy",
            "qty": 1, "price": 140, "transaction_time": "2026-10-05T02:00:00Z",
        },
    ])
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fills(
                alpaca_order_id, alpaca_fill_id, filled_at, symbol, side, qty, price, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "act-legacy-1",
                "act-legacy-1",
                "2026-10-05T17:00:00Z",
                "AMD",
                "buy",
                1,
                80,
                json.dumps({"id": "act-legacy-1", "order_id": "order-legacy", "symbol": "AMD"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO fills(
                alpaca_order_id, alpaca_fill_id, filled_at, symbol, side, qty, price, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "act-legacy-2",
                "act-legacy-2",
                "2026-10-05T17:05:00Z",
                "AMD",
                "buy",
                1,
                81,
                json.dumps({"id": "act-legacy-2", "order_id": "order-legacy", "symbol": "AMD"}),
            ),
        )
    # aaa, bbb, and the legacy AMD order. The 02:00Z SOL fill is still Sunday evening CT.
    assert db.count_fills_today(now=CYCLE_NOW) == 3


def test_daily_cap_blocks_when_count_reaches_limit_and_not_before():
    import agent_config as cfg

    assert cfg.daily_trade_cap_reached(4, 5) is False
    assert cfg.daily_trade_cap_reached(5, 5) is True
    assert cfg.daily_trade_cap_reached(6, 5) is True


def test_execution_blocks_new_order_when_count_equals_cap(monkeypatch):
    from agenttrade import db
    from agents.execution import execution_agent
    from buckets import Bucket

    db.init_db()
    monkeypatch.setattr(db, "trading_paused", lambda: False)
    monkeypatch.setattr(db, "trading_halted", lambda: False)
    import agent_config as cfg
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr(cfg, "daily_trades", 5)
    monkeypatch.setattr(cfg, "MAX_DAILY_TRADES", 5)
    posted = {"n": 0}
    monkeypatch.setattr(
        "agents.execution.alpaca_post",
        lambda *_a, **_k: posted.__setitem__("n", posted["n"] + 1),
    )
    bucket = Bucket(name="Crypto", allocation_pct=0.1, mode="crypto", asset_class="crypto", is_crypto=True)
    results = execution_agent(
        [{"ticker": "SOL/USD", "notional_usd": 50, "current_price": 140}],
        bucket,
        account_snapshot={"account": {"cash": 80000}, "open_orders": []},
    )
    assert posted["n"] == 0
    assert results[0]["status"] == "blocked"
    assert results[0]["blocked_reason"] == "daily_trade_limit"


def test_execution_allows_order_when_count_is_under_cap(monkeypatch):
    from agenttrade import db
    from agents.execution import execution_agent
    from buckets import Bucket

    db.init_db()
    monkeypatch.setattr(db, "trading_paused", lambda: False)
    monkeypatch.setattr(db, "trading_halted", lambda: False)
    import agent_config as cfg
    monkeypatch.setattr(cfg, "BUYING_ENABLED", True)
    monkeypatch.setattr(cfg, "daily_trades", 4)
    monkeypatch.setattr(cfg, "MAX_DAILY_TRADES", 5)
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    monkeypatch.setattr(cfg.bucket_manager, "tag_position", lambda *_a, **_k: None)
    monkeypatch.setattr("agenttrade.strategy_modes.mode_blocks_order", lambda *_a, **_k: (False, ""))
    monkeypatch.setattr("agenttrade.strategy_modes.get_bucket_execution_mode", lambda *_a, **_k: "live")
    monkeypatch.setattr("agents.execution.check_buy_allowed", lambda *a, **k: (True, "", {}))
    monkeypatch.setattr("agents.execution.get_account", lambda: {"cash": 80000, "equity": 100000})
    monkeypatch.setattr("agents.execution.enforce_no_margin_order_guard", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr("agents.execution.alpaca_post", lambda *_a, **_k: {"id": "crypto-sol"})
    bucket = Bucket(name="Crypto", allocation_pct=0.1, mode="crypto", asset_class="crypto", is_crypto=True)
    results = execution_agent(
        [{"ticker": "SOL/USD", "notional_usd": 50, "current_price": 140}],
        bucket,
        account_snapshot={"account": {"cash": 80000}, "open_orders": []},
    )
    assert results[0]["status"] == "placed"
    assert cfg.daily_trades == 5
