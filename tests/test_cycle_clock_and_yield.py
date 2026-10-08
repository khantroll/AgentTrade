"""Last-cycle age uses a real offset, and dividend yield is a fraction."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import health_check
import trading_day
from market_data import DELISTED_SYMBOLS, dividend_yield_fraction


EASTERN = ZoneInfo("America/New_York")


def _freeze_host_eastern(monkeypatch, now: datetime):
    monkeypatch.setattr(trading_day, "host_local_timezone", lambda: EASTERN)

    def _hours(dt, moment=None):
        return trading_day.hours_since(dt, now)

    monkeypatch.setattr(health_check, "_hours_ago", _hours)


def test_naive_eastern_stamp_is_not_four_hours_old(monkeypatch):
    # Cycle finished 09:41 Eastern, health ran at 14:00 UTC (19 minutes later).
    _freeze_host_eastern(monkeypatch, datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc))
    row = health_check._last_cycle_check({"last_run": "2026-10-08T09:41:00"})
    assert "08:41 CT" in row["message"]
    assert row["hours"] == pytest.approx(19 / 60, abs=0.02)
    assert "4.3h" not in row["message"]
    assert row["status"] == "ok"


def test_previous_day_gap_is_about_18_hours_not_22(monkeypatch):
    # 15:08 Eastern on Oct 7, checked 08:35 CT Oct 8 (13:35 UTC). Real gap ~18.4h.
    _freeze_host_eastern(monkeypatch, datetime(2026, 10, 8, 13, 35, tzinfo=timezone.utc))
    row = health_check._last_cycle_check({"last_run": "2026-10-07T15:08:00"})
    assert "2026-10-07 14:08 CT" in row["message"]
    assert 18.0 < row["hours"] < 19.0
    assert row["status"] == "warn"
    assert "check cron" in row["message"]


def test_naive_eastern_stamp_is_not_read_as_chicago(monkeypatch):
    """11:32 ET is 10:32 CT. A Chicago process zone must not display 11:32 CT."""
    monkeypatch.setattr(trading_day, "host_local_timezone", lambda: ZoneInfo("America/Chicago"))

    def _hours(dt, moment=None):
        return trading_day.hours_since(dt, datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(health_check, "_hours_ago", _hours)
    row = health_check._last_cycle_check({"last_run": "2026-10-08T11:32:00"})
    assert "10:32 CT" in row["message"]
    assert "11:32 CT" not in row["message"]
    assert row["hours"] == pytest.approx(28 / 60, abs=0.02)


def test_aware_utc_stamp_displays_in_chicago_without_a_second_shift(monkeypatch):
    _freeze_host_eastern(monkeypatch, datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc))
    row = health_check._last_cycle_check({"last_run": "2026-10-08T13:41:00+00:00"})
    assert "08:41 CT" in row["message"]
    assert row["hours"] < 0.5


def test_new_cycle_stamp_is_timezone_aware():
    stamp = trading_day.aware_now_iso()
    parsed = trading_day.parse_cycle_timestamp(stamp)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert abs(trading_day.hours_since(parsed)) < 0.05


def test_dividend_yield_collapses_percent_and_double_percent():
    # T ~4.54% and GAIN ~6.27% were logged as 454% and 627%.
    assert dividend_yield_fraction(4.54) == pytest.approx(0.0454)
    assert dividend_yield_fraction(6.27) == pytest.approx(0.0627)
    assert dividend_yield_fraction(454) == pytest.approx(0.0454)
    assert dividend_yield_fraction(627) == pytest.approx(0.0627)
    assert dividend_yield_fraction(0.0454) == pytest.approx(0.0454)

    # Annual dollars / price wins over a scaled Yahoo field.
    assert dividend_yield_fraction(627, price=29.0, annual_dividend=1.817) == pytest.approx(1.817 / 29.0)


def test_yield_floor_uses_the_fraction_not_the_percent_number():
    """A 1.5% name must fail the 2% floor. A percent-scale 1.5 used to pass."""
    low = dividend_yield_fraction(1.5)
    high = dividend_yield_fraction(6.27)
    assert low < 0.02
    assert high >= 0.02
    # Ranking stays in fraction space, so the ex-date bonus is not drowned.
    payout = 0.5
    assert (high * 10) * (1 - payout) < 5


def test_delisted_names_are_out_of_the_stock_universe():
    from screener import ALL_CANDIDATES, DIVIDEND_UNIVERSE

    for ticker in ("NEWR", "HOLX", "NSA", "MMC", "SUMO", "JAMF", "CYBR", "NOVA", "MPW", "MMP"):
        assert ticker in DELISTED_SYMBOLS
        assert ticker not in ALL_CANDIDATES
        assert ticker not in DIVIDEND_UNIVERSE


def test_missing_stops_log_once(monkeypatch, caplog):
    import agent_config as cfg
    import agents.position_review as review

    monkeypatch.setattr(review, "get_open_orders", lambda: [])
    monkeypatch.setattr(cfg.bucket_manager, "_load_tags", lambda: {})
    positions = [
        {"symbol": f"ZZ{i}", "qty": 1, "avg_entry_price": 10, "current_price": 11, "market_value": 11}
        for i in range(3)
    ]
    with caplog.at_level("INFO"):
        review.review_positions(positions, {"portfolio_value": 1000}, {}, True)
    assert caplog.text.count("using bucket % defaults") == 1
    assert "3 position(s)" in caplog.text
