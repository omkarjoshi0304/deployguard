# DeployGuard 🛡️

> **Autonomous deployment-failure triage & remediation agent**
> Built for the All Things Agentic Hackathon — **The Taskmaster** track.

When a Kubernetes deployment fails, DeployGuard wakes up on its own, gathers the
evidence (pod events, logs, the diff that shipped), reasons about the root cause
with **Gemini 3.5 Flash**, checks its memory of past incidents, and posts a
diagnosis + proposed fix to Slack — then applies the fix **only after a human
approves**. No engineer opens a laptop to triage.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design and
[`docs/PLAN.md`](docs/PLAN.md) for the build plan.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Reasoning | **Gemini 3.5 Flash** (Vertex AI) |
| Agent framework | **Google ADK** (Python) |
| Compute | **Cloud Run** (scales to zero) |
| Eventing | **Cloud Pub/Sub** |
| State / memory | **Firestore** |
| Secrets | **Secret Manager** |
| Recovery | **Cloud Scheduler** (sweeper) |
| Cluster access | **GKE Connect Gateway** + Workload Identity |
| Observability | **OpenTelemetry → Cloud Trace** |

---

## Repository Layout

```
agent/       ADK triage agent (Cloud Run, Pub/Sub-triggered)  — decides
approver/    Slack callback + idempotency guard (Cloud Run)    — acts
sweeper/     Expires stuck actions (Cloud Scheduler)           — recovers
watcher/     In-cluster K8s event watcher (Phase 3)            — real events
deploy/      Dockerfiles + one-shot setup script
scripts/     Demo helpers (simulate a failure)
docs/        Architecture + plan
```

---

## Prerequisites

- A Google Cloud project with billing enabled (use the $150 hackathon credits).
- `gcloud` CLI authenticated: `gcloud auth login && gcloud config set project <PROJECT_ID>`
- Python 3.11+
- (Phase 3) A GKE Autopilot cluster registered with the Connect Gateway.

---

## Quickstart

### 1. Configure

```bash
cp .env.example .env
# edit .env with your PROJECT_ID, REGION, SLACK_* values
```

### 2. Provision GCP resources (one shot)

```bash
./deploy/setup.sh
```

This creates the Pub/Sub topic + subscription, the Firestore database, the
Cloud Scheduler sweeper job, secrets, and deploys the two Cloud Run services.

### 3. Run the demo

```bash
./scripts/simulate_failure.sh crashloop
```

Watch the agent triage the failure in the Cloud Run logs, then check Slack for
the diagnosis + Approve/Reject buttons.

### 4. Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# run the agent locally against the emulators / real GCP
functions-framework --target=handle_pubsub --source=agent/main.py --port=8080
```

---

## Cost Control

All services scale to zero. Set a budget alert (`$20`) in the Cloud Console.
**Turn everything off after recording the demo** — the video is the proof of
execution, not a live service.

```bash
./deploy/setup.sh --teardown
```

---

## License

MIT — see `LICENSE`. Built during the hackathon submission period (Aug 2026).
