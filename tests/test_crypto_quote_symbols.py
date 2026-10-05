"""Yahoo quote symbols must not be Alpaca slash pairs.

yfinance requests ``/v8/finance/chart/{ticker}``. ``BTC/USD`` is parsed as a
path, Yahoo 404s, and yfinance logs ``$BTC/USD: possibly delisted``. The
chart client accepts ``BTC-USD`` (see yfinance.scrapers.quote). Alpaca order
symbols stay slash-form.
"""

from datetime import datetime, timedelta, timezone

import market_data


class _EmptyHistory:
    empty = True

    def __len__(self):
        return 0


class _RecordingTicker:
    def __init__(self, symbol):
        self.symbol = symbol
        type(self).seen.append(symbol)

    seen: list = []

    def history(self, *args, **kwargs):
        return _EmptyHistory()

    @property
    def info(self):
        return {}


def _install_ticker(monkeypatch, module):
    _RecordingTicker.seen = []
    monkeypatch.setattr(module.yf, "Ticker", _RecordingTicker)


def test_slash_crypto_becomes_yahoo_hyphen():
    assert market_data.crypto_to_yfinance("BTC/USD") == "BTC-USD"
    assert market_data.crypto_to_yfinance(" sol/usd ") == "SOL-USD"
    assert market_data.crypto_to_yfinance("ETH/USDT") == "ETH-USDT"
    assert market_data.yfinance_symbol("LINK/USD") == "LINK-USD"


def test_concatenated_crypto_becomes_yahoo_hyphen():
    # yfinance treats BTCUSD as a different (missing) symbol and logs it delisted.
    assert market_data.crypto_to_yfinance("BTCUSD") == "BTC-USD"
    assert market_data.yfinance_symbol("ethusd") == "ETH-USD"
    assert market_data.yfinance_symbol("BTC-USD") == "BTC-USD"


def test_equity_symbols_are_not_rewritten():
    assert market_data.yfinance_symbol("AAPL") == "AAPL"
    assert market_data.yfinance_symbol("BRK-B") == "BRK-B"
    assert market_data.yfinance_symbol("  spy ") == "SPY"
    assert "/" not in market_data.yfinance_symbol("AAPL")


def test_fetch_crypto_data_quotes_yahoo_form_and_keeps_alpaca_symbol(monkeypatch):
    _install_ticker(monkeypatch, market_data)
    out = market_data.fetch_crypto_data("SOL/USD")
    assert _RecordingTicker.seen == ["SOL-USD"]
    assert out["ticker"] == "SOL/USD"
    assert out["symbol"] == "SOL/USD"
    assert "/" not in _RecordingTicker.seen[0]


def test_fetch_stock_data_normalizes_crypto_and_leaves_equity(monkeypatch):
    _install_ticker(monkeypatch, market_data)
    market_data.fetch_stock_data("BTC/USD")
    market_data.fetch_stock_data("AAPL")
    market_data.fetch_stock_data("BRK-B")
    assert _RecordingTicker.seen == ["BTC-USD", "AAPL", "BRK-B"]


def test_crypto_universe_screen_queries_hyphen_and_returns_slash(monkeypatch):
    import screener

    _install_ticker(monkeypatch, screener)
    universe = screener.crypto_universe_screen(["BTC/USD", "ETH/USD"], top_n=5)
    assert _RecordingTicker.seen == ["BTC-USD", "ETH-USD"]
    assert universe == ["BTC/USD", "ETH/USD"]


def test_momentum_download_does_not_send_slash_symbols(monkeypatch):
    import screener

    seen = {}

    def _download(tickers, **kwargs):
        seen["tickers"] = list(tickers)
        raise RuntimeError("no network")

    monkeypatch.setattr(screener.yf, "download", _download)
    fallback = screener.momentum_screen(["AAPL", "BRK-B", "BTC/USD"], top_n=5)
    assert seen["tickers"] == ["AAPL", "BRK-B", "BTC-USD"]
    assert fallback == ["AAPL", "BRK-B", "BTC/USD"]
    assert all("/" not in ticker for ticker in seen["tickers"])


def test_signal_outcome_lookup_uses_yahoo_symbol(monkeypatch):
    import yfinance as yf

    import signal_performance as sp

    _RecordingTicker.seen = []
    monkeypatch.setattr(yf, "Ticker", _RecordingTicker)
    start = datetime.now(timezone.utc) - timedelta(days=10)
    end = datetime.now(timezone.utc)
    assert sp._fetch_price_history("BTC/USD", start, end) == []
    assert _RecordingTicker.seen == ["BTC-USD"]


def test_sqlite_backtest_price_lookup_uses_yahoo_symbol(monkeypatch):
    import yfinance as yf

    from agenttrade.backtest import _fetch_prices

    _RecordingTicker.seen = []
    monkeypatch.setattr(yf, "Ticker", _RecordingTicker)
    start = datetime.now(timezone.utc) - timedelta(days=10)
    end = datetime.now(timezone.utc)
    assert _fetch_prices("SOL/USD", start, end) == {}
    assert _RecordingTicker.seen == ["SOL-USD"]
