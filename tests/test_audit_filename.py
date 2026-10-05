"""Audit filenames must not contain the slash in a crypto symbol."""

def test_audit_filename_symbol_rewrites_slash_only():
    from market_data import audit_filename_symbol

    assert audit_filename_symbol("SOL/USD") == "SOL-USD"
    assert audit_filename_symbol("BTC/USD") == "BTC-USD"
    assert audit_filename_symbol("AAPL") == "AAPL"
    assert audit_filename_symbol("BRK.B") == "BRK.B"


def test_audit_log_keeps_symbol_in_body_and_sanitizes_filename(tmp_path, monkeypatch):
    import market_data

    monkeypatch.setattr(market_data, "AUDIT_DIR", str(tmp_path))
    market_data.audit_log(
        "BUY",
        "SOL/USD",
        "rank SOL/USD",
        "raw",
        {"action": "BUY", "ticker": "SOL/USD"},
        {"symbol": "SOL/USD"},
    )
    market_data.audit_log(
        "SKIP",
        "AAPL",
        "prompt",
        "raw",
        {"action": "SKIP", "ticker": "AAPL"},
        {"symbol": "AAPL"},
    )

    logs = sorted(path.name for path in tmp_path.iterdir())
    assert len(logs) == 2
    crypto = next(name for name in logs if "SOL-USD" in name)
    equity = next(name for name in logs if "AAPL" in name)
    assert "/" not in crypto
    assert "SOL/USD" not in crypto
    assert crypto.endswith("_SOL-USD_BUY.log")
    assert equity.endswith("_AAPL_SKIP.log")

    body = (tmp_path / crypto).read_text(encoding="utf-8")
    assert "Ticker: SOL/USD" in body
    assert '"ticker": "SOL/USD"' in body
    assert (tmp_path / crypto).parent == tmp_path
