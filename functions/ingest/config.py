"""Configuration loading: series catalog and environment/secret resolution."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)

Frequency = Literal["daily", "monthly", "quarterly"]
VALID_FREQUENCIES: tuple[Frequency, ...] = ("daily", "monthly", "quarterly")


@dataclass(frozen=True)
class Series:
    """A BDE series to ingest."""

    id: str
    name: str
    unit: str
    frequency: Frequency
    description: str = ""


@dataclass(frozen=True, repr=False)
class BDECredentials:
    """BDE API credentials. `repr` is redacted to prevent accidental password logging."""

    user: str
    password: str

    def __repr__(self) -> str:
        return f"BDECredentials(user={self.user!r}, password='***')"


def load_series(config_path: str | Path) -> list[Series]:
    """Parse series.yaml into a flat list of Series objects with frequency attached."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if "series" not in data:
        raise ValueError(f"{path} missing top-level 'series' key")

    out: list[Series] = []
    for freq, entries in data["series"].items():
        if freq not in VALID_FREQUENCIES:
            raise ValueError(f"Invalid frequency '{freq}' in {path}; expected one of {VALID_FREQUENCIES}")
        for entry in entries or []:
            out.append(
                Series(
                    id=entry["id"],
                    name=entry["name"],
                    unit=entry["unit"],
                    frequency=freq,  # type: ignore[arg-type]
                    description=entry.get("description", ""),
                )
            )
    return out


def filter_by_frequency(series: list[Series], frequency: Frequency) -> list[Series]:
    return [s for s in series if s.frequency == frequency]


def load_bde_credentials(project_id: str | None = None) -> BDECredentials:
    """Load BDE credentials.

    Prefers Secret Manager (production) when GCP_PROJECT_ID is set; falls back to env vars (local dev).
    """
    project = project_id or os.environ.get("GCP_PROJECT_ID")
    use_secret_manager = os.environ.get("USE_SECRET_MANAGER", "").lower() in ("1", "true", "yes")

    if project and use_secret_manager:
        return _load_credentials_from_secret_manager(project)

    user = os.environ.get("BDE_USER")
    password = os.environ.get("BDE_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "BDE credentials not found. Set BDE_USER and BDE_PASSWORD env vars, "
            "or set USE_SECRET_MANAGER=true with GCP_PROJECT_ID."
        )
    return BDECredentials(user=user, password=password)


def _load_credentials_from_secret_manager(project_id: str) -> BDECredentials:
    from google.cloud import secretmanager  # type: ignore[import-untyped]

    client = secretmanager.SecretManagerServiceClient()

    def _access(secret_id: str) -> str:
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")

    return BDECredentials(user=_access("bde-user"), password=_access("bde-password"))
