# chile-macro-pipeline

Serverless ETL pipeline for Chilean macroeconomic indicators from the Banco Central de Chile REST API (BDE), running on Google Cloud Platform. Ingests daily/monthly/quarterly series into BigQuery for analysis in Looker Studio.

Portfolio project. Emphasises clean architecture, idempotency, least-privilege IAM, and testable code.

---

## Architecture

```
Cloud Scheduler ─► ingest (Cloud Functions gen2) ─► GCS raw/ ─(finalize event)─► transform_load (Cloud Functions gen2) ─► BigQuery raw.observations ─► analytics.v_* views ─► Looker Studio
                          │
                          └─► Secret Manager (BDE creds)
```

Full architecture rationale in [`docs/architecture.md`](docs/architecture.md).

**Key properties:**
- **Idempotent** — GCS `if_generation_match=0` at write; BQ `MERGE` on `(series_id, observation_date)` at load. Safe to replay any run.
- **Least-privilege IAM** — separate service accounts for ingest (write to raw bucket + read BDE secret) and transform_load (read raw bucket + write raw dataset).
- **Immutable raw layer** — original BDE JSON preserved in GCS forever, enabling replay of downstream transformations without re-hitting the API.
- **Decoupled** — Eventarc trigger lets you drop historical files into `raw/` (backfill) and everything downstream fires automatically.

---

## Repository layout

```
chile-macro-pipeline/
├── config/series.yaml               # Series catalog — add series here
├── docs/architecture.md
├── functions/
│   ├── ingest/                      # HTTP-triggered by Cloud Scheduler
│   └── transform_load/              # Event-triggered by GCS finalize
├── sql/
│   ├── schema/                      # Datasets + raw tables
│   ├── seed/dim_series.sql          # MERGE UPSERT from series.yaml
│   └── views/                       # analytics.v_* — for Looker Studio
├── scripts/
│   ├── validate_series.py           # Verify BDE credentials + series codes
│   └── backfill.py                  # Load historical data into GCS
├── infrastructure/
│   ├── setup.sh                     # APIs, bucket, datasets, secrets, IAM, schemas
│   ├── deploy_functions.sh          # gcloud functions deploy for both functions
│   └── scheduler.sh                 # Cloud Scheduler jobs (daily/monthly/quarterly)
├── tests/                           # pytest (79 tests, all mocks — no live GCP)
└── .github/workflows/{lint,test}.yml
```

---

## Series ingested

| Frequency | Series ID | Name | Unit |
|---|---|---|---|
| daily | `F073.TCO.PRE.Z.D` | Dólar observado | CLP/USD |
| daily | `F073.UFF.PRE.Z.D` | Unidad de Fomento (UF) | CLP |
| daily | `F022.TPM.TIN.D001.NO.Z.D` | Tasa de política monetaria (TPM) | % |
| monthly | `F074.IPC.VAR.Z.Z.C.M` | IPC variación mensual | % |
| monthly | `F032.IMC.IND.Z.Z.EP18.Z.Z.0.M` | IMACEC | índice (2018=100) |
| quarterly | `F032.PIB.FLU.R.CLP.EP18.Z.Z.0.T` | PIB real | MM CLP encadenados |

Adding a series: append to `config/series.yaml`, add a row to `sql/seed/dim_series.sql` (or regenerate it), redeploy the ingest function. If it's a new indicator worth its own view, drop a `sql/views/v_*.sql` file.

---

## Setup

### 0. Prerequisites

- GCP account with a project + billing enabled
- `gcloud` CLI authenticated: `gcloud auth login` and `gcloud auth application-default login`
- Python 3.12
- BDE API credentials — register at https://si3.bcentral.cl/estadisticas/principal1/web_services/index.htm

### 1. Local env

```bash
cp .env.example .env
# Edit .env with:
#   BDE_USER, BDE_PASSWORD           — from BDE portal
#   GCP_PROJECT_ID                    — your GCP project
#   GCS_BUCKET                        — bucket name (globally unique)
#   REGION=us-central1                — or your preferred region

python -m venv .venv
.venv/bin/pip install -r requirements.txt   # or .venv/Scripts/pip on Windows
```

### 2. Verify BDE access + series codes

```bash
python scripts/validate_series.py --days 400
```

Should print `OK` for every series with an observation count. Fix any `FAIL`/`EMPTY` before proceeding.

### 3. Provision GCP resources

```bash
gcloud config set project $GCP_PROJECT_ID
./infrastructure/setup.sh
```

This enables APIs, creates the bucket + datasets, three service accounts (with least-privilege IAM), the two BDE secrets in Secret Manager, and runs every SQL file under `sql/`.

### 4. Deploy the functions

```bash
./infrastructure/deploy_functions.sh
```

Deploys `ingest` (HTTP) and `transform_load` (Eventarc on GCS finalize).

### 5. Wire up Cloud Scheduler

```bash
./infrastructure/scheduler.sh
```

Creates three jobs in `America/Santiago`:

| Job | Cron | Payload |
|---|---|---|
| `ingest-daily` | `0 9 * * 1-5` (Mon–Fri 09:00 CLT) | `{"frequency": "daily"}` |
| `ingest-monthly` | `0 10 5 * *` (5th of each month) | `{"frequency": "monthly"}` |
| `ingest-quarterly` | `0 10 15 1,4,7,10 *` (15th of Jan/Apr/Jul/Oct) | `{"frequency": "quarterly"}` |

### 6. (Optional) Backfill history

```bash
python scripts/backfill.py --years 5
```

Writes 5 years of raw JSON to GCS. The `transform_load` function processes each file automatically as it arrives. Rate-limited to 500ms between BDE calls; safe to re-run (skips existing files).

### 7. Connect Looker Studio

- Data source → BigQuery → project `${GCP_PROJECT_ID}` → dataset `analytics` → view `v_dashboard_consolidado`.
- For a dedicated view per indicator, use `v_tipo_cambio`, `v_ipc_mensual`, etc.

---

## Companion visualization

[**chile-macro-dashboard**](https://github.com/mfcbdev/chile-macro-dashboard) — a Next.js 15 + Cloud Run frontend that reads the `analytics.v_*` views directly from BigQuery (no separate API), rendering six KPIs and per-series detail charts with ES/EN + dark/light toggles.

**Live:** https://chile-macro-dashboard-636532693335.us-central1.run.app

---

## Development

```bash
pytest -q                    # 79 tests, all mocks — no GCP needed
ruff check .                 # lint
ruff format .                # auto-format
```

Adding tests: pytest discovers `tests/test_*.py`; `pyproject.toml` puts function dirs and `scripts/` on `pythonpath` so tests can import them directly.

---

## Operations

### Manually trigger an ingest

```bash
URL=$(gcloud functions describe ingest --region=$REGION --gen2 --format='value(serviceConfig.uri)')
curl -X POST "$URL" \
    -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
    -H "Content-Type: application/json" \
    -d '{"frequency": "daily"}'
```

### Inspect what got loaded

```sql
SELECT frequency, COUNT(*) AS rows, MAX(observation_date) AS latest, MAX(ingested_at) AS last_load
FROM `raw.observations`
GROUP BY frequency
ORDER BY frequency;
```

### Freshness check (worth scheduling)

```sql
WITH latest AS (
    SELECT series_id, MAX(observation_date) AS latest_obs, MAX(ingested_at) AS last_load
    FROM `raw.observations`
    GROUP BY series_id
)
SELECT
    l.series_id,
    s.name,
    l.latest_obs,
    DATE_DIFF(CURRENT_DATE(), l.latest_obs, DAY) AS days_stale
FROM latest l
LEFT JOIN `raw.dim_series` s USING (series_id)
ORDER BY days_stale DESC;
```

### Common troubleshooting

| Symptom | Cause / fix |
|---|---|
| Ingest returns `errors[]` with `BDEError code -50` | Bad series code — verify in [BDE catalog](https://si3.bcentral.cl/estadisticas/Principal1/Web_Services/Webservices/series.xlsx) and update `config/series.yaml`. |
| Transform_load logs `Raw file is not valid JSON` | BDE returned an HTML error page instead of JSON — usually transient. File remains in `raw/` for reprocess. |
| Duplicate rows in `raw.observations` | Should be impossible thanks to MERGE, but check `sql/views/*.sql` — some views assume a single row per date. |
| Scheduler job fires but function 403s | Check `sa-scheduler-invoker` has `roles/run.invoker` on the `ingest` Cloud Run service (deploy_functions.sh + scheduler.sh set this up). |

---

## Security notes

- BDE credentials **must never** be committed. `.env` is gitignored (line 25). In production they live only in Secret Manager.
- If credentials are ever transmitted through chat/logs/PRs, rotate them by deactivating and reactivating API access on the BDE portal.
- `BDECredentials.__repr__` is redacted so accidental logging shows `password='***'`.
- Cloud Function ingress: `--no-allow-unauthenticated`; only the scheduler SA can invoke.

---

## Cost

Runs comfortably in GCP free tier at portfolio scale:

- Cloud Functions gen2: 2M invocations/mo free (we use ~30/mo)
- BigQuery: 1TB queries + 10GB storage free (raw payloads are KB-sized)
- GCS: 5GB free
- Secret Manager: 6 secrets + 10K access ops/mo free
- Cloud Scheduler: 3 jobs free

---

## License

MIT.
