"""Tests for the transformer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from transformer import TransformError, transform_payload

FIXED_INGEST = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _payload(obs: list[dict], series_id: str = "F073.TCO.PRE.Z.D") -> dict:
    return {
        "Codigo": 0,
        "Descripcion": "OK",
        "Series": {"seriesId": series_id, "descripEsp": "Dólar", "Obs": obs},
    }


def test_transform_basic_row() -> None:
    payload = _payload([{"indexDateString": "14-07-2026", "value": "950.25", "statusCode": "OK"}])
    rows = transform_payload(
        payload, series_id="F073.TCO.PRE.Z.D", frequency="daily", ingested_at=FIXED_INGEST
    )

    assert rows == [
        {
            "series_id": "F073.TCO.PRE.Z.D",
            "observation_date": "2026-07-14",
            "value": 950.25,
            "status_code": "OK",
            "frequency": "daily",
            "ingested_at": "2026-07-14T12:00:00+00:00",
        }
    ]


def test_transform_multiple_rows() -> None:
    payload = _payload(
        [
            {"indexDateString": "01-06-2026", "value": "100.0", "statusCode": "OK"},
            {"indexDateString": "02-06-2026", "value": "101.5", "statusCode": "OK"},
            {"indexDateString": "03-06-2026", "value": "102.0", "statusCode": "OK"},
        ]
    )
    rows = transform_payload(payload, series_id="X", frequency="daily", ingested_at=FIXED_INGEST)
    assert [r["observation_date"] for r in rows] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert [r["value"] for r in rows] == [100.0, 101.5, 102.0]


def test_transform_preserves_non_ok_status() -> None:
    payload = _payload(
        [
            {"indexDateString": "14-07-2026", "value": "1.0", "statusCode": "OK"},
            {"indexDateString": "15-07-2026", "value": "", "statusCode": "ND"},
        ]
    )
    rows = transform_payload(payload, series_id="X", frequency="daily", ingested_at=FIXED_INGEST)
    assert rows[1]["status_code"] == "ND"
    assert rows[1]["value"] is None


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("950.25", 950.25),
        ("0", 0.0),
        ("-1.5", -1.5),
        ("", None),
        (None, None),
        ("NA", None),
        ("NaN", None),
        ("null", None),
        ("not-a-number", None),
        (42, 42.0),
        (3.14, 3.14),
    ],
)
def test_value_parsing(raw_value, expected) -> None:
    payload = _payload([{"indexDateString": "14-07-2026", "value": raw_value, "statusCode": "OK"}])
    rows = transform_payload(payload, series_id="X", frequency="daily", ingested_at=FIXED_INGEST)
    assert rows[0]["value"] == expected


def test_transform_empty_observations_returns_empty_list() -> None:
    payload = _payload([])
    rows = transform_payload(payload, series_id="X", frequency="daily", ingested_at=FIXED_INGEST)
    assert rows == []


def test_transform_rejects_non_dict() -> None:
    with pytest.raises(TransformError, match="not a dict"):
        transform_payload("not a dict", series_id="X", frequency="daily")  # type: ignore[arg-type]


def test_transform_rejects_bde_error_payload() -> None:
    payload = {"Codigo": -5, "Descripcion": "Series not found"}
    with pytest.raises(TransformError, match="Codigo"):
        transform_payload(payload, series_id="X", frequency="daily")


def test_transform_rejects_bad_date() -> None:
    payload = _payload([{"indexDateString": "2026-07-14", "value": "1.0", "statusCode": "OK"}])
    with pytest.raises(TransformError, match="bad date"):
        transform_payload(payload, series_id="X", frequency="daily")


def test_transform_rejects_missing_date() -> None:
    payload = _payload([{"value": "1.0", "statusCode": "OK"}])
    with pytest.raises(TransformError, match="missing indexDateString"):
        transform_payload(payload, series_id="X", frequency="daily")


def test_transform_uses_utc_for_ingested_at_when_not_provided() -> None:
    payload = _payload([{"indexDateString": "14-07-2026", "value": "1.0", "statusCode": "OK"}])
    rows = transform_payload(payload, series_id="X", frequency="daily")
    # Should be a valid ISO timestamp ending with +00:00
    assert rows[0]["ingested_at"].endswith("+00:00")
    # And parseable
    parsed = datetime.fromisoformat(rows[0]["ingested_at"])
    assert parsed.tzinfo is not None
