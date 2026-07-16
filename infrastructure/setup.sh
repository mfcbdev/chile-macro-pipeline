#!/usr/bin/env bash
# Provision GCP resources for chile-macro-pipeline.
# Idempotent: safe to re-run. Requires: gcloud, bq, jq (optional).
#
# Prereqs:
#   1. `gcloud auth login` and `gcloud auth application-default login`
#   2. Env vars set (in .env or shell): GCP_PROJECT_ID, GCS_BUCKET, REGION
#   3. `gcloud config set project "$GCP_PROJECT_ID"`
#
# What this creates:
#   - Enables Cloud Functions, Run, Build, Storage, BigQuery, Secret Manager, Eventarc, Scheduler APIs
#   - GCS bucket for raw JSON
#   - Two BQ datasets: raw, analytics
#   - Two service accounts: sa-ingest, sa-transform-load (least-privilege)
#   - Two secrets: bde-user, bde-password (values read from local .env, if present)
#   - Schemas + views (runs every .sql under sql/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load .env if present (does NOT export secrets to committed logs)
if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env"
    set +a
fi

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID (in .env or env)}"
: "${GCS_BUCKET:?Set GCS_BUCKET (in .env or env)}"
: "${REGION:=us-central1}"

echo "Project:  ${GCP_PROJECT_ID}"
echo "Bucket:   ${GCS_BUCKET}"
echo "Region:   ${REGION}"
echo

# -----------------------------------------------------------------------------
# 1. Enable APIs
# -----------------------------------------------------------------------------
echo "==> Enabling required APIs..."
gcloud services enable \
    cloudfunctions.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    storage.googleapis.com \
    bigquery.googleapis.com \
    secretmanager.googleapis.com \
    eventarc.googleapis.com \
    cloudscheduler.googleapis.com \
    logging.googleapis.com \
    --project="${GCP_PROJECT_ID}" \
    --quiet

# -----------------------------------------------------------------------------
# 2. GCS bucket for raw JSON
# -----------------------------------------------------------------------------
echo "==> Creating GCS bucket gs://${GCS_BUCKET}..."
if ! gcloud storage buckets describe "gs://${GCS_BUCKET}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${GCS_BUCKET}" \
        --project="${GCP_PROJECT_ID}" \
        --location="${REGION}" \
        --uniform-bucket-level-access
else
    echo "    Bucket already exists — skipping."
fi

# -----------------------------------------------------------------------------
# 3. BigQuery datasets
# -----------------------------------------------------------------------------
echo "==> Creating BQ datasets..."
for ds in raw analytics; do
    if ! bq --project_id="${GCP_PROJECT_ID}" show --dataset "${GCP_PROJECT_ID}:${ds}" >/dev/null 2>&1; then
        bq --project_id="${GCP_PROJECT_ID}" mk --dataset --location="${REGION}" "${GCP_PROJECT_ID}:${ds}"
    else
        echo "    Dataset ${ds} already exists — skipping."
    fi
done

# -----------------------------------------------------------------------------
# 4. Service accounts (least-privilege)
# -----------------------------------------------------------------------------
echo "==> Creating service accounts..."
for sa in sa-ingest sa-transform-load sa-scheduler-invoker; do
    if ! gcloud iam service-accounts describe "${sa}@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
        --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
        gcloud iam service-accounts create "${sa}" \
            --project="${GCP_PROJECT_ID}" \
            --display-name="${sa}"
    else
        echo "    ${sa} already exists — skipping."
    fi
done

INGEST_SA="sa-ingest@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
TRANSFORM_SA="sa-transform-load@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="sa-scheduler-invoker@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# -----------------------------------------------------------------------------
# 5. Secrets — bde-user and bde-password
# -----------------------------------------------------------------------------
echo "==> Ensuring BDE secrets exist in Secret Manager..."
create_or_add_version() {
    local secret_id="$1"
    local value="$2"
    if ! gcloud secrets describe "${secret_id}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
        printf "%s" "${value}" | gcloud secrets create "${secret_id}" \
            --project="${GCP_PROJECT_ID}" \
            --replication-policy="automatic" \
            --data-file=-
        echo "    Created secret ${secret_id}."
    else
        # Only add a new version if the current value differs (avoids unbounded versioning on re-runs).
        current="$(gcloud secrets versions access latest --secret="${secret_id}" --project="${GCP_PROJECT_ID}" 2>/dev/null || echo "")"
        if [[ "${current}" != "${value}" ]]; then
            printf "%s" "${value}" | gcloud secrets versions add "${secret_id}" \
                --project="${GCP_PROJECT_ID}" \
                --data-file=-
            echo "    Added new version of ${secret_id}."
        else
            echo "    ${secret_id} unchanged — skipping."
        fi
    fi
}

if [[ -n "${BDE_USER:-}" && -n "${BDE_PASSWORD:-}" ]]; then
    create_or_add_version "bde-user" "${BDE_USER}"
    create_or_add_version "bde-password" "${BDE_PASSWORD}"
else
    echo "    BDE_USER / BDE_PASSWORD not set in .env — create secrets manually:"
    echo "      echo -n 'YOUR_USER' | gcloud secrets create bde-user --project=${GCP_PROJECT_ID} --replication-policy=automatic --data-file=-"
    echo "      echo -n 'YOUR_PASSWORD' | gcloud secrets create bde-password --project=${GCP_PROJECT_ID} --replication-policy=automatic --data-file=-"
fi

# Grant per-secret accessor to the ingest SA only.
for secret_id in bde-user bde-password; do
    if gcloud secrets describe "${secret_id}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
        gcloud secrets add-iam-policy-binding "${secret_id}" \
            --project="${GCP_PROJECT_ID}" \
            --member="serviceAccount:${INGEST_SA}" \
            --role="roles/secretmanager.secretAccessor" \
            --condition=None \
            --quiet >/dev/null
    fi
done

# -----------------------------------------------------------------------------
# 6. IAM — least-privilege bindings
# -----------------------------------------------------------------------------
echo "==> Granting IAM roles..."

# Ingest SA: write to raw bucket
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
    --member="serviceAccount:${INGEST_SA}" \
    --role="roles/storage.objectCreator" \
    --quiet >/dev/null

# Transform-load SA: read from raw bucket
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
    --member="serviceAccount:${TRANSFORM_SA}" \
    --role="roles/storage.objectViewer" \
    --quiet >/dev/null

# Transform-load SA: dataEditor on raw dataset (for MERGE + staging tables)
bq --project_id="${GCP_PROJECT_ID}" add-iam-policy-binding \
    --member="serviceAccount:${TRANSFORM_SA}" \
    --role="roles/bigquery.dataEditor" \
    "${GCP_PROJECT_ID}:raw" >/dev/null

# Transform-load SA: jobUser at project level (needed to run MERGE queries)
gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
    --member="serviceAccount:${TRANSFORM_SA}" \
    --role="roles/bigquery.jobUser" \
    --condition=None \
    --quiet >/dev/null

# Eventarc service agent needs pubsub publisher + eventarc event receiver on the GCS bucket's project
# (Cloud Functions gen2 handles this automatically on first deploy — no manual step needed.)

# -----------------------------------------------------------------------------
# 7. Apply SQL schemas + views
# -----------------------------------------------------------------------------
echo "==> Applying SQL schemas + seed + views..."
run_sql_file() {
    local file="$1"
    echo "    → ${file}"
    bq --project_id="${GCP_PROJECT_ID}" query \
        --use_legacy_sql=false \
        --location="${REGION}" \
        < "${file}"
}

# Order matters: datasets → tables → seed → views
for f in "${REPO_ROOT}"/sql/schema/*.sql; do run_sql_file "${f}"; done
for f in "${REPO_ROOT}"/sql/seed/*.sql;   do run_sql_file "${f}"; done
for f in "${REPO_ROOT}"/sql/views/*.sql;  do run_sql_file "${f}"; done

echo
echo "==> Setup complete."
echo "    Next: ./infrastructure/deploy_functions.sh"
