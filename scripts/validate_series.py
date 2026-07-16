"""Verify BDE credentials and confirm every series in config/series.yaml resolves.

Reads BDE_USER / BDE_PASSWORD from a local .env (never committed) and hits GetSeries
for each series with a small date window. Prints an ok/fail summary and exits non-zero
if any series fails — safe to wire into a pre-deploy check.

Usage:
    python scripts/validate_series.py
    python scripts/validate_series.py --config config/series.yaml --days 30
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

# Make functions/ingest/utils importable when running from repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "functions" / "ingest"))

from bde_client import BDEClient, BDEError  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from config import load_bde_credentials, load_series  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "series.yaml"))
    parser.add_argument("--days", type=int, default=30, help="Days of history to request (default: 30)")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    try:
        creds = load_bde_credentials()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 2

    client = BDEClient(credentials=creds)
    series = load_series(args.config)

    last = date.today() - timedelta(days=1)
    first = last - timedelta(days=args.days)

    print(f"Testing {len(series)} series against BDE ({first} -> {last})...\n")

    failures: list[str] = []
    for s in series:
        prefix = f"  [{s.frequency:9}] {s.id:40}"
        try:
            parsed, _ = client.get_series(s.id, first, last)
        except BDEError as exc:
            print(f"{prefix} FAIL  code={exc.code} — {exc.description}")
            failures.append(s.id)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"{prefix} FAIL  {type(exc).__name__}: {exc}")
            failures.append(s.id)
            continue

        n = len(parsed.observations)
        status = "OK  " if n > 0 else "EMPTY"
        print(f"{prefix} {status} {n} observations — {s.name}")

    print()
    if failures:
        print(f"{len(failures)} series failed: {', '.join(failures)}")
        return 1
    print(f"All {len(series)} series validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
