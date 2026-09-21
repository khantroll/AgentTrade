"""Unit tests for period summary stats and benchmark fallback."""

from unittest.mock import patch

import pytest

import period_summary as ps


class TestMaxDrawdown:
    def test_empty_series(self):
        assert ps.max_drawdown([]) == {"max_drawdown": None, "max_drawdown_pct": None}

    def test_monotonic_up_no_drawdown(self):
        dd = ps.max_drawdown([100, 110, 120])
        assert dd["max_drawdown"] == 0.0
        assert dd["max_drawdown_pct"] == 0.0

    def test_peak_to_trough(self):
        dd = ps.max_drawdown([100, 120, 90, 95])
        assert dd["max_drawdown"] == 30.0
        assert dd["max_drawdown_pct"] == 25.0


class TestTradeStats:
    def test_win_rate_and_profit_factor(self):
        closed = [
            {"in_period": True, "realized_pl": 100},
            {"in_period": True, "realized_pl": 50},
            {"in_period": True, "realized_pl": -25},
        ]
        stats = ps.trade_stats(closed)

        assert stats["closed_trades"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(66.7, abs=0.1)
        assert stats["avg_winner"] == 75.0
        assert stats["avg_loser"] == -25.0
        assert stats["profit_factor"] == 6.0

    def test_respects_in_period_flag(self):
        closed = [
            {"in_period": False, "realized_pl": 999},
            {"in_period": True, "realized_pl": 10},
        ]
        stats = ps.trade_stats(closed)
        assert stats["closed_trades"] == 1
        assert stats["wins"] == 1


class TestBenchmarkReturn:
    def test_benchmark_return_helper(self):
        out = ps._benchmark_return(100, 110, "SPY", "yfinance")
        assert out["ok"] is True
        assert out["return_pct"] == 10.0
        assert out["source"] == "yfinance"

    @patch("period_summary._fetch_benchmark_alpaca")
    @patch("period_summary._fetch_benchmark_yfinance")
    def test_yfinance_primary_success(self, mock_yf, mock_alpaca):
        mock_yf.return_value = ps._benchmark_return(400, 440, "SPY", "yfinance")
        result = ps.fetch_benchmark_return("2025-01-01", "2025-03-01")

        assert result["ok"] is True
        assert result["source"] == "yfinance"
        assert result.get("fallback") is None
        mock_alpaca.assert_not_called()

    @patch("period_summary._fetch_benchmark_alpaca")
    @patch("period_summary._fetch_benchmark_yfinance")
    def test_alpaca_fallback_when_yfinance_fails(self, mock_yf, mock_alpaca):
        mock_yf.return_value = {"ok": False, "error": "rate limited", "source": "yfinance"}
        mock_alpaca.return_value = ps._benchmark_return(500, 525, "SPY", "alpaca", bars=42)

        result = ps.fetch_benchmark_return("2025-01-01", "2025-03-01")

        assert result["ok"] is True
        assert result["source"] == "alpaca"
        assert result["fallback"] is True
        assert result["primary_error"] == "rate limited"
        assert result["return_pct"] == 5.0
        assert result["bars"] == 42

    @patch("period_summary._fetch_benchmark_alpaca")
    @patch("period_summary._fetch_benchmark_yfinance")
    def test_both_sources_fail(self, mock_yf, mock_alpaca):
        mock_yf.return_value = {"ok": False, "error": "yf down", "source": "yfinance"}
        mock_alpaca.return_value = {"ok": False, "error": "no keys", "source": "alpaca"}

        result = ps.fetch_benchmark_return("2025-01-01", "2025-03-01")

        assert result["ok"] is False
        assert result["primary_error"] == "yf down"
        assert result["fallback_error"] == "no keys"


class TestComputePeriodSummary:
    @patch("trade_log.summarize_trades")
    @patch("trade_log.load_trades")
    @patch("pnl_attribution.fifo_realized")
    @patch("period_summary.fetch_benchmark_return")
    @patch("period_summary.load_cycles")
    def test_compute_period_summary_happy_path(
        self, mock_load, mock_bench, mock_fifo, mock_load_trades, mock_summ,
    ):
        mock_load.return_value = [
            {"date": "2025-01-01", "portfolio_value": 10000},
            {"date": "2025-01-15", "portfolio_value": 11000},
        ]
        mock_bench.return_value = ps._benchmark_return(400, 420, "SPY", "yfinance")
        mock_fifo.return_value = ([
            {"in_period": True, "realized_pl": 100},
            {"in_period": True, "realized_pl": -20},
        ], {})
        mock_load_trades.return_value = [{"symbol": "A"}]
        mock_summ.return_value = {"total": 4}

        result = ps.compute_period_summary(max_days=30)

        assert result["ok"] is True
        assert result["starting_value"] == 10000.0
        assert result["ending_value"] == 11000.0
        assert result["portfolio_return_pct"] == 10.0
        assert result["benchmark"]["return_pct"] == 5.0
        assert result["alpha_vs_benchmark_pct"] == 5.0
        assert result["max_drawdown_pct"] == 0.0
        assert result["closed_trades"] == 2
        assert result["win_rate"] == pytest.approx(50.0)
        assert result["fills"] == 4

    @patch("period_summary.load_cycles")
    def test_compute_period_summary_no_history(self, mock_load):
        mock_load.return_value = []
        result = ps.compute_period_summary(max_days=90)
        assert result["ok"] is False
        assert "portfolio history" in result["message"].lower()
