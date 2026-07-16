#!/usr/bin/env bash
# Deploy both Cloud Functions (gen2) with least-privilege SAs.
# - ingest:         HTTP trigger, invoked by Cloud Scheduler
# - transform_load: Eventarc trigger on GCS finalize under raw/
#
# Series config (config/series.yaml) is copied into a build dir alongside the ingest
# function so the deployed source is self-contained.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env"
    set +a
fi

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
: "${GCS_BUCKET:?Set GCS_BUCKET}"
: "${REGION:=us-central1}"

INGEST_SA="sa-ingest@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
TRANSFORM_SA="sa-transform-load@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# -----------------------------------------------------------------------------
# 1. Build ingest source (function dir + series.yaml)
# -----------------------------------------------------------------------------
echo "==> Building ingest source..."
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

cp -r "${REPO_ROOT}/functions/ingest/." "${BUILD_DIR}/"
cp "${REPO_ROOT}/config/series.yaml" "${BUILD_DIR}/series.yaml"

# -----------------------------------------------------------------------------
# 2. Deploy ingest
# -----------------------------------------------------------------------------
echo "==> Deploying function: ingest..."
gcloud functions deploy ingest \
    --project="${GCP_PROJECT_ID}" \
    --region="${REGION}" \
    --gen2 \
    --runtime=python312 \
    --source="${BUILD_DIR}" \
    --entry-point=ingest \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account="${INGEST_SA}" \
    --set-env-vars="GCP_PROJECT_ID=${GCP_PROJECT_ID},GCS_BUCKET=${GCS_BUCKET},USE_SECRET_MANAGER=true" \
    --memory=256Mi \
    --timeout=540s \
    --max-instances=3 \
    --quiet

# -----------------------------------------------------------------------------
# 3. Deploy transform_load
# -----------------------------------------------------------------------------
echo "==> Deploying function: transform_load..."
gcloud functions deploy transform-load \
    --project="${GCP_PROJECT_ID}" \
    --region="${REGION}" \
    --gen2 \
    --runtime=python312 \
    --source="${REPO_ROOT}/functions/transform_load" \
    --entry-point=transform_load \
    --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
    --trigger-event-filters="bucket=${GCS_BUCKET}" \
    --service-account="${TRANSFORM_SA}" \
    --set-env-vars="GCP_PROJECT_ID=${GCP_PROJECT_ID},BQ_DATASET=raw,BQ_TABLE=observations" \
    --memory=512Mi \
    --timeout=300s \
    --max-instances=5 \
    --retry \
    --quiet

echo
echo "==> Deploy complete."
echo "    Ingest URL:"
gcloud functions describe ingest --project="${GCP_PROJECT_ID}" --region="${REGION}" --gen2 \
    --format="value(serviceConfig.uri)"
echo "    Next: ./infrastructure/scheduler.sh"
