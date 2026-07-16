"""Integration-style tests for the ingest orchestration.

The BDE client and GCS client are mocked; date logic and config loading are real.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bde_client import BDEError, BDESeriesResponse

# Load functions/ingest/main.py explicitly to avoid the main.py collision with transform_load/main.py.
_INGEST_MAIN_PATH = Path(__file__).resolve().parents[1] / "functions" / "ingest" / "main.py"
_spec = importlib.util.spec_from_file_location("ingest_main", _INGEST_MAIN_PATH)
ingest_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest_main)


@pytest.fixture
def series_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "series.yaml"
    p.write_text(
        """
series:
  daily:
    - id: "F073.TCO.PRE.Z.D"
      name: "Dólar observado"
      unit: "CLP/USD"
    - id: "F073.UFF.PRE.Z.D"
      name: "UF"
      unit: "CLP"
  monthly:
    - id: "F074.IPC.VAR.Z.Z.C.M"
      name: "IPC"
      unit: "%"
""",
        encoding="utf-8",
    )
    return p


def _bde_response(series_id: str, obs_count: int = 1) -> tuple[BDESeriesResponse, dict]:
    obs = [{"indexDateString": "13-07-2026", "value": "1.0", "statusCode": "OK"}] * obs_count
    parsed = BDESeriesResponse(series_id=series_id, description="", observations=obs)
    raw = {"Codigo": 0, "Descripcion": "OK", "Series": {"seriesId": series_id, "descripEsp": "", "Obs": obs}}
    return parsed, raw


def _mock_gcs_client(*, existing_paths: set[str] | None = None) -> MagicMock:
    """A GCS client where blob.exists() returns True only for paths in existing_paths."""
    existing_paths = existing_paths or set()

    def _make_blob(path: str) -> MagicMock:
        blob = MagicMock()
        blob.exists.return_value = path in existing_paths
        return blob

    bucket = MagicMock()
    bucket.blob.side_effect = _make_blob
    client = MagicMock()
    client.bucket.return_value = bucket
    return client


def test_run_ingest_writes_all_daily_series(series_yaml: Path) -> None:
    bde = MagicMock()
    bde.get_series.side_effect = lambda sid, _f, _l: _bde_response(sid)
    gcs = _mock_gcs_client()

    summary = ingest_main.run_ingest(
        frequency="daily",
        bucket_name="test-bucket",
        config_path=str(series_yaml),
        today=date(2026, 7, 14),
        bde_client=bde,
        gcs_client=gcs,
    )

    assert summary["processed"] == 2
    assert summary["skipped"] == 0
    assert summary["errors"] == []
    assert summary["period_label"] == "2026-07-13"
    assert bde.get_series.call_count == 2


def test_run_ingest_only_processes_requested_frequency(series_yaml: Path) -> None:
    bde = MagicMock()
    bde.get_series.side_effect = lambda sid, _f, _l: _bde_response(sid)
    gcs = _mock_gcs_client()

    summary = ingest_main.run_ingest(
        frequency="monthly",
        bucket_name="b",
        config_path=str(series_yaml),
        today=date(2026, 7, 5),
        bde_client=bde,
        gcs_client=gcs,
    )

    assert summary["processed"] == 1
    assert summary["period_label"] == "2026-06"
    assert bde.get_series.call_count == 1


def test_run_ingest_skips_existing_files_without_calling_bde(series_yaml: Path) -> None:
    existing = {
        "raw/daily/F073.TCO.PRE.Z.D/2026-07-13.json",
        "raw/daily/F073.UFF.PRE.Z.D/2026-07-13.json",
    }
    bde = MagicMock()
    gcs = _mock_gcs_client(existing_paths=existing)

    summary = ingest_main.run_ingest(
        frequency="daily",
        bucket_name="b",
        config_path=str(series_yaml),
        today=date(2026, 7, 14),
        bde_client=bde,
        gcs_client=gcs,
    )

    assert summary["processed"] == 0
    assert summary["skipped"] == 2
    bde.get_series.assert_not_called()


def test_run_ingest_isolates_per_series_errors(series_yaml: Path) -> None:
    def _side_effect(sid: str, _f, _l):
        if sid == "F073.TCO.PRE.Z.D":
            raise BDEError(code=-5, description="Not found", series_id=sid)
        return _bde_response(sid)

    bde = MagicMock()
    bde.get_series.side_effect = _side_effect
    gcs = _mock_gcs_client()

    summary = ingest_main.run_ingest(
        frequency="daily",
        bucket_name="b",
        config_path=str(series_yaml),
        today=date(2026, 7, 14),
        bde_client=bde,
        gcs_client=gcs,
    )

    assert summary["processed"] == 1
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["series_id"] == "F073.TCO.PRE.Z.D"


def test_run_ingest_records_empty_series(series_yaml: Path) -> None:
    def _side_effect(sid: str, _f, _l):
        if sid == "F073.TCO.PRE.Z.D":
            return _bde_response(sid, obs_count=0)
        return _bde_response(sid)

    bde = MagicMock()
    bde.get_series.side_effect = _side_effect
    gcs = _mock_gcs_client()

    summary = ingest_main.run_ingest(
        frequency="daily",
        bucket_name="b",
        config_path=str(series_yaml),
        today=date(2026, 7, 14),
        bde_client=bde,
        gcs_client=gcs,
    )

    assert summary["processed"] == 1
    assert summary["empty"] == ["F073.TCO.PRE.Z.D"]
