"""Tests for GCS storage helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from gcs import build_raw_path, upload_json_idempotent
from google.api_core.exceptions import PreconditionFailed


def test_build_raw_path_daily() -> None:
    assert (
        build_raw_path("daily", "F073.TCO.PRE.Z.D", "2026-07-14")
        == "raw/daily/F073.TCO.PRE.Z.D/2026-07-14.json"
    )


def test_build_raw_path_monthly() -> None:
    assert (
        build_raw_path("monthly", "F074.IPC.VAR.Z.Z.C.M", "2026-06")
        == "raw/monthly/F074.IPC.VAR.Z.Z.C.M/2026-06.json"
    )


def test_build_raw_path_rejects_empty() -> None:
    with pytest.raises(ValueError):
        build_raw_path("", "X", "2026-07-14")


def _mock_client(*, blob_exists: bool, upload_raises: Exception | None = None) -> tuple[MagicMock, MagicMock]:
    blob = MagicMock()
    blob.exists.return_value = blob_exists
    if upload_raises is not None:
        blob.upload_from_string.side_effect = upload_raises

    bucket = MagicMock()
    bucket.blob.return_value = blob

    client = MagicMock()
    client.bucket.return_value = bucket
    return client, blob


def test_upload_json_writes_when_new() -> None:
    client, blob = _mock_client(blob_exists=False)

    result = upload_json_idempotent(client, "my-bucket", "raw/daily/X/2026-07-14.json", {"a": 1})

    assert result.written is True
    assert result.gcs_uri == "gs://my-bucket/raw/daily/X/2026-07-14.json"
    blob.upload_from_string.assert_called_once()
    kwargs = blob.upload_from_string.call_args.kwargs
    assert kwargs["content_type"] == "application/json"
    assert kwargs["if_generation_match"] == 0


def test_upload_json_skips_when_exists() -> None:
    client, blob = _mock_client(blob_exists=True)

    result = upload_json_idempotent(client, "my-bucket", "raw/daily/X/2026-07-14.json", {"a": 1})

    assert result.written is False
    blob.upload_from_string.assert_not_called()


def test_upload_json_treats_precondition_failure_as_skip() -> None:
    client, blob = _mock_client(blob_exists=False, upload_raises=PreconditionFailed("race"))

    result = upload_json_idempotent(client, "my-bucket", "raw/daily/X/2026-07-14.json", {"a": 1})

    assert result.written is False


def test_upload_json_reraises_unexpected_errors() -> None:
    client, _ = _mock_client(blob_exists=False, upload_raises=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        upload_json_idempotent(client, "my-bucket", "raw/daily/X/2026-07-14.json", {"a": 1})
