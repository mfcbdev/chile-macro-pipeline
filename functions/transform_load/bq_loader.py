"""Load transformed rows into BigQuery with MERGE-based deduplication.

Pattern:
  1. Load rows into a per-run staging table via `load_table_from_json` (a batch load job,
     NOT a streaming insert — streaming has a 90-min buffer that MERGE can miss).
  2. MERGE staging into the target on (series_id, observation_date).
  3. Drop staging. A 1-hour table expiration is set as a safety net for orphans.

Target table is expected to exist (see sql/schema/01_raw_observations.sql).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import bigquery  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# BigQuery identifiers cannot be parameterized in SQL; validate the allowed shape ourselves
# to prevent malformed env vars from producing broken (or, worst case, injecting) MERGE SQL.
_BQ_PROJECT_RE = re.compile(r"^[a-z][a-z0-9\-]{4,28}[a-z0-9]$")
_BQ_DATASET_TABLE_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")


def _validate_identifier(name: str, kind: str, pattern: re.Pattern[str]) -> None:
    if not pattern.match(name):
        raise ValueError(f"Invalid BigQuery {kind} identifier: {name!r}")


# Schema kept in sync with sql/schema/01_raw_observations.sql
OBSERVATIONS_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("series_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("observation_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("value", "FLOAT64"),
    bigquery.SchemaField("status_code", "STRING"),
    bigquery.SchemaField("frequency", "STRING"),
    bigquery.SchemaField("ingested_at", "TIMESTAMP"),
]

STAGING_TTL = timedelta(hours=1)


def load_rows_with_merge(
    client: bigquery.Client,
    rows: list[dict[str, Any]],
    target_dataset: str,
    target_table: str,
    project: str | None = None,
) -> int:
    """Load `rows` into `{project}.{target_dataset}.{target_table}` via staging + MERGE.

    Returns the number of rows sent to staging (not the count matched vs inserted, which
    would require an extra round-trip).
    """
    if not rows:
        logger.info("No rows to load; skipping.")
        return 0

    project = project or client.project
    _validate_identifier(project, "project", _BQ_PROJECT_RE)
    _validate_identifier(target_dataset, "dataset", _BQ_DATASET_TABLE_RE)
    _validate_identifier(target_table, "table", _BQ_DATASET_TABLE_RE)

    staging_name = f"_staging_observations_{uuid.uuid4().hex[:12]}"
    staging_ref = f"{project}.{target_dataset}.{staging_name}"
    target_ref = f"{project}.{target_dataset}.{target_table}"

    logger.info(
        "Loading rows into staging",
        extra={"staging": staging_ref, "row_count": len(rows)},
    )

    _load_to_staging(client, rows, staging_ref)

    try:
        _merge_staging_into_target(client, staging_ref, target_ref)
    finally:
        # Delete staging even if MERGE fails; expiration is the ultimate safety net.
        client.delete_table(staging_ref, not_found_ok=True)

    logger.info(
        "MERGE complete",
        extra={"target": target_ref, "row_count": len(rows)},
    )
    return len(rows)


def _load_to_staging(client: bigquery.Client, rows: list[dict[str, Any]], staging_ref: str) -> None:
    job_config = bigquery.LoadJobConfig(
        schema=OBSERVATIONS_SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    load_job = client.load_table_from_json(rows, staging_ref, job_config=job_config)
    load_job.result()

    # Belt-and-suspenders: set expiration in case cleanup fails.
    staging_table = client.get_table(staging_ref)
    staging_table.expires = datetime.now(UTC) + STAGING_TTL
    client.update_table(staging_table, ["expires"])


def _merge_staging_into_target(client: bigquery.Client, staging_ref: str, target_ref: str) -> None:
    # Referencing tables with backticks is required when using fully-qualified names.
    merge_sql = f"""
    MERGE `{target_ref}` T
    USING `{staging_ref}` S
      ON T.series_id = S.series_id
     AND T.observation_date = S.observation_date
    WHEN MATCHED THEN
      UPDATE SET
        value = S.value,
        status_code = S.status_code,
        frequency = S.frequency,
        ingested_at = S.ingested_at
    WHEN NOT MATCHED THEN
      INSERT (series_id, observation_date, value, status_code, frequency, ingested_at)
      VALUES (S.series_id, S.observation_date, S.value, S.status_code, S.frequency, S.ingested_at)
    """
    query_job = client.query(merge_sql)
    query_job.result()
