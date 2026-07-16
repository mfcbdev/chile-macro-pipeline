"""Tests for the BigQuery loader.

The BQ client is fully mocked; we verify the sequence of calls (load → merge → delete)
and the shape of the MERGE SQL, not real BQ behavior.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bq_loader import OBSERVATIONS_SCHEMA, load_rows_with_merge


def _mock_client(project: str = "my-project") -> MagicMock:
    client = MagicMock()
    client.project = project

    # load_table_from_json returns a LoadJob whose .result() returns None
    load_job = MagicMock()
    load_job.result.return_value = None
    client.load_table_from_json.return_value = load_job

    # get_table returns a table object that we can mutate .expires on
    table = MagicMock()
    table.expires = None
    client.get_table.return_value = table

    # query returns a QueryJob
    query_job = MagicMock()
    query_job.result.return_value = None
    client.query.return_value = query_job

    return client


def _sample_rows() -> list[dict]:
    return [
        {
            "series_id": "F073.TCO.PRE.Z.D",
            "observation_date": "2026-07-14",
            "value": 950.25,
            "status_code": "OK",
            "frequency": "daily",
            "ingested_at": "2026-07-14T12:00:00+00:00",
        }
    ]


def test_load_rows_empty_short_circuits() -> None:
    client = _mock_client()
    n = load_rows_with_merge(client, [], "raw", "observations")
    assert n == 0
    client.load_table_from_json.assert_not_called()
    client.query.assert_not_called()


def test_load_rows_full_flow() -> None:
    client = _mock_client()
    rows = _sample_rows()

    n = load_rows_with_merge(client, rows, "raw", "observations")

    assert n == 1

    # Loaded into a staging table under raw.
    client.load_table_from_json.assert_called_once()
    call = client.load_table_from_json.call_args
    passed_rows, staging_ref = call.args
    assert passed_rows == rows
    assert staging_ref.startswith("my-project.raw._staging_observations_")

    # Schema was passed in the LoadJobConfig.
    job_config = call.kwargs["job_config"]
    assert job_config.schema == OBSERVATIONS_SCHEMA

    # Staging expiration was set.
    client.get_table.assert_called_once_with(staging_ref)
    client.update_table.assert_called_once()

    # MERGE SQL references both tables and the natural key.
    client.query.assert_called_once()
    merge_sql = client.query.call_args.args[0]
    assert f"`{staging_ref}`" in merge_sql
    assert "`my-project.raw.observations`" in merge_sql
    assert "series_id" in merge_sql and "observation_date" in merge_sql
    assert "MERGE" in merge_sql
    assert "WHEN MATCHED THEN" in merge_sql
    assert "WHEN NOT MATCHED THEN" in merge_sql

    # Staging was cleaned up.
    client.delete_table.assert_called_once_with(staging_ref, not_found_ok=True)


def test_load_rows_uses_explicit_project() -> None:
    client = _mock_client(project="default-project")
    load_rows_with_merge(client, _sample_rows(), "raw", "observations", project="override")

    call = client.load_table_from_json.call_args
    _rows, staging_ref = call.args
    assert staging_ref.startswith("override.raw._staging_observations_")


def test_load_rows_still_cleans_up_staging_when_merge_fails() -> None:
    client = _mock_client()
    client.query.side_effect = RuntimeError("merge boom")

    import contextlib

    with contextlib.suppress(RuntimeError):
        load_rows_with_merge(client, _sample_rows(), "raw", "observations")

    client.delete_table.assert_called_once()


@pytest.mark.parametrize(
    ("project", "dataset", "table"),
    [
        ("bad`project", "raw", "observations"),
        ("valid-project", "raw; DROP TABLE X;", "observations"),
        ("valid-project", "raw", "obs`ervations"),
        ("valid-project", "raw", ""),
    ],
)
def test_load_rows_rejects_malicious_identifiers(project: str, dataset: str, table: str) -> None:
    client = _mock_client()
    with pytest.raises(ValueError, match="Invalid BigQuery"):
        load_rows_with_merge(client, _sample_rows(), dataset, table, project=project)
    client.load_table_from_json.assert_not_called()


def test_staging_table_names_are_unique() -> None:
    client = _mock_client()
    seen = set()
    for _ in range(5):
        load_rows_with_merge(client, _sample_rows(), "raw", "observations")
    for call in client.load_table_from_json.call_args_list:
        _rows, staging_ref = call.args
        seen.add(staging_ref)
    assert len(seen) == 5
