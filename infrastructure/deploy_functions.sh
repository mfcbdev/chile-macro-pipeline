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
# 0. Grant the default build service account the roles it needs.
# Cloud Functions gen2 uses the Compute Engine default SA as the build SA, which
# on newer projects does NOT have the required build roles by default. Without
# this, the first `gcloud functions deploy` fails with a cryptic
# "missing permission on the build service account" error.
# See: https://cloud.google.com/functions/docs/troubleshooting#build-service-account
# -----------------------------------------------------------------------------
echo "==> Ensuring build service account has required roles..."
PROJECT_NUMBER="$(gcloud projects describe "${GCP_PROJECT_ID}" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for role in \
    roles/cloudbuild.builds.builder \
    roles/artifactregistry.writer \
    roles/logging.logWriter \
    roles/storage.objectViewer; do
    gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
        --member="serviceAccount:${BUILD_SA}" \
        --role="${role}" \
        --condition=None \
        --quiet >/dev/null
done

# Additional roles required specifically for the Eventarc-triggered function:
# 1. Cloud Storage service agent must be able to publish to Pub/Sub (GCS Eventarc uses Pub/Sub under the hood).
# 2. Eventarc service agent must be able to read the bucket to validate the trigger.
# 3. The transform_load SA must be marked as an eventarc.eventReceiver.
# Without these, the transform_load deploy fails with cryptic "Permission storage.buckets.get denied"
# or "Cloud Storage service account unable to publish to Pub/Sub" errors.
echo "==> Ensuring Eventarc + Storage service agents have required roles..."

# Force-provision the Cloud Storage service agent. `gcloud storage service-agent` returns
# the *expected* email but does NOT provision the SA on a fresh project — calling the
# Storage REST API's serviceAccount endpoint is what actually triggers provisioning.
STORAGE_SA="$(
    curl -s \
        -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        "https://storage.googleapis.com/storage/v1/projects/${GCP_PROJECT_ID}/serviceAccount" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['email_address'])"
)"
EVENTARC_SA="service-${PROJECT_NUMBER}@gcp-sa-eventarc.iam.gserviceaccount.com"

# Give IAM ~30s to propagate before we try to bind roles to the freshly-provisioned SA.
sleep 30

gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
    --member="serviceAccount:${STORAGE_SA}" \
    --role="roles/pubsub.publisher" \
    --condition=None --quiet >/dev/null

gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
    --member="serviceAccount:${EVENTARC_SA}" \
    --role="roles/storage.legacyBucketReader" \
    --quiet >/dev/null

gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
    --member="serviceAccount:${TRANSFORM_SA}" \
    --role="roles/eventarc.eventReceiver" \
    --condition=None --quiet >/dev/null

# One more short wait before Eventarc trigger validation kicks in during deploy.
sleep 30

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
# The Eventarc trigger invokes transform_load via its Cloud Run URL using
# sa-transform-load as the caller identity. That SA needs run.invoker on its
# own Cloud Run service — Cloud Run enforces this even for internally-generated
# calls. Without this, Eventarc events land as "request was not authenticated"
# 403s in the transform-load logs and BQ never gets rows.
# The service must already exist for this binding, so we run it AFTER the first
# deploy attempt below — the initial deploy creates the service, we then bind
# the role and rely on Eventarc's built-in retry to replay any failed events.
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

# Grant sa-transform-load run.invoker on the just-deployed Cloud Run service so
# Eventarc calls carrying its identity can actually invoke the function.
echo "==> Granting sa-transform-load invoker on its Cloud Run service..."
gcloud run services add-iam-policy-binding transform-load \
    --project="${GCP_PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${TRANSFORM_SA}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null

echo
echo "==> Deploy complete."
echo "    Ingest URL:"
gcloud functions describe ingest --project="${GCP_PROJECT_ID}" --region="${REGION}" --gen2 \
    --format="value(serviceConfig.uri)"
echo "    Next: ./infrastructure/scheduler.sh"
