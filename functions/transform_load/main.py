"""Cloud Function (gen2, CloudEvent trigger): transform GCS raw JSON → BigQuery.

Trigger: `google.cloud.storage.object.v1.finalized` on the raw bucket.
The function is idempotent by design — the loader MERGEs on (series_id, observation_date),
so duplicate finalize events (which do happen) don't produce duplicate rows.

Only objects under `raw/{frequency}/{series_id}/{period}.json` are processed; any other
finalize event (e.g. dead_letter/, staging paths) is silently ignored.
"""

from __future__ import annotations

import json
import logging
import os
import re

import functions_framework  # type: ignore[import-untyped]
from bq_loader import load_rows_with_merge
from cloudevents.http import CloudEvent  # type: ignore[import-untyped]
from google.cloud import bigquery, storage  # type: ignore[import-untyped]
from transformer import TransformError, transform_payload

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

RAW_PATH_RE = re.compile(r"^raw/(daily|monthly|quarterly)/([^/]+)/([^/]+)\.json$")


@functions_framework.cloud_event
def transform_load(cloud_event: CloudEvent) -> None:
    """CloudEvent entry point.

    Raises on unexpected failures so Eventarc retries per configured policy.
    Structural failures (bad payload) are logged and swallowed — retrying won't help.
    """
    data = cloud_event.data or {}
    bucket_name = data.get("bucket")
    object_path = data.get("name")

    if not bucket_name or not object_path:
        logger.warning("CloudEvent missing bucket/name; ignoring", extra={"data": data})
        return

    match = RAW_PATH_RE.match(object_path)
    if not match:
        logger.info("Object not under raw/; ignoring", extra={"path": object_path})
        return

    frequency, series_id, _period_label = match.groups()

    target_dataset = os.environ.get("BQ_DATASET", "raw")
    target_table = os.environ.get("BQ_TABLE", "observations")
    project = os.environ.get("GCP_PROJECT_ID")

    gcs = storage.Client()
    bq = bigquery.Client(project=project)

    process_file(
        gcs_client=gcs,
        bq_client=bq,
        bucket_name=bucket_name,
        object_path=object_path,
        series_id=series_id,
        frequency=frequency,
        target_dataset=target_dataset,
        target_table=target_table,
        project=project,
    )


def process_file(
    *,
    gcs_client: storage.Client,
    bq_client: bigquery.Client,
    bucket_name: str,
    object_path: str,
    series_id: str,
    frequency: str,
    target_dataset: str,
    target_table: str,
    project: str | None = None,
) -> int:
    """Download → parse → transform → load. Returns rows loaded (0 if skipped)."""
    logger.info(
        "Processing raw object",
        extra={"bucket": bucket_name, "path": object_path, "series_id": series_id, "frequency": frequency},
    )

    raw_bytes = gcs_client.bucket(bucket_name).blob(object_path).download_as_bytes()

    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError:
        logger.exception("Raw file is not valid JSON; skipping", extra={"path": object_path})
        return 0

    try:
        rows = transform_payload(payload, series_id=series_id, frequency=frequency)
    except TransformError:
        logger.exception("Transform failed; skipping (raw file preserved)", extra={"path": object_path})
        return 0

    if not rows:
        logger.info("No rows to load", extra={"path": object_path})
        return 0

    loaded = load_rows_with_merge(
        client=bq_client,
        rows=rows,
        target_dataset=target_dataset,
        target_table=target_table,
        project=project,
    )
    logger.info(
        "Loaded rows into BigQuery",
        extra={"path": object_path, "row_count": loaded, "series_id": series_id},
    )
    return loaded
