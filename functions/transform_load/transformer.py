"""Transform raw BDE JSON payloads into BigQuery-ready row dicts.

The transformer is pure: no I/O, no clients, deterministic given inputs. Everything about
row shape and value coercion is decided here so the loader is trivially thin.

Output row schema (aligned with sql/schema/01_raw_observations.sql):
    series_id         STRING   REQUIRED
    observation_date  DATE     REQUIRED  (ISO YYYY-MM-DD)
    value             FLOAT64  NULLABLE  (None when BDE returned an unparseable value)
    status_code       STRING   NULLABLE  (verbatim from BDE, e.g. "OK")
    frequency         STRING   NULLABLE  (daily/monthly/quarterly)
    ingested_at       TIMESTAMP NULLABLE (ISO UTC)
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger(__name__)


class TransformError(Exception):
    """Raised when a raw payload is structurally invalid (unrecoverable)."""


def _parse_bde_date(value: str) -> date:
    """BDE returns dates as 'DD-MM-YYYY'."""
    return datetime.strptime(value, "%d-%m-%Y").date()


def _parse_bde_value(value: Any) -> float | None:
    """Coerce BDE's stringified number to float. Returns None for empty/NaN/unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s == "" or s.upper() in ("NA", "NAN", "NULL"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def transform_payload(
    raw: dict[str, Any],
    *,
    series_id: str,
    frequency: str,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Convert a BDE GetSeries payload into a list of row dicts ready for BigQuery.

    Raises:
        TransformError: when the payload structure is invalid or contains unparseable dates.
                        (An unparseable *value* is not an error — it becomes NULL.)
    """
    if not isinstance(raw, dict):
        raise TransformError(f"Payload is not a dict: {type(raw).__name__}")

    if raw.get("Codigo") != 0:
        raise TransformError(f"Payload has non-zero Codigo={raw.get('Codigo')!r}")

    series_block = raw.get("Series") or {}
    obs = series_block.get("Obs") or []

    if ingested_at is None:
        ingested_at = datetime.now(UTC)

    ingested_iso = ingested_at.astimezone(UTC).isoformat()

    rows: list[dict[str, Any]] = []
    unparseable_values = 0

    for i, o in enumerate(obs):
        raw_date = o.get("indexDateString")
        if not raw_date:
            raise TransformError(f"obs[{i}] missing indexDateString: {o!r}")
        try:
            observation_date = _parse_bde_date(raw_date)
        except ValueError as exc:
            raise TransformError(f"obs[{i}] bad date {raw_date!r}: {exc}") from exc

        raw_value = o.get("value")
        value = _parse_bde_value(raw_value)
        if value is None and raw_value not in (None, ""):
            unparseable_values += 1

        rows.append(
            {
                "series_id": series_id,
                "observation_date": observation_date.isoformat(),
                "value": value,
                "status_code": o.get("statusCode"),
                "frequency": frequency,
                "ingested_at": ingested_iso,
            }
        )

    if unparseable_values:
        logger.warning(
            "Some observations had unparseable values",
            extra={"series_id": series_id, "unparseable_count": unparseable_values},
        )

    return rows
