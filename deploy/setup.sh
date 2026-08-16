#!/usr/bin/env bash
# One-shot provisioning + deploy for DeployGuard.
# Usage:  ./deploy/setup.sh            provision + deploy everything
#         ./deploy/setup.sh --teardown remove billable resources after the demo
set -euo pipefail

# shellcheck disable=SC1091
[ -f .env ] && source .env

: "${PROJECT_ID:?set PROJECT_ID in .env}"
REGION="${REGION:-europe-west1}"
TOPIC="${PUBSUB_TOPIC:-deploy-failures}"
SUB="${PUBSUB_SUBSCRIPTION:-deploy-failures-agent}"
AGENT_SA="deployguard-agent@${PROJECT_ID}.iam.gserviceaccount.com"
APPROVER_SA="deployguard-approver@${PROJECT_ID}.iam.gserviceaccount.com"

teardown() {
  echo ">> Tearing down billable resources..."
  gcloud run services delete deployguard-agent --region "$REGION" -q || true
  gcloud run services delete deployguard-approver --region "$REGION" -q || true
  gcloud run services delete deployguard-sweeper --region "$REGION" -q || true
  gcloud scheduler jobs delete deployguard-sweep --location "$REGION" -q || true
  gcloud pubsub subscriptions delete "$SUB" -q || true
  gcloud pubsub topics delete "$TOPIC" -q || true
  echo ">> Done. Firestore data left intact (free tier)."
  exit 0
}
[ "${1:-}" = "--teardown" ] && teardown

echo ">> Enabling APIs..."
gcloud services enable \
  run.googleapis.com pubsub.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com \
  aiplatform.googleapis.com cloudtrace.googleapis.com \
  --project "$PROJECT_ID"

echo ">> Firestore (Native mode)..."
gcloud firestore databases create --location="$REGION" --project "$PROJECT_ID" 2>/dev/null || true

echo ">> Service accounts (least privilege)..."
gcloud iam service-accounts create deployguard-agent --project "$PROJECT_ID" 2>/dev/null || true
gcloud iam service-accounts create deployguard-approver --project "$PROJECT_ID" 2>/dev/null || true
for role in roles/datastore.user roles/aiplatform.user roles/cloudtrace.agent \
            roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${AGENT_SA}" --role "$role" -q
done
# Only the approver may access cluster-write secrets / mutate the cluster.
for role in roles/datastore.user roles/secretmanager.secretAccessor \
            roles/container.developer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${APPROVER_SA}" --role "$role" -q
done

echo ">> Secrets..."
create_secret () { # name value
  gcloud secrets create "$1" --project "$PROJECT_ID" 2>/dev/null || true
  printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- --project "$PROJECT_ID"
}
create_secret slack-bot-token       "${SLACK_BOT_TOKEN:-changeme}"
create_secret slack-signing-secret  "${SLACK_SIGNING_SECRET:-changeme}"

echo ">> Pub/Sub..."
gcloud pubsub topics create "$TOPIC" --project "$PROJECT_ID" 2>/dev/null || true

echo ">> Building + deploying agent (Cloud Run)..."
gcloud run deploy deployguard-agent \
  --source . --region "$REGION" --project "$PROJECT_ID" \
  --service-account "$AGENT_SA" --no-allow-unauthenticated --min-instances 0 \
  --set-env-vars "GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash},FIRESTORE_DATABASE=(default),MOCK_K8S=${MOCK_K8S:-1}" \
  --set-secrets "SLACK_BOT_TOKEN=slack-bot-token:latest" \
  --command "" --args "" \
  --port 8080 \
  --quiet \
  --set-build-env-vars "" \
  --dockerfile deploy/Dockerfile.agent 2>/dev/null || \
gcloud run deploy deployguard-agent --source . --region "$REGION" --project "$PROJECT_ID"

AGENT_URL=$(gcloud run services describe deployguard-agent --region "$REGION" \
  --project "$PROJECT_ID" --format='value(status.url)')

echo ">> Pub/Sub push subscription (ack deadline 300s)..."
gcloud pubsub subscriptions create "$SUB" \
  --topic "$TOPIC" --project "$PROJECT_ID" \
  --push-endpoint "${AGENT_URL}" \
  --ack-deadline 300 \
  --push-auth-service-account "$AGENT_SA" 2>/dev/null || true

echo ">> Deploying approver + sweeper..."
gcloud run deploy deployguard-approver \
  --source . --region "$REGION" --project "$PROJECT_ID" \
  --service-account "$APPROVER_SA" --allow-unauthenticated --min-instances 0 \
  --set-secrets "SLACK_SIGNING_SECRET=slack-signing-secret:latest" \
  --dockerfile deploy/Dockerfile.approver 2>/dev/null || true

gcloud run deploy deployguard-sweeper \
  --source . --region "$REGION" --project "$PROJECT_ID" \
  --service-account "$APPROVER_SA" --no-allow-unauthenticated --min-instances 0 \
  --dockerfile deploy/Dockerfile.sweeper 2>/dev/null || true

SWEEPER_URL=$(gcloud run services describe deployguard-sweeper --region "$REGION" \
  --project "$PROJECT_ID" --format='value(status.url)' 2>/dev/null || echo "")

echo ">> Cloud Scheduler (sweeper every 5 min)..."
gcloud scheduler jobs create http deployguard-sweep \
  --location "$REGION" --project "$PROJECT_ID" \
  --schedule "*/5 * * * *" \
  --uri "${SWEEPER_URL}/sweep" --http-method POST \
  --oidc-service-account-email "$APPROVER_SA" 2>/dev/null || true

echo ""
echo "✅ Done."
echo "   Agent URL:    $AGENT_URL"
echo "   Trigger demo: ./scripts/simulate_failure.sh crashloop"
