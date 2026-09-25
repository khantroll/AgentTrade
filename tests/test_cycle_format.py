"""Rebalance log amounts use a Python format that supports thousands separators."""

from pathlib import Path

import pytest

import cycle


def test_old_printf_thousands_separator_is_unsupported():
    with pytest.raises(ValueError, match="format character"):
        "%+,.0f" % -12345.2


def test_format_signed_amount():
    assert cycle.format_signed_amount(-12345.2) == "-12,345"
    assert cycle.format_signed_amount(2500) == "+2,500"
    assert cycle.format_signed_amount(0) == "+0"


def test_rebalance_log_line_includes_signed_amount():
    line = "  %s: target=%s%% current=%s%% drift=$%s → %s" % (
        "Growth",
        40,
        55,
        cycle.format_signed_amount(-50000.4),
        "SELL",
    )
    assert line == "  Growth: target=40% current=55% drift=$-50,000 → SELL"


def test_cycle_rebalance_log_does_not_use_printf_comma_format():
    src = Path(cycle.__file__).read_text(encoding="utf-8")
    assert "drift=$%+,.0f" not in src
    assert "format_signed_amount(r[\"drift_$\"])" in src
