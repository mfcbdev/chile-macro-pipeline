"""Compute (first_date, last_date, period_label) for each ingestion frequency.

Semantics:
  daily     — the previous calendar day (single-day window).
  monthly   — the previous calendar month (first through last day).
  quarterly — the previous calendar quarter (first day of quarter through last day of quarter).

The period_label is the string used in the GCS object path and is unique per (frequency, run):
  daily     — "YYYY-MM-DD" (the target day)
  monthly   — "YYYY-MM"
  quarterly — "YYYY-Q{1..4}"
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class DateWindow:
    first_date: date
    last_date: date
    period_label: str


def daily_window(today: date) -> DateWindow:
    """Yesterday relative to `today`."""
    target = today - timedelta(days=1)
    return DateWindow(first_date=target, last_date=target, period_label=target.isoformat())


def monthly_window(today: date) -> DateWindow:
    """Previous full calendar month relative to `today`."""
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    label = f"{first_of_prev.year:04d}-{first_of_prev.month:02d}"
    return DateWindow(first_date=first_of_prev, last_date=last_of_prev, period_label=label)


def quarterly_window(today: date) -> DateWindow:
    """Previous full calendar quarter relative to `today`."""
    # Quarter of `today` (1..4)
    current_q = (today.month - 1) // 3 + 1
    if current_q == 1:
        prev_q = 4
        year = today.year - 1
    else:
        prev_q = current_q - 1
        year = today.year

    first_month = (prev_q - 1) * 3 + 1
    last_month = first_month + 2
    first = date(year, first_month, 1)
    last_day = calendar.monthrange(year, last_month)[1]
    last = date(year, last_month, last_day)
    label = f"{year:04d}-Q{prev_q}"
    return DateWindow(first_date=first, last_date=last, period_label=label)


def window_for(frequency: str, today: date) -> DateWindow:
    if frequency == "daily":
        return daily_window(today)
    if frequency == "monthly":
        return monthly_window(today)
    if frequency == "quarterly":
        return quarterly_window(today)
    raise ValueError(f"Unknown frequency: {frequency!r}")
