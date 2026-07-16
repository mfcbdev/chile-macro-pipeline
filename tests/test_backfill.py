"""Tests for backfill script's pure functions (iterators, payload splitter, stats)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

# Load scripts/backfill.py explicitly so its sys.path shim doesn't fight with pytest.
# Register in sys.modules so @dataclass (which looks up cls.__module__) works.
_BACKFILL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backfill.py"
_spec = importlib.util.spec_from_file_location("backfill", _BACKFILL_PATH)
backfill = importlib.util.module_from_spec(_spec)
sys.modules["backfill"] = backfill
_spec.loader.exec_module(backfill)


# ---------------------------------------------------------------------------
# _iter_months
# ---------------------------------------------------------------------------


def test_iter_months_single_month() -> None:
    months = list(backfill._iter_months(date(2024, 5, 3), date(2024, 5, 27)))
    assert months == [(date(2024, 5, 1), date(2024, 5, 31))]


def test_iter_months_spans_year_boundary() -> None:
    months = list(backfill._iter_months(date(2023, 11, 15), date(2024, 2, 5)))
    assert months == [
        (date(2023, 11, 1), date(2023, 11, 30)),
        (date(2023, 12, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),
    ]


def test_iter_months_february_non_leap() -> None:
    months = list(backfill._iter_months(date(2026, 2, 1), date(2026, 2, 28)))
    assert months == [(date(2026, 2, 1), date(2026, 2, 28))]


# ---------------------------------------------------------------------------
# _iter_quarters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            date(2024, 5, 3),
            date(2024, 5, 27),
            [(date(2024, 4, 1), date(2024, 6, 30), "2024-Q2")],
        ),
        (
            date(2023, 11, 15),
            date(2024, 4, 5),
            [
                (date(2023, 10, 1), date(2023, 12, 31), "2023-Q4"),
                (date(2024, 1, 1), date(2024, 3, 31), "2024-Q1"),
                (date(2024, 4, 1), date(2024, 6, 30), "2024-Q2"),
            ],
        ),
        (
            date(2024, 1, 1),
            date(2024, 12, 31),
            [
                (date(2024, 1, 1), date(2024, 3, 31), "2024-Q1"),
                (date(2024, 4, 1), date(2024, 6, 30), "2024-Q2"),
                (date(2024, 7, 1), date(2024, 9, 30), "2024-Q3"),
                (date(2024, 10, 1), date(2024, 12, 31), "2024-Q4"),
            ],
        ),
    ],
)
def test_iter_quarters(start: date, end: date, expected) -> None:
    quarters = list(backfill._iter_quarters(start, end))
    assert quarters == expected


# ---------------------------------------------------------------------------
# _split_yearly_response_by_day
# ---------------------------------------------------------------------------


def test_split_yearly_response_splits_and_keeps_envelope() -> None:
    yearly = {
        "Codigo": 0,
        "Descripcion": "OK",
        "Series": {
            "seriesId": "F073.TCO.PRE.Z.D",
            "descripEsp": "Dólar observado",
            "Obs": [
                {"indexDateString": "02-01-2024", "value": "890.10", "statusCode": "OK"},
                {"indexDateString": "03-01-2024", "value": "891.50", "statusCode": "OK"},
                {"indexDateString": "05-01-2024", "value": "895.20", "statusCode": "OK"},
            ],
        },
    }

    by_day = backfill._split_yearly_response_by_day(yearly, year=2024)

    assert set(by_day.keys()) == {date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)}
    # Each per-day payload preserves the envelope but has exactly one observation.
    for d, payload in by_day.items():
        assert payload["Codigo"] == 0
        assert payload["Descripcion"] == "OK"
        assert payload["Series"]["seriesId"] == "F073.TCO.PRE.Z.D"
        assert len(payload["Series"]["Obs"]) == 1
        assert payload["Series"]["Obs"][0]["indexDateString"] == d.strftime("%d-%m-%Y")


def test_split_yearly_response_filters_out_wrong_year() -> None:
    yearly = {
        "Codigo": 0,
        "Descripcion": "OK",
        "Series": {
            "Obs": [
                {"indexDateString": "31-12-2023", "value": "1.0", "statusCode": "OK"},
                {"indexDateString": "01-01-2024", "value": "2.0", "statusCode": "OK"},
                {"indexDateString": "01-01-2025", "value": "3.0", "statusCode": "OK"},
            ],
        },
    }
    by_day = backfill._split_yearly_response_by_day(yearly, year=2024)
    assert list(by_day.keys()) == [date(2024, 1, 1)]


def test_split_yearly_response_handles_empty() -> None:
    yearly = {"Codigo": 0, "Descripcion": "OK", "Series": {"Obs": []}}
    assert backfill._split_yearly_response_by_day(yearly, year=2024) == {}


def test_split_yearly_response_skips_malformed_dates() -> None:
    yearly = {
        "Codigo": 0,
        "Descripcion": "OK",
        "Series": {
            "Obs": [
                {"indexDateString": "not-a-date", "value": "1.0", "statusCode": "OK"},
                {"indexDateString": "15-06-2024", "value": "2.0", "statusCode": "OK"},
            ],
        },
    }
    by_day = backfill._split_yearly_response_by_day(yearly, year=2024)
    assert list(by_day.keys()) == [date(2024, 6, 15)]


def test_split_yearly_response_does_not_mutate_input() -> None:
    yearly = {
        "Codigo": 0,
        "Descripcion": "OK",
        "Series": {"Obs": [{"indexDateString": "01-01-2024", "value": "1.0", "statusCode": "OK"}]},
    }
    original_obs_count = len(yearly["Series"]["Obs"])
    _ = backfill._split_yearly_response_by_day(yearly, year=2024)
    assert len(yearly["Series"]["Obs"]) == original_obs_count


# ---------------------------------------------------------------------------
# BackfillStats
# ---------------------------------------------------------------------------


def test_stats_merge_accumulates() -> None:
    s = backfill.BackfillStats()
    s = s.merge(written=2)
    s = s.merge(written=1, skipped=3)
    s = s.merge(errors=1, empty=1)
    assert s == backfill.BackfillStats(written=3, skipped=3, empty=1, errors=1)
