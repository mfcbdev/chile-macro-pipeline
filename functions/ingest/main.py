"""Cloud Function (gen2) entry point: ingest BDE series into GCS.

Trigger: HTTP (invoked by Cloud Scheduler).
Payload: {"frequency": "daily"|"monthly"|"quarterly"}
Return:  {"processed": N, "skipped": M, "errors": [...], "frequency": ..., "period_label": ...}

The function is intentionally tolerant of per-series failure: one bad series does not
crash the batch. HTTP 200 with a summary lets Cloud Scheduler see a clean success while
still surfacing errors for observability.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import functions_framework  # type: ignore[import-untyped]
from bde_client import BDEClient, BDEError
from date_ranges import DateWindow, window_for
from gcs import build_raw_path, upload_json_idempotent
from google.cloud import storage  # type: ignore[import-untyped]

from config import (
    VALID_FREQUENCIES,
    Frequency,
    Series,
    filter_by_frequency,
    load_bde_credentials,
    load_series,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Deployed alongside the function; overridable for local runs / tests.
SERIES_CONFIG_PATH = os.environ.get("SERIES_CONFIG_PATH", str(Path(__file__).parent / "series.yaml"))


@functions_framework.http
def ingest(request) -> tuple[dict[str, Any], int]:  # noqa: ANN001 - Flask request injected by functions-framework
    """HTTP entry point."""
    payload = request.get_json(silent=True) or {}

    frequency = payload.get("frequency")
    if frequency not in VALID_FREQUENCIES:
        return (
            {"error": f"Missing or invalid 'frequency'; expected one of {list(VALID_FREQUENCIES)}"},
            400,
        )

    bucket_name = os.environ.get("GCS_BUCKET")
    if not bucket_name:
        return {"error": "GCS_BUCKET env var is not set"}, 500

    summary = run_ingest(
        frequency=frequency,  # type: ignore[arg-type]
        bucket_name=bucket_name,
        config_path=SERIES_CONFIG_PATH,
        today=date.today(),
    )
    return summary, 200


def run_ingest(
    frequency: Frequency,
    bucket_name: str,
    config_path: str,
    today: date,
    *,
    bde_client: BDEClient | None = None,
    gcs_client: storage.Client | None = None,
) -> dict[str, Any]:
    """Core ingestion loop. Isolated from Flask so it's directly unit-testable."""
    started_at = datetime.now(UTC)
    window = window_for(frequency, today)

    all_series = load_series(config_path)
    to_process: list[Series] = filter_by_frequency(all_series, frequency)

    logger.info(
        "Starting ingest",
        extra={
            "frequency": frequency,
            "series_count": len(to_process),
            "first_date": window.first_date.isoformat(),
            "last_date": window.last_date.isoformat(),
            "period_label": window.period_label,
        },
    )

    if bde_client is None:
        creds = load_bde_credentials()
        bde_client = BDEClient(credentials=creds)
    if gcs_client is None:
        gcs_client = storage.Client()

    processed = 0
    skipped = 0
    empty: list[str] = []
    errors: list[dict[str, str]] = []

    for series in to_process:
        try:
            result = _ingest_single(series, window, bde_client, gcs_client, bucket_name)
        except BDEError as exc:
            logger.error(
                "BDE API error",
                extra={"series_id": series.id, "code": str(exc.code), "description": exc.description},
            )
            errors.append({"series_id": series.id, "error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 — we want per-series isolation
            logger.exception("Unexpected error ingesting series", extra={"series_id": series.id})
            errors.append({"series_id": series.id, "error": f"{type(exc).__name__}: {exc}"})
            continue

        if result == "written":
            processed += 1
        elif result == "skipped":
            skipped += 1
        elif result == "empty":
            empty.append(series.id)

    duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)

    summary = {
        "frequency": frequency,
        "period_label": window.period_label,
        "processed": processed,
        "skipped": skipped,
        "empty": empty,
        "errors": errors,
        "duration_ms": duration_ms,
    }
    logger.info("Ingest complete", extra=summary)
    return summary


def _ingest_single(
    series: Series,
    window: DateWindow,
    bde_client: BDEClient,
    gcs_client: storage.Client,
    bucket_name: str,
) -> str:
    """Return one of: 'written', 'skipped', 'empty'."""
    path = build_raw_path(series.frequency, series.id, window.period_label)

    # Idempotency short-circuit: don't call BDE if the file is already in GCS.
    bucket = gcs_client.bucket(bucket_name)
    if bucket.blob(path).exists():
        logger.info(
            "Series already ingested for period; skipping BDE call",
            extra={"series_id": series.id, "path": path},
        )
        return "skipped"

    parsed, raw = bde_client.get_series(series.id, window.first_date, window.last_date)

    if parsed.is_empty:
        logger.warning(
            "BDE returned no observations; not writing file",
            extra={"series_id": series.id, "period_label": window.period_label},
        )
        return "empty"

    result = upload_json_idempotent(gcs_client, bucket_name, path, raw)
    return "written" if result.written else "skipped"
