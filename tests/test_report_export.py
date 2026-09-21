"""Contract tests for reconstructed report_export helpers."""

from unittest.mock import patch

import report_export as re


def test_performance_summary_rows_matches_dashboard_contract():
    ps = {
        "start_date": "2025-01-01", "end_date": "2025-01-31",
        "starting_value": 10000, "ending_value": 11000,
        "portfolio_return_pct": 10, "benchmark": {"return_pct": 5, "source": "yfinance"},
        "alpha_vs_benchmark_pct": 5, "max_drawdown": 300, "max_drawdown_pct": 2.5,
        "closed_trades": 3, "win_rate": 66.7, "avg_winner": 75,
        "avg_loser": -25, "profit_factor": 6, "fills": 4,
    }
    rows = re.performance_summary_rows(ps, max_days=30)
    assert rows[0] == ["metric", "value"]
    assert ["Starting Value", "10000.00"] in rows
    assert ["S&P Return (same period)", "5.00%"] in rows
    assert ["Max Drawdown", "2.50% ($300.00)"] in rows
    assert ["meta_range_days", "30"] in rows


@patch("period_summary.compute_period_summary")
def test_performance_summary_csv(mock_compute):
    mock_compute.return_value = {"starting_value": 100, "ending_value": 110, "benchmark": {}}
    body = re.performance_summary_csv(max_days=30)
    assert "metric,value" in body
    assert "Starting Value,100.00" in body


@patch("trade_log.get_trades")
@patch("performance_history.get_history")
def test_full_report_sections(mock_history, mock_trades):
    mock_history.return_value = {
        "period_summary": {"starting_value": 100, "ending_value": 110, "benchmark": {}},
        "daily": [{"date": "2025-01-01", "portfolio_value": 100}],
    }
    mock_trades.return_value = {"trades": [], "attribution": {"by_bucket": [], "by_symbol": [], "closed_trades": []}}
    body = re.full_report_csv(max_days=30)
    assert "AgentTrade Full Report" in body
    assert "=== PERFORMANCE SUMMARY ===" in body
    assert "=== DAILY PORTFOLIO ===" in body
    assert "=== TRADE LOG (FILLS) ===" in body
    assert "=== P&L BY BUCKET ===" in body
