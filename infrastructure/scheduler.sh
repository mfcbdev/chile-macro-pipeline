#!/usr/bin/env bash
# Create Cloud Scheduler jobs that invoke the ingest function on daily/monthly/quarterly cadences.
# Uses OIDC auth: the scheduler-invoker SA is granted roles/run.invoker on the ingest function.

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
: "${REGION:=us-central1}"
: "${TIMEZONE:=America/Santiago}"

SCHEDULER_SA="sa-scheduler-invoker@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# Resolve the ingest function URL
INGEST_URL="$(
    gcloud functions describe ingest \
        --project="${GCP_PROJECT_ID}" \
        --region="${REGION}" \
        --gen2 \
        --format='value(serviceConfig.uri)'
)"
if [[ -z "${INGEST_URL}" ]]; then
    echo "Could not resolve ingest function URL — did you run deploy_functions.sh?"
    exit 1
fi
echo "Ingest URL: ${INGEST_URL}"

# Grant the scheduler SA permission to invoke the underlying Cloud Run service.
# Cloud Functions gen2 is Cloud Run under the hood; roles/run.invoker is the right role.
gcloud run services add-iam-policy-binding ingest \
    --project="${GCP_PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${SCHEDULER_SA}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null

create_or_update_job() {
    local job_name="$1"
    local cron_expr="$2"
    local frequency="$3"

    local args=(
        --project="${GCP_PROJECT_ID}"
        --location="${REGION}"
        --schedule="${cron_expr}"
        --time-zone="${TIMEZONE}"
        --uri="${INGEST_URL}"
        --http-method=POST
        --headers="Content-Type=application/json"
        --message-body="{\"frequency\": \"${frequency}\"}"
        --oidc-service-account-email="${SCHEDULER_SA}"
        --oidc-token-audience="${INGEST_URL}"
        --attempt-deadline=540s
    )

    if gcloud scheduler jobs describe "${job_name}" \
        --project="${GCP_PROJECT_ID}" \
        --location="${REGION}" >/dev/null 2>&1; then
        echo "==> Updating job: ${job_name}"
        gcloud scheduler jobs update http "${job_name}" "${args[@]}" --quiet >/dev/null
    else
        echo "==> Creating job: ${job_name}"
        gcloud scheduler jobs create http "${job_name}" "${args[@]}" --quiet >/dev/null
    fi
}

create_or_update_job "ingest-daily"     "0 9 * * 1-5"          "daily"
create_or_update_job "ingest-monthly"   "0 10 5 * *"           "monthly"
create_or_update_job "ingest-quarterly" "0 10 15 1,4,7,10 *"   "quarterly"

echo
echo "==> Scheduler jobs configured (timezone: ${TIMEZONE})."
gcloud scheduler jobs list --project="${GCP_PROJECT_ID}" --location="${REGION}" \
    --filter="name~ingest-" --format="table(name.basename(),schedule,state,lastAttemptTime)"
