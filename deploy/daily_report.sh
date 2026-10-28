#!/usr/bin/env bash
# Deploys the daily brief as a Cloud Run Job triggered by Cloud Scheduler at
# 7:00 AM America/Los_Angeles, Monday-Friday (DST handled by Scheduler).
#
# Prerequisites (one time):
#   gcloud auth login && gcloud config set project <PROJECT_ID>
#   gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
#       secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
#   Create each secret listed in SECRETS below, e.g.
#       printf '%s' "<value>" | gcloud secrets create SF_CLIENT_SECRET --data-file=-
#
# Usage: deploy/daily_report.sh            (re-run safely to update)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="${REGION:-us-west1}"
JOB="${JOB:-barrister-daily-report}"
SA_NAME="${SA_NAME:-daily-report}"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULE="${SCHEDULE:-0 7 * * 1-5}"
RECIPIENTS="${REPORT_RECIPIENTS:?set REPORT_RECIPIENTS, comma-separated}"

# Non-secret settings. Fill in before the first deploy.
ENV_VARS="REPORT_RECIPIENTS=${RECIPIENTS}"
ENV_VARS+=",REPORT_FROM=${REPORT_FROM:?set REPORT_FROM, e.g. reports@barrister-suites.com}"
ENV_VARS+=",SMTP_HOST=${SMTP_HOST:?set SMTP_HOST, e.g. smtp.gmail.com}"
ENV_VARS+=",SMTP_PORT=${SMTP_PORT:-587}"
ENV_VARS+=",SMTP_USERNAME=${SMTP_USERNAME:?set SMTP_USERNAME}"
ENV_VARS+=",SF_DOMAIN_URL=${SF_DOMAIN_URL:?set SF_DOMAIN_URL, e.g. https://barrister.my.salesforce.com}"
ENV_VARS+=",GA4_PROPERTY_ID=${GA4_PROPERTY_ID:?set GA4_PROPERTY_ID}"
ENV_VARS+=",GOOGLE_ADS_CUSTOMER_ID=${GOOGLE_ADS_CUSTOMER_ID:?set GOOGLE_ADS_CUSTOMER_ID}"
[[ -n "${GOOGLE_ADS_LOGIN_CUSTOMER_ID:-}" ]] && ENV_VARS+=",GOOGLE_ADS_LOGIN_CUSTOMER_ID=${GOOGLE_ADS_LOGIN_CUSTOMER_ID}"

# Secret Manager secret name == env var name, always the latest version.
SECRETS=(SMTP_PASSWORD SF_CLIENT_ID SF_CLIENT_SECRET GOOGLE_ADS_CLIENT_ID GOOGLE_ADS_CLIENT_SECRET
         GOOGLE_ADS_REFRESH_TOKEN GOOGLE_ADS_DEVELOPER_TOKEN)
SECRET_FLAGS=$(for s in "${SECRETS[@]}"; do printf '%s=%s:latest,' "$s" "$s"; done | sed 's/,$//')

if ! gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --display-name "Barrister daily report"
fi
for s in "${SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:$SA" \
    --role roles/secretmanager.secretAccessor >/dev/null
done

# Builds the repo's Dockerfile and runs `python -m daily_report` instead of the MCP server.
gcloud run jobs deploy "$JOB" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --command python --args=-m,daily_report \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRET_FLAGS" \
  --max-retries 1 \
  --task-timeout 10m

# Scheduler calls the Run Admin API as the same service account.
gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" \
  --member "serviceAccount:$SA" --role roles/run.invoker >/dev/null

URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "$JOB" --location "$REGION" >/dev/null 2>&1; then
  ACTION=update
else
  ACTION=create
fi
gcloud scheduler jobs "$ACTION" http "$JOB" \
  --location "$REGION" \
  --schedule "$SCHEDULE" \
  --time-zone "America/Los_Angeles" \
  --uri "$URI" \
  --http-method POST \
  --oauth-service-account-email "$SA"

echo "Deployed. Test now with: gcloud run jobs execute $JOB --region $REGION --wait"
