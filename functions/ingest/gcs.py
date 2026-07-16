"""Google Cloud Storage helpers for the ingest function."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from google.cloud import storage  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadResult:
    """Outcome of an idempotent upload attempt."""

    bucket: str
    path: str
    written: bool  # True if uploaded, False if skipped because object already existed

    @property
    def gcs_uri(self) -> str:
        return f"gs://{self.bucket}/{self.path}"


def build_raw_path(frequency: str, series_id: str, period_label: str) -> str:
    """Return the canonical GCS object path for a raw BDE payload.

    period_label is the date/period the file represents (e.g. "2026-07-14" or "2026-06").
    """
    if not frequency or not series_id or not period_label:
        raise ValueError("frequency, series_id, and period_label are all required")
    return f"raw/{frequency}/{series_id}/{period_label}.json"


def upload_json_idempotent(
    client: storage.Client,
    bucket_name: str,
    path: str,
    payload: dict[str, Any],
) -> UploadResult:
    """Upload `payload` as JSON to gs://bucket_name/path if the object does not already exist.

    Uses the GCS `x-goog-if-generation-match: 0` precondition so two concurrent writers cannot
    both create the object; the loser gets a 412 and we treat it as "already existed".
    """
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(path)

    if blob.exists():
        logger.info("Skipping upload; object already exists", extra={"bucket": bucket_name, "path": path})
        return UploadResult(bucket=bucket_name, path=path, written=False)

    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

    try:
        # if_generation_match=0 means "only create if the object does not exist yet"
        blob.upload_from_string(body, content_type="application/json", if_generation_match=0)
    except Exception as exc:
        # Precondition failure = another writer beat us; treat as skipped, not an error.
        # google-cloud-storage raises google.api_core.exceptions.PreconditionFailed (412).
        # We import lazily to avoid a hard dep in test envs.
        from google.api_core.exceptions import PreconditionFailed  # type: ignore[import-untyped]

        if isinstance(exc, PreconditionFailed):
            logger.info(
                "Upload race: object created by concurrent writer",
                extra={"bucket": bucket_name, "path": path},
            )
            return UploadResult(bucket=bucket_name, path=path, written=False)
        raise

    logger.info(
        "Uploaded raw payload",
        extra={"bucket": bucket_name, "path": path, "size_bytes": len(body)},
    )
    return UploadResult(bucket=bucket_name, path=path, written=True)
