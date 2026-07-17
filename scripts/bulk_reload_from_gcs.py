"""One-shot bulk reload: read every raw JSON in GCS, transform, load into BigQuery in ONE MERGE.

Use this after `backfill.py` if the per-file transform_load path was throttled by BigQuery's
1,500 DML statements per table per day quota. This bypasses transform_load entirely, so
however many files exist in GCS produces exactly ONE load job + ONE MERGE statement.

Safe to re-run — the MERGE is idempotent on (series_id, observation_date), same as the
production loader.

Usage:
    python scripts/bulk_reload_from_gcs.py
    python scripts/bulk_reload_from_gcs.py --prefix raw/daily/
    python scripts/bulk_reload_from_gcs.py --dry-run

Requires GCS_BUCKET + GCP_PROJECT_ID in .env (or as env vars).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "functions" / "ingest"))
sys.path.insert(0, str(REPO_ROOT / "functions" / "transform_load"))

from bq_loader import load_rows_with_merge  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from google.cloud import bigquery, storage  # type: ignore[import-untyped]  # noqa: E402
from transformer import TransformError, transform_payload  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bulk_reload")

RAW_PATH_RE = re.compile(r"^raw/(daily|monthly|quarterly)/([^/]+)/([^/]+)\.json$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="raw/", help="GCS object prefix to scan (default: raw/)")
    parser.add_argument("--bucket", default=None, help="GCS bucket (defaults to $GCS_BUCKET)")
    parser.add_argument("--project", default=None, help="GCP project (defaults to $GCP_PROJECT_ID)")
    parser.add_argument("--dry-run", action="store_true", help="List and transform but don't touch BQ.")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    import os

    bucket_name = args.bucket or os.environ.get("GCS_BUCKET")
    project = args.project or os.environ.get("GCP_PROJECT_ID")
    if not bucket_name:
        parser.error("Set --bucket or GCS_BUCKET")
    if not project:
        parser.error("Set --project or GCP_PROJECT_ID")

    gcs = storage.Client(project=project)
    bq = bigquery.Client(project=project)

    logger.info("Scanning gs://%s/%s ...", bucket_name, args.prefix)
    blobs = list(gcs.list_blobs(bucket_name, prefix=args.prefix))
    logger.info("Found %d objects", len(blobs))

    all_rows: list[dict] = []
    scanned = 0
    skipped = 0
    failed = 0

    for blob in blobs:
        match = RAW_PATH_RE.match(blob.name)
        if not match:
            skipped += 1
            continue

        frequency, series_id, _period = match.groups()
        try:
            payload = json.loads(blob.download_as_bytes())
            rows = transform_payload(payload, series_id=series_id, frequency=frequency)
        except (json.JSONDecodeError, TransformError) as exc:
            logger.warning("skip %s: %s", blob.name, exc)
            failed += 1
            continue

        all_rows.extend(rows)
        scanned += 1
        if scanned % 500 == 0:
            logger.info("Scanned %d files (%d rows so far)", scanned, len(all_rows))

    logger.info(
        "Scan complete: %d files scanned, %d rows extracted, %d files skipped, %d failed",
        scanned,
        len(all_rows),
        skipped,
        failed,
    )

    if not all_rows:
        logger.info("Nothing to load.")
        return 0

    if args.dry_run:
        logger.info(
            "[dry-run] would load %d rows via one MERGE into %s.raw.observations", len(all_rows), project
        )
        return 0

    logger.info("Loading %d rows into %s.raw.observations via staging + MERGE...", len(all_rows), project)
    loaded = load_rows_with_merge(
        client=bq,
        rows=all_rows,
        target_dataset="raw",
        target_table="observations",
        project=project,
    )
    logger.info("MERGE complete: %d rows written to raw.observations", loaded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
