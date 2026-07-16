"""Tests for per-frequency date range calculation."""

from __future__ import annotations

from datetime import date

import pytest
from date_ranges import daily_window, monthly_window, quarterly_window, window_for


def test_daily_window_returns_yesterday() -> None:
    w = daily_window(date(2026, 7, 14))
    assert w.first_date == date(2026, 7, 13)
    assert w.last_date == date(2026, 7, 13)
    assert w.period_label == "2026-07-13"


def test_daily_window_crosses_month_boundary() -> None:
    w = daily_window(date(2026, 8, 1))
    assert w.first_date == date(2026, 7, 31)
    assert w.period_label == "2026-07-31"


def test_daily_window_crosses_year_boundary() -> None:
    w = daily_window(date(2026, 1, 1))
    assert w.first_date == date(2025, 12, 31)
    assert w.period_label == "2025-12-31"


def test_monthly_window_returns_previous_full_month() -> None:
    w = monthly_window(date(2026, 7, 5))
    assert w.first_date == date(2026, 6, 1)
    assert w.last_date == date(2026, 6, 30)
    assert w.period_label == "2026-06"


def test_monthly_window_february_non_leap() -> None:
    w = monthly_window(date(2026, 3, 1))
    assert w.first_date == date(2026, 2, 1)
    assert w.last_date == date(2026, 2, 28)


def test_monthly_window_february_leap_year() -> None:
    w = monthly_window(date(2024, 3, 5))
    assert w.first_date == date(2024, 2, 1)
    assert w.last_date == date(2024, 2, 29)


def test_monthly_window_crosses_year() -> None:
    w = monthly_window(date(2026, 1, 5))
    assert w.first_date == date(2025, 12, 1)
    assert w.last_date == date(2025, 12, 31)
    assert w.period_label == "2025-12"


@pytest.mark.parametrize(
    ("today", "expected_first", "expected_last", "expected_label"),
    [
        (date(2026, 4, 15), date(2026, 1, 1), date(2026, 3, 31), "2026-Q1"),
        (date(2026, 7, 15), date(2026, 4, 1), date(2026, 6, 30), "2026-Q2"),
        (date(2026, 10, 15), date(2026, 7, 1), date(2026, 9, 30), "2026-Q3"),
        (date(2026, 1, 15), date(2025, 10, 1), date(2025, 12, 31), "2025-Q4"),
    ],
)
def test_quarterly_window(
    today: date, expected_first: date, expected_last: date, expected_label: str
) -> None:
    w = quarterly_window(today)
    assert w.first_date == expected_first
    assert w.last_date == expected_last
    assert w.period_label == expected_label


def test_window_for_dispatch() -> None:
    today = date(2026, 7, 14)
    assert window_for("daily", today) == daily_window(today)
    assert window_for("monthly", today) == monthly_window(today)
    assert window_for("quarterly", today) == quarterly_window(today)


def test_window_for_invalid_frequency_raises() -> None:
    with pytest.raises(ValueError, match="Unknown frequency"):
        window_for("hourly", date(2026, 7, 14))
