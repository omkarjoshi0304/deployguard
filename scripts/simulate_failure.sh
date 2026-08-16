#!/usr/bin/env bash
# Publish a synthetic failure event to Pub/Sub to drive a reproducible demo.
# Usage:  ./scripts/simulate_failure.sh [crashloop|oom|envvar]
set -euo pipefail

# shellcheck disable=SC1091
[ -f .env ] && source .env
: "${PROJECT_ID:?set PROJECT_ID in .env}"
TOPIC="${PUBSUB_TOPIC:-deploy-failures}"
SCENARIO="${1:-crashloop}"

case "$SCENARIO" in
  crashloop)
    REASON="ImagePullBackOff"; IMAGE="gcr.io/acme/checkout:v2.3.1" ;;
  oom)
    REASON="OOMKilled";        IMAGE="gcr.io/acme/checkout:v2.3.0" ;;
  envvar)
    REASON="CrashLoopBackOff"; IMAGE="gcr.io/acme/checkout:v2.3.0" ;;
  *)
    echo "unknown scenario: $SCENARIO (use crashloop|oom|envvar)"; exit 1 ;;
esac

read -r -d '' PAYLOAD <<JSON || true
{
  "source": "simulated",
  "namespace": "prod",
  "workload": "checkout-api",
  "pod": "checkout-api-7d9f-xk2",
  "reason": "${REASON}",
  "last_deploy": {
    "revision": "checkout-api-00042",
    "image": "${IMAGE}",
    "commit": "a1b2c3d"
  },
  "observed_at": "2026-08-20T02:14:00Z"
}
JSON

echo ">> Publishing '${SCENARIO}' (${REASON}) to ${TOPIC}..."
gcloud pubsub topics publish "$TOPIC" \
  --project "$PROJECT_ID" \
  --message "$PAYLOAD"
echo ">> Sent. Watch: gcloud run services logs read deployguard-agent --region \$REGION"
