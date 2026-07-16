# chile-macro-pipeline — Architecture

## 1. Architecture at a glance

Batch ELT with two decoupled Cloud Functions, GCS as the immutable raw landing zone,
BigQuery as the warehouse. Cloud Scheduler drives ingestion; a GCS `finalize` event
triggers transform-and-load.

```
Cloud Scheduler ─► ingest (CF gen2) ─► GCS raw/ ─(finalize)─► transform_load (CF gen2) ─► BigQuery raw.observations ─► analytics.v_* views ─► Looker Studio
                        │
                        └─► Secret Manager (BDE creds)
```

**Why batch, not streaming:** BDE publishes on fixed cadences (daily/monthly/quarterly).
Streaming would add cost with no latency benefit. Batch also makes replay/backfill trivial.

**Why two functions, not one:** Separation of concerns lets us:

- Reprocess bad transformations without re-hitting BDE (raw JSON is preserved in GCS).
- Retry transform_load independently if BQ has issues.
- Backfill by dropping historical JSON into GCS and letting the trigger fire naturally.

Trade-off: slightly more infra than a single function. Worth it for a portfolio piece
that demonstrates raw/curated layering.

## 2. Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Raw file format | JSON (as returned by BDE) | Zero-loss; parsing deferred to transform stage. |
| Raw file path | `raw/{frequency}/{series_id}/{date}.json` | Enables partition-style discovery; dedup by object name. |
| Idempotency in ingest | Check `blob.exists()` before writing; skip if present | Cheap, uses GCS as the source of truth for "already fetched". |
| Idempotency in load | `MERGE INTO raw.observations` on `(series_id, observation_date)` | Handles reruns and late corrections from BDE. |
| Partitioning | BQ partitioned by `observation_date`, clustered by `series_id` | Matches dominant query patterns (time range + per-series). |
| Series metadata | Static `dim_series` seeded from `series.yaml` | Low-churn; version-controlled with code. |
| Config source of truth | `config/series.yaml` | Adding a series = one PR, no code change. |
| Secrets | Secret Manager in prod, `.env` in local | Local uses `python-dotenv`. |
| Timezone | `observation_date` as DATE (no tz); `ingested_at` as TIMESTAMP UTC | BDE dates are calendar dates in CLT. Log ingestion in UTC to avoid DST ambiguity. |
| Transform trigger | GCS `google.cloud.storage.object.v1.finalized` via Eventarc | Decouples the two functions cleanly. |

## 3. Data model

**Raw layer** (`raw.observations`): one row per (series, date). MERGE on load.
Partitioned by `observation_date`, clustered by `series_id`.

**Dimensions:**

- `raw.dim_series` — id, name, unit, frequency, description. Seeded from `series.yaml` on deploy (idempotent MERGE).
- `raw.dim_frequency` — small static lookup (daily/monthly/quarterly + cron schedules for reference).

**Analytics layer** (views, not tables — cheap on free tier):

- `v_tipo_cambio` — USD/CLP + `LAG()` daily change + 7-day MA via `AVG() OVER`.
- `v_uf_diaria` — UF with % daily change.
- `v_tpm` — rate with change-point detection via `LAG` diff.
- `v_ipc_mensual` — CPI + rolling 12m sum for annual inflation.
- `v_imacec` — index + YoY via `LAG(12)`.
- `v_dashboard_consolidado` — wide view joining the above for Looker Studio.

SCD not needed: series definitions are effectively immutable; if BDE changes a code,
treat it as a new series.

## 4. Error handling & failure modes

| Failure | Handling |
|---|---|
| BDE HTTP 5xx / timeout | `requests` with `urllib3.Retry`: 3 attempts, exponential backoff (1s/2s/4s), retry on 429/500/502/503/504. |
| BDE returns `Codigo != 0` | Log error with `Descripcion`, count as failure, continue with next series. Function returns 200 with summary — do NOT crash the whole batch for one bad series. |
| BDE returns empty `Obs[]` | Log warning, do not write empty file (avoids poisoning idempotency check). |
| Observation `statusCode != "OK"` | Load anyway with the flag; downstream views filter to `status_code = 'OK'`. Preserves auditability. |
| GCS write fails | Bubble up — Cloud Function retries automatically on unhandled exception. |
| Transform parse error | Move bad file to `gs://{bucket}/dead_letter/` + log; do not block the queue. |
| BQ load failure | Function fails → Eventarc retries per policy. Idempotent MERGE makes retries safe. |
| Duplicate GCS finalize events | MERGE dedups; also safe. |

**Return contract for ingest:** `{"processed": N, "skipped": M, "errors": [...]}`.

## 5. Backfill strategy

**Scope:** 5 years of history for portfolio credibility.

Standalone `scripts/backfill.py`:

1. Reads `series.yaml`.
2. For each series, chunks the date range by year (daily) or per-series (monthly/quarterly).
3. Calls BDE directly, writes to the same GCS paths — existing transform trigger picks them up.
4. Rate-limits: 500ms between requests.
5. Idempotent: skips existing GCS objects.

Reuses the production transform path — no separate code, and validates the trigger end-to-end.

## 6. Deployment topology & IAM

- **Service accounts** (least-privilege, one per function):
  - `sa-ingest`: `secretmanager.secretAccessor` (on BDE creds only), `storage.objectCreator` (on raw bucket prefix).
  - `sa-transform-load`: `storage.objectViewer` (raw), `bigquery.dataEditor` (raw dataset).
- **Bucket lifecycle:** raw files kept indefinitely (KB-sized). Optional: Nearline after 90 days.
- **Cost:** all in free tier for portfolio-scale use.

## 7. Observability

- **Logging:** structured JSON via `logging` + `JsonFormatter`. Fields: `series_id`, `frequency`, `duration_ms`, `record_count`, `severity`.
- **Metrics:** Cloud Functions built-in + custom log-based counter `bde_api_errors_by_series`.
- **Alerts (v1):**
  - Ingest error rate > 20% over 1h.
  - Zero observations loaded in 24h for a daily series.
- **Freshness check:** scheduled BQ query flagging series with no new rows past their cadence + grace period.

## 8. Open questions / risks

1. **Series codes need verification** against the official XLSX catalog before writing the ingest function.
2. **BDE rate limits are undocumented.** 500ms backfill throttle is conservative.
3. **Historical availability varies by series** — backfill should log gaps, not fail.
4. **Looker Studio auth to BQ:** service account with `bigquery.dataViewer` on analytics dataset only.

## 9. Implementation order

| # | Step |
|---|---|
| 0 | Verify BDE series codes & test API access locally |
| 1 | Scaffolding + `.env.example` + `series.yaml` + `config.py` |
| 2 | `bde_client.py` with retry + response parsing |
| 3 | `storage.py` helpers (idempotent write) |
| 4 | Ingest function `main.py` + date-range logic |
| 5 | `transformer.py` (DD-MM-YYYY parsing, status filter) |
| 6 | `bq_loader.py` with MERGE-based dedup |
| 7 | Transform+load function `main.py` |
| 8 | SQL schema + views |
| 9 | Infrastructure scripts (setup/deploy/scheduler) |
| 10 | `backfill.py` |
| 11 | README |
| 12 | GitHub Actions (lint + test) |
