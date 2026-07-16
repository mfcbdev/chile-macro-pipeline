"""Historical backfill for BDE series.

Reads config/series.yaml, calls the BDE API in chunks, and writes raw JSON to GCS at
the same paths used by the ingest Cloud Function. The transform_load function's Eventarc
trigger then transforms + loads each chunk into BigQuery automatically.

Chunk strategy per frequency:
  daily     — one file per calendar day     (BDE query batched by year for efficiency)
  monthly   — one file per calendar month
  quarterly — one file per calendar quarter (labelled YYYY-Q{1..4})

The daily strategy is subtle: the pipeline's ingest function writes one file per day
under `raw/daily/{series}/YYYY-MM-DD.json`, so backfill must produce those same
one-day files. We fetch a full year in a single BDE call (much fewer round-trips), then
split the response into per-day payloads matching the ingest format.

Idempotent: skips objects that already exist in GCS.
Rate-limited: sleeps between BDE calls (default 500ms).

Usage:
    python scripts/backfill.py --years 5
    python scripts/backfill.py --start 2020-01-01 --end 2024-12-31
    python scripts/backfill.py --frequency daily --start 2024-01-01 --end 2024-12-31
    python scripts/backfill.py --dry-run   # print what would happen, do not call BDE or write

Requires GCS_BUCKET env var (or --bucket) and BDE credentials in .env.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from calendar import monthrange
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "functions" / "ingest"))

from bde_client import BDEClient, BDEError  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from gcs import build_raw_path, upload_json_idempotent  # noqa: E402

from config import Series, load_bde_credentials, load_series  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")


@dataclass(frozen=True)
class BackfillStats:
    written: int = 0
    skipped: int = 0
    empty: int = 0
    errors: int = 0

    def merge(self, **kwargs: int) -> BackfillStats:
        return BackfillStats(
            written=self.written + kwargs.get("written", 0),
            skipped=self.skipped + kwargs.get("skipped", 0),
            empty=self.empty + kwargs.get("empty", 0),
            errors=self.errors + kwargs.get("errors", 0),
        )


def _end_of_month(d: date) -> date:
    return d.replace(day=monthrange(d.year, d.month)[1])


def _iter_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _iter_months(start: date, end: date):
    """Yield (first_day, last_day) for each calendar month covered by [start, end]."""
    cursor = start.replace(day=1)
    while cursor <= end:
        first = cursor
        last = _end_of_month(cursor)
        yield first, last
        cursor = last + timedelta(days=1)


def _iter_quarters(start: date, end: date):
    """Yield (first_day, last_day, label) for each calendar quarter covered by [start, end]."""
    q_first_month = ((start.month - 1) // 3) * 3 + 1
    cursor = date(start.year, q_first_month, 1)
    while cursor <= end:
        q = (cursor.month - 1) // 3 + 1
        last_month = cursor.month + 2
        last = date(cursor.year, last_month, monthrange(cursor.year, last_month)[1])
        label = f"{cursor.year:04d}-Q{q}"
        yield cursor, last, label
        # advance to next quarter
        cursor = date(
            cursor.year + (1 if last_month == 12 else 0), 1 if last_month == 12 else last_month + 1, 1
        )


def _split_yearly_response_by_day(
    yearly_raw: dict[str, Any],
    year: int,
) -> dict[date, dict[str, Any]]:
    """Turn a single BDE response spanning a year into per-day payloads keyed by date.

    Each per-day payload mirrors the shape of a live daily-ingest payload: same top-level
    Codigo/Descripcion/Series envelope, with Obs containing just the one observation.
    """
    obs = (yearly_raw.get("Series") or {}).get("Obs") or []
    by_day: dict[date, dict[str, Any]] = {}
    for o in obs:
        try:
            d = datetime.strptime(o["indexDateString"], "%d-%m-%Y").date()
        except (KeyError, ValueError):
            logger.warning("Skipping observation with bad date: %r", o)
            continue
        if d.year != year:
            continue
        single = deepcopy(yearly_raw)
        series_block = single.setdefault("Series", {})
        series_block["Obs"] = [o]
        by_day[d] = single
    return by_day


def _backfill_daily(
    series: Series,
    start: date,
    end: date,
    client: BDEClient,
    gcs_client,
    bucket: str,
    delay_s: float,
    dry_run: bool,
) -> BackfillStats:
    """Batch by year to minimise BDE calls; split into per-day GCS objects."""
    stats = BackfillStats()

    for year in range(start.year, end.year + 1):
        year_start = max(start, date(year, 1, 1))
        year_end = min(end, date(year, 12, 31))

        # Cheap pre-check: if every target file for this year already exists, skip the call.
        target_days = list(_iter_days(year_start, year_end))
        target_paths = [build_raw_path(series.frequency, series.id, d.isoformat()) for d in target_days]

        if not dry_run:
            bucket_obj = gcs_client.bucket(bucket)
            existing = {p for p in target_paths if bucket_obj.blob(p).exists()}
            missing = [(d, p) for d, p in zip(target_days, target_paths, strict=True) if p not in existing]
            if not missing:
                logger.info("[%s %d] all %d days already in GCS", series.id, year, len(target_days))
                stats = stats.merge(skipped=len(target_days))
                continue
        else:
            missing = list(zip(target_days, target_paths, strict=True))

        logger.info(
            "[%s %d] fetching %d–%d (%d days needed)", series.id, year, year_start, year_end, len(missing)
        )

        if dry_run:
            stats = stats.merge(written=len(missing))
            continue

        try:
            _, raw = client.get_series(series.id, year_start, year_end)
        except BDEError as exc:
            logger.error("[%s %d] BDE error: %s", series.id, year, exc)
            stats = stats.merge(errors=1)
            continue

        by_day = _split_yearly_response_by_day(raw, year)

        for d, path in missing:
            payload = by_day.get(d)
            if payload is None:
                stats = stats.merge(empty=1)
                continue
            result = upload_json_idempotent(gcs_client, bucket, path, payload)
            stats = stats.merge(written=1) if result.written else stats.merge(skipped=1)

        time.sleep(delay_s)

    return stats


def _backfill_monthly(
    series: Series,
    start: date,
    end: date,
    client: BDEClient,
    gcs_client,
    bucket: str,
    delay_s: float,
    dry_run: bool,
) -> BackfillStats:
    stats = BackfillStats()
    for first, last in _iter_months(start, end):
        label = f"{first.year:04d}-{first.month:02d}"
        path = build_raw_path(series.frequency, series.id, label)

        if not dry_run and gcs_client.bucket(bucket).blob(path).exists():
            stats = stats.merge(skipped=1)
            continue

        logger.info("[%s] fetching %s", series.id, label)
        if dry_run:
            stats = stats.merge(written=1)
            continue

        try:
            parsed, raw = client.get_series(series.id, first, last)
        except BDEError as exc:
            logger.error("[%s %s] BDE error: %s", series.id, label, exc)
            stats = stats.merge(errors=1)
            continue

        if parsed.is_empty:
            stats = stats.merge(empty=1)
            continue

        result = upload_json_idempotent(gcs_client, bucket, path, raw)
        stats = stats.merge(written=1) if result.written else stats.merge(skipped=1)
        time.sleep(delay_s)
    return stats


def _backfill_quarterly(
    series: Series,
    start: date,
    end: date,
    client: BDEClient,
    gcs_client,
    bucket: str,
    delay_s: float,
    dry_run: bool,
) -> BackfillStats:
    stats = BackfillStats()
    for first, last, label in _iter_quarters(start, end):
        path = build_raw_path(series.frequency, series.id, label)

        if not dry_run and gcs_client.bucket(bucket).blob(path).exists():
            stats = stats.merge(skipped=1)
            continue

        logger.info("[%s] fetching %s", series.id, label)
        if dry_run:
            stats = stats.merge(written=1)
            continue

        try:
            parsed, raw = client.get_series(series.id, first, last)
        except BDEError as exc:
            logger.error("[%s %s] BDE error: %s", series.id, label, exc)
            stats = stats.merge(errors=1)
            continue

        if parsed.is_empty:
            stats = stats.merge(empty=1)
            continue

        result = upload_json_idempotent(gcs_client, bucket, path, raw)
        stats = stats.merge(written=1) if result.written else stats.merge(skipped=1)
        time.sleep(delay_s)
    return stats


BACKFILL_DISPATCH = {
    "daily": _backfill_daily,
    "monthly": _backfill_monthly,
    "quarterly": _backfill_quarterly,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "series.yaml"))
    parser.add_argument("--bucket", default=None, help="GCS bucket name (defaults to $GCS_BUCKET)")
    parser.add_argument("--years", type=int, default=None, help="Number of years back from today")
    parser.add_argument("--start", type=date.fromisoformat, default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--frequency",
        choices=("daily", "monthly", "quarterly"),
        default=None,
        help="Restrict backfill to one frequency (default: all)",
    )
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between BDE calls (default: 0.5)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not call BDE or write; print planned work."
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    # Resolve date window
    today = date.today()
    if args.start and args.end:
        start, end = args.start, args.end
    elif args.years:
        end = today - timedelta(days=1)
        start = end.replace(year=end.year - args.years)
    else:
        parser.error("Provide either --years N or both --start and --end")

    if start > end:
        parser.error(f"start ({start}) is after end ({end})")

    # Resolve bucket
    import os

    bucket = args.bucket or os.environ.get("GCS_BUCKET")
    if not bucket and not args.dry_run:
        parser.error("Set --bucket or GCS_BUCKET (or use --dry-run)")

    all_series = load_series(args.config)
    if args.frequency:
        all_series = [s for s in all_series if s.frequency == args.frequency]

    logger.info(
        "Backfilling %d series from %s to %s (delay=%.2fs, dry_run=%s)",
        len(all_series),
        start,
        end,
        args.delay,
        args.dry_run,
    )

    # Lazy imports of clients so --dry-run works with zero GCP setup
    client = None
    gcs_client = None
    if not args.dry_run:
        creds = load_bde_credentials()
        client = BDEClient(credentials=creds)
        from google.cloud import storage  # type: ignore[import-untyped]

        gcs_client = storage.Client()

    total = BackfillStats()
    for series in all_series:
        fn = BACKFILL_DISPATCH[series.frequency]
        stats = fn(series, start, end, client, gcs_client, bucket, args.delay, args.dry_run)
        logger.info(
            "[%s] done: %d written, %d skipped, %d empty, %d errors",
            series.id,
            stats.written,
            stats.skipped,
            stats.empty,
            stats.errors,
        )
        total = total.merge(
            written=stats.written, skipped=stats.skipped, empty=stats.empty, errors=stats.errors
        )

    logger.info(
        "Backfill complete: %d written, %d skipped, %d empty, %d errors",
        total.written,
        total.skipped,
        total.empty,
        total.errors,
    )
    return 0 if total.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
