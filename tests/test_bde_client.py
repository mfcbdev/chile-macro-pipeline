"""Tests for the BDE API client."""

from __future__ import annotations

from datetime import date

import pytest
import responses
from bde_client import BDE_BASE_URL, BDEClient, BDEError

from config import BDECredentials


@pytest.fixture
def client() -> BDEClient:
    return BDEClient(credentials=BDECredentials(user="u", password="p"))


def test_bde_credentials_repr_redacts_password() -> None:
    creds = BDECredentials(user="alice@example.com", password="hunter2")
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in str(creds)
    assert "alice@example.com" in repr(creds)


@responses.activate
def test_get_series_success(client: BDEClient) -> None:
    responses.add(
        responses.GET,
        BDE_BASE_URL,
        json={
            "Codigo": 0,
            "Descripcion": "OK",
            "Series": {
                "seriesId": "F073.TCO.PRE.Z.D",
                "descripEsp": "Dólar observado",
                "Obs": [
                    {"indexDateString": "14-07-2026", "value": "950.25", "statusCode": "OK"},
                ],
            },
        },
        status=200,
    )

    parsed, raw = client.get_series("F073.TCO.PRE.Z.D", date(2026, 7, 14), date(2026, 7, 14))

    assert parsed.series_id == "F073.TCO.PRE.Z.D"
    assert len(parsed.observations) == 1
    assert parsed.observations[0]["value"] == "950.25"
    assert raw["Codigo"] == 0


@responses.activate
def test_get_series_api_error_raises(client: BDEClient) -> None:
    responses.add(
        responses.GET,
        BDE_BASE_URL,
        json={"Codigo": -5, "Descripcion": "Series not found"},
        status=200,
    )

    with pytest.raises(BDEError) as exc_info:
        client.get_series("BAD.CODE", date(2026, 7, 14), date(2026, 7, 14))

    assert exc_info.value.code == -5
    assert "Series not found" in str(exc_info.value)


@responses.activate
def test_get_series_empty_observations(client: BDEClient) -> None:
    responses.add(
        responses.GET,
        BDE_BASE_URL,
        json={
            "Codigo": 0,
            "Descripcion": "OK",
            "Series": {"seriesId": "X", "descripEsp": "", "Obs": []},
        },
        status=200,
    )

    parsed, _ = client.get_series("X", date(2026, 7, 14), date(2026, 7, 14))
    assert parsed.is_empty


@responses.activate
def test_get_series_retries_on_5xx(client: BDEClient) -> None:
    responses.add(responses.GET, BDE_BASE_URL, status=503)
    responses.add(responses.GET, BDE_BASE_URL, status=503)
    responses.add(
        responses.GET,
        BDE_BASE_URL,
        json={
            "Codigo": 0,
            "Descripcion": "OK",
            "Series": {"seriesId": "X", "descripEsp": "", "Obs": []},
        },
        status=200,
    )

    parsed, _ = client.get_series("X", date(2026, 7, 14), date(2026, 7, 14))
    assert parsed.series_id == "X"
    assert len(responses.calls) == 3


@responses.activate
def test_get_series_invalid_json_raises(client: BDEClient) -> None:
    responses.add(responses.GET, BDE_BASE_URL, body="<html>server error</html>", status=200)

    with pytest.raises(BDEError) as exc_info:
        client.get_series("X", date(2026, 7, 14), date(2026, 7, 14))

    assert exc_info.value.code == "INVALID_JSON"


@responses.activate
def test_get_series_sends_correct_params(client: BDEClient) -> None:
    responses.add(
        responses.GET,
        BDE_BASE_URL,
        json={"Codigo": 0, "Descripcion": "OK", "Series": {"seriesId": "X", "descripEsp": "", "Obs": []}},
        status=200,
    )

    client.get_series("F073.TCO.PRE.Z.D", date(2024, 1, 1), date(2024, 1, 31))

    call = responses.calls[0]
    assert "timeseries=F073.TCO.PRE.Z.D" in call.request.url
    assert "firstdate=2024-01-01" in call.request.url
    assert "lastdate=2024-01-31" in call.request.url
    assert "function=GetSeries" in call.request.url
    assert "user=u" in call.request.url
