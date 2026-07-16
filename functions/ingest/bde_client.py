"""Client for the Banco Central de Chile REST API (SieteRestWS).

Docs: https://si3.bcentral.cl/estadisticas/principal1/web_services/index.htm
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import BDECredentials

logger = logging.getLogger(__name__)

BDE_BASE_URL = "https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx"
DEFAULT_TIMEOUT = 30  # seconds


class BDEError(Exception):
    """Raised when the BDE API returns a non-zero Codigo or an unparseable response."""

    def __init__(self, code: int | str, description: str, series_id: str | None = None):
        self.code = code
        self.description = description
        self.series_id = series_id
        super().__init__(f"BDE error [{code}] for {series_id or '<no series>'}: {description}")


@dataclass(frozen=True)
class BDESeriesResponse:
    """Parsed successful response from GetSeries."""

    series_id: str
    description: str
    observations: list[dict[str, str]]  # raw obs dicts as returned by BDE

    @property
    def is_empty(self) -> bool:
        return not self.observations


class BDEClient:
    """Thin wrapper around the BDE REST endpoint with retry + structured errors."""

    def __init__(
        self,
        credentials: BDECredentials,
        base_url: str = BDE_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ):
        self._credentials = credentials
        self._base_url = base_url
        self._timeout = timeout
        self._session = session or self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1.0,  # 1s, 2s, 4s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def get_series(
        self,
        series_id: str,
        first_date: date,
        last_date: date,
    ) -> tuple[BDESeriesResponse, dict[str, Any]]:
        """Call GetSeries. Returns (parsed_response, raw_json).

        The raw JSON is returned alongside the parsed view so callers can persist it verbatim.

        Raises:
            BDEError: when the API returns Codigo != 0 or the response is malformed.
            requests.RequestException: on network/HTTP failure after retries exhausted.
        """
        params = {
            "user": self._credentials.user,
            "pass": self._credentials.password,
            "timeseries": series_id,
            "firstdate": first_date.isoformat(),
            "lastdate": last_date.isoformat(),
            "function": "GetSeries",
        }

        logger.info(
            "Calling BDE GetSeries",
            extra={
                "series_id": series_id,
                "first_date": params["firstdate"],
                "last_date": params["lastdate"],
            },
        )

        response = self._session.get(self._base_url, params=params, timeout=self._timeout)
        response.raise_for_status()

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise BDEError(
                code="INVALID_JSON",
                description=f"Response was not valid JSON: {exc}",
                series_id=series_id,
            ) from exc

        code = payload.get("Codigo")
        description = payload.get("Descripcion", "")
        if code != 0:
            raise BDEError(
                code=code if code is not None else "MISSING", description=description, series_id=series_id
            )

        series_block = payload.get("Series") or {}
        obs = series_block.get("Obs") or []

        parsed = BDESeriesResponse(
            series_id=series_block.get("seriesId", series_id),
            description=series_block.get("descripEsp", ""),
            observations=obs,
        )
        return parsed, payload
