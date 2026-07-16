"""Tests for the transform_load orchestration.

Storage + BigQuery clients are mocked; the transformer runs for real end-to-end.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

# Load functions/transform_load/main.py explicitly to avoid the main.py collision with ingest/main.py.
_TL_MAIN_PATH = Path(__file__).resolve().parents[1] / "functions" / "transform_load" / "main.py"
_spec = importlib.util.spec_from_file_location("transform_load_main", _TL_MAIN_PATH)
tl_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tl_main)


def _payload_bytes(obs: list[dict], series_id: str = "F073.TCO.PRE.Z.D") -> bytes:
    return json.dumps(
        {"Codigo": 0, "Descripcion": "OK", "Series": {"seriesId": series_id, "descripEsp": "", "Obs": obs}}
    ).encode("utf-8")


def _mock_gcs_returning(body: bytes) -> MagicMock:
    blob = MagicMock()
    blob.download_as_bytes.return_value = body
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket
    return client


def _mock_bq_client(project: str = "test-project") -> MagicMock:
    client = MagicMock()
    client.project = project
    load_job = MagicMock()
    load_job.result.return_value = None
    client.load_table_from_json.return_value = load_job
    table = MagicMock()
    table.expires = None
    client.get_table.return_value = table
    query_job = MagicMock()
    query_job.result.return_value = None
    client.query.return_value = query_job
    return client


def test_process_file_happy_path() -> None:
    body = _payload_bytes([{"indexDateString": "14-07-2026", "value": "950.25", "statusCode": "OK"}])
    gcs = _mock_gcs_returning(body)
    bq = _mock_bq_client()

    loaded = tl_main.process_file(
        gcs_client=gcs,
        bq_client=bq,
        bucket_name="raw-bucket",
        object_path="raw/daily/F073.TCO.PRE.Z.D/2026-07-14.json",
        series_id="F073.TCO.PRE.Z.D",
        frequency="daily",
        target_dataset="raw",
        target_table="observations",
    )

    assert loaded == 1
    gcs.bucket.assert_called_once_with("raw-bucket")
    bq.load_table_from_json.assert_called_once()
    bq.query.assert_called_once()
    # The rows passed to BQ should have been transformed correctly.
    passed_rows, _staging = bq.load_table_from_json.call_args.args
    assert passed_rows == [
        {
            "series_id": "F073.TCO.PRE.Z.D",
            "observation_date": "2026-07-14",
            "value": 950.25,
            "status_code": "OK",
            "frequency": "daily",
            "ingested_at": passed_rows[0]["ingested_at"],
        }
    ]


def test_process_file_skips_on_invalid_json() -> None:
    gcs = _mock_gcs_returning(b"<not json>")
    bq = _mock_bq_client()

    loaded = tl_main.process_file(
        gcs_client=gcs,
        bq_client=bq,
        bucket_name="b",
        object_path="raw/daily/X/2026-07-14.json",
        series_id="X",
        frequency="daily",
        target_dataset="raw",
        target_table="observations",
    )

    assert loaded == 0
    bq.load_table_from_json.assert_not_called()


def test_process_file_skips_on_transform_error() -> None:
    # Non-zero Codigo means transformer will raise.
    body = json.dumps({"Codigo": -5, "Descripcion": "bad"}).encode("utf-8")
    gcs = _mock_gcs_returning(body)
    bq = _mock_bq_client()

    loaded = tl_main.process_file(
        gcs_client=gcs,
        bq_client=bq,
        bucket_name="b",
        object_path="raw/daily/X/2026-07-14.json",
        series_id="X",
        frequency="daily",
        target_dataset="raw",
        target_table="observations",
    )

    assert loaded == 0
    bq.load_table_from_json.assert_not_called()


def test_process_file_skips_when_no_rows() -> None:
    body = _payload_bytes([])
    gcs = _mock_gcs_returning(body)
    bq = _mock_bq_client()

    loaded = tl_main.process_file(
        gcs_client=gcs,
        bq_client=bq,
        bucket_name="b",
        object_path="raw/daily/X/2026-07-14.json",
        series_id="X",
        frequency="daily",
        target_dataset="raw",
        target_table="observations",
    )

    assert loaded == 0
    bq.load_table_from_json.assert_not_called()


def test_raw_path_regex_matches_expected_shapes() -> None:
    good = [
        "raw/daily/F073.TCO.PRE.Z.D/2026-07-14.json",
        "raw/monthly/F074.IPC.VAR.Z.Z.C.M/2026-06.json",
        "raw/quarterly/F032.PIB.FLU.R.CLP.EP18.Z.Z.0.T/2026-Q2.json",
    ]
    for path in good:
        assert tl_main.RAW_PATH_RE.match(path), f"should match: {path}"


def test_raw_path_regex_rejects_bad_shapes() -> None:
    bad = [
        "raw/hourly/X/2026-07-14.json",  # unknown frequency
        "raw/daily/X/2026-07-14.txt",  # wrong extension
        "dead_letter/daily/X/2026-07-14.json",  # not under raw/
        "raw/daily/X.json",  # missing series or period segment
        "raw/daily/X/nested/2026-07-14.json",  # extra path component
    ]
    for path in bad:
        assert not tl_main.RAW_PATH_RE.match(path), f"should not match: {path}"
