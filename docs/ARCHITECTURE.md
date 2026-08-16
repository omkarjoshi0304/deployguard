# DeployGuard — Architecture

> Autonomous deployment-failure triage & remediation agent
> Track: **The Taskmaster** · All Things Agentic Hackathon

---

## 1. The One-Sentence Pitch

When a deployment fails, DeployGuard wakes up on its own, gathers the evidence
(logs, pod events, the diff that shipped), reasons about the root cause with
Gemini 3.5 Flash, checks its memory of past incidents, and posts a diagnosis +
proposed fix to Slack — then applies the fix **only after a human approves**.
No engineer opens a laptop to triage.

---

## 2. System Architecture (data flow)

```
  ┌────────────────────┐        ┌──────────────────────┐
  │  EVENT SOURCES     │        │  (A) Simulated:      │
  │                    │        │  gcloud pubsub       │
  │  • GKE cluster     │        │  publish (dev/demo)  │
  │    (CrashLoop,     │───────▶│                      │
  │     failed rollout)│        │  (B) Real: in-cluster│
  │  • GitHub Actions  │        │  watcher / CI webhook│
  │    (CI failure)    │        └──────────┬───────────┘
  └────────────────────┘                   │
                                           ▼
                            ┌──────────────────────────┐
                            │  Pub/Sub topic           │
                            │  `deploy-failures`       │
                            │  (decouples ingest from  │
                            │   processing)            │
                            └──────────┬───────────────┘
                                       │ push subscription (ack deadline 300s)
                                       ▼
        ┌───────────────────────────────────────────────────┐
        │  Cloud Run: deployguard-agent  (scales to zero)   │
        │                                                    │
        │  [dedup guard] Firestore txn on message_id ───────┼──▶ drop duplicates
        │                                                    │
        │  ┌──────────────────────────────────────────┐     │
        │  │  ADK Root Agent (Gemini 3.5 Flash)       │     │
        │  │  orchestrates the triage workflow via    │     │
        │  │  scoped tools:                           │     │
        │  │                                          │     │
        │  │   1. get_pod_events(namespace, pod)      │─────┼──▶ K8s API (Connect Gateway)
        │  │   2. get_pod_logs(pod)                   │─────┼──▶ K8s API (Connect Gateway)
        │  │   3. get_deploy_diff(revision)           │─────┼──▶ Git / GKE rev
        │  │   4. query_incident_memory(signature)    │─────┼──▶ Firestore
        │  │   5. propose_fix(...) → structured plan  │     │
        │  │   6. request_approval(action) → Slack    │─────┼──▶ Slack API
        │  └──────────────────────────────────────────┘     │
        │            │ writes reasoning + trace              │
        └────────────┼───────────────────────────────────────┘
                     │                        ▲
          ┌──────────▼─────────┐    ┌─────────┴───────────┐
          │  Firestore         │    │  Cloud Trace /      │
          │  • incidents       │    │  OpenTelemetry      │
          │  • runbooks        │    │  (reasoning chain)  │
          │  • pending_actions │    └─────────────────────┘
          └──────────┬─────────┘
                     │ pending action (with action_id)
                     ▼
          ┌────────────────────────────────────────────┐
          │  Slack message w/ Approve / Reject buttons  │
          └──────────────┬──────────────────────────────┘
                         │ human clicks Approve
                         ▼
          ┌────────────────────────────────────────────┐
          │  Cloud Run: deployguard-approver (HTTP)    │
          │  • verifies Slack HMAC signature + timestamp│  ◀── AUTH (replay-safe)
          │  • verifies action_id not already applied  │  ◀── IDEMPOTENCY GUARD
          │    (Firestore transaction)                 │      "don't roll back twice"
          │  • state machine: pending→applying→applied │
          │  • executes apply_fix():                    │
          │      – kubectl rollout undo   (rollback)   │─────▶ K8s API (Connect Gateway)
          │      – patch env / resources  (config fix) │─────▶ K8s API (Connect Gateway)
          │  • writes resolution to incidents          │─────▶ Firestore
          └────────────────────────────────────────────┘
                         ▲
          ┌──────────────┴──────────────────────────────┐
          │  Cloud Scheduler → sweeper (every 5 min)    │  ◀── RECOVERS STUCK ACTIONS
          │  expires stale pending/applying actions,     │
          │  re-notifies Slack                           │
          └──────────────────────────────────────────────┘
```

**Key architectural decisions (these are the 30% score):**

| Decision | Why it matters to judges |
|---|---|
| Pub/Sub between ingest & agent | Decouples systems; agent can crash/restart without losing events; async background execution (the Taskmaster mandate) |
| Human-approval gate + idempotency guard | Production-grade safety; directly answers the webinar's "why a resumable agent orders two laptops" trap |
| Firestore incident memory | Persistent cross-session state; agent gets smarter over time, not a stateless script |
| Tools are individually scoped | "Are tools properly isolated and scoped for security?" — explicit judging question |
| Secret Manager + least-privilege SA | "How did you secure credentials?" — explicit judging question |
| OpenTelemetry reasoning traces | Observability; proves it's engineered, not a prompt hack |
| Cloud Run scales to zero | Cost control near $0 (hackathon tip) |

---

## 3. Component Breakdown

### 3.1 Event Ingest
- **Simulated (build first, demo-safe):** `gcloud pubsub topics publish deploy-failures --message='{...}'`. A JSON payload describing a failure. Zero infra, 100% reproducible demo.
- **Real (add later for wow):** A tiny in-cluster **watcher** (K8s informer using the Python client) watching for `CrashLoopBackOff` / failed rollout events, publishing to the same Pub/Sub topic. Same contract → the agent doesn't change.
- **CI path:** GitHub Actions `on: failure` → webhook → a thin Cloud Run HTTP ingest endpoint → publishes to Pub/Sub.

> The whole system talks a single **failure-event contract** (see §5), so all three sources are interchangeable. This decoupling is deliberate and worth calling out in the video.

### 3.2 The Agent (Cloud Run: `deployguard-agent`)
- Built with **Google ADK** (Python). Root agent using **Gemini 3.5 Flash**.
- Receives the Pub/Sub push, runs the **dedup guard**, hydrates a `FailureEvent`, runs the triage loop.
- Emits an **`Incident`** record and a **`PendingAction`** to Firestore, posts to Slack.
- **Does not apply changes** — separation of "decide" from "act."

### 3.3 The Approver (Cloud Run: `deployguard-approver`)
- Separate service (separation of concerns / blast-radius isolation).
- Handles Slack interactive callbacks (Approve/Reject).
- Runs **Slack signature verification** and the **idempotency guard** (Firestore transaction on `action_id`) before executing.
- Executes the remediation via the explicit **state machine** and records the resolution.

### 3.4 The Sweeper (Cloud Scheduler + Cloud Run)
- Runs every 5 min; finds `pending_actions` stuck in `pending`/`applying` past a TTL.
- Marks them `expired`, re-notifies Slack. This is the "recover if a worker loops" safety net.

### 3.5 Memory (Firestore) — see §4

### 3.6 Observability
- ADK's built-in tracing → **Cloud Trace** via OpenTelemetry.
- Every tool call, model turn, and decision is a span → this is your "end-to-end reasoning chain."

---

## 4. Data Model (Firestore)

```
incidents/{message_id}           # keyed by Pub/Sub message_id = dedup key
  ├─ created_at:        timestamp
  ├─ source:            "gke" | "github-actions" | "simulated"
  ├─ signature:         string   # normalized fingerprint of the failure
  ├─ signal:            map      # raw: pod, namespace, events, log excerpt
  ├─ diagnosis:         map      # { root_cause, confidence, evidence[] }
  ├─ proposed_fix:      map      # { type, target, patch, rationale }
  ├─ status:            "diagnosed" | "awaiting_approval" | "applied" | "rejected" | "failed"
  ├─ matched_runbook:   ref?     # runbooks/{id} if a known pattern matched
  └─ resolution:        map?     # { applied_at, applied_by, outcome }

runbooks/{runbook_id}           # known-pattern library (grows over time)
  ├─ signature_pattern: string
  ├─ failure_class:     "bad_image_tag" | "missing_env_var" | "oom_killed"
  ├─ recommended_fix:   map
  └─ hit_count:         number

pending_actions/{action_id}      # idempotency + approval state machine
  ├─ incident_id:       ref
  ├─ action:            map      # the exact fix to apply
  ├─ state:             "pending" | "applying" | "applied" | "rejected" | "expired" | "failed"
  ├─ created_at:        timestamp
  ├─ ttl:               timestamp # sweeper expires past this
  ├─ applied_at:        timestamp?
  └─ error:             string?
```

> `action_id` is the idempotency key AND a single-use capability token. The
> approver runs a Firestore transaction: read → if `state != "pending"` abort →
> set `applying` → execute → set `applied`. A double-click, a Slack retry, or a
> replayed request is safe. **Demo this on camera — almost no solo entry will.**

---

## 5. The Failure-Event Contract

Every source produces this shape; the agent only knows this shape:

```json
{
  "source": "gke",
  "namespace": "prod",
  "workload": "checkout-api",
  "pod": "checkout-api-7d9f-xk2",
  "reason": "CrashLoopBackOff",
  "last_deploy": {
    "revision": "checkout-api-00042",
    "image": "gcr.io/acme/checkout:v2.3.1",
    "commit": "a1b2c3d"
  },
  "observed_at": "2026-08-20T02:14:00Z"
}
```

---

## 6. Scope: Failure Classes (do 3 deeply, not 10 shallowly)

| Class | Signal the agent keys on | Proposed fix |
|---|---|---|
| **Bad image tag / pull error** | `ImagePullBackOff`, `ErrImagePull` | `kubectl rollout undo` to last good revision |
| **Missing / wrong env var** | CrashLoop + log "KeyError/env not set" | Patch Deployment env from last good revision |
| **OOMKilled** | `OOMKilled` in pod events | Bump memory limit (bounded, e.g. +50%) |

Anything outside these three → agent posts a diagnosis and **routes to a human**
(no auto-fix). Being explicit about the boundary is engineering maturity, not a
gap — say so in the write-up.

---

## 7. Resilience & Failure Handling

> This is the section that wins the 30% "failure-tolerant architecture" score.
> Judges explicitly ask: *"how does the system recover if a worker loops, times
> out, or returns a hallucination?"* Here is the answer for each real failure mode.

### 7.1 Failure-modes → mitigation table

| # | Failure mode | Mitigation |
|---|---|---|
| 1 | **Pub/Sub push timeout → endless retries** | Dedup on `message_id` (Firestore txn) so redelivery is a no-op; ack deadline 300s; Cloud Run request timeout ≥ ack deadline |
| 2 | **Cloud Run → GKE access fails silently** | **GKE Connect Gateway** (IAM + Workload Identity, no VPC/kubeconfig); every K8s call wrapped → sets `status="failed"`, logs a trace span, posts to Slack. No swallowed exceptions |
| 3 | **Unauthenticated Slack endpoint** | Slack **HMAC-SHA256 signature** verification (secret in Secret Manager); reject timestamps >5 min old (replay protection); `action_id` is a single-use capability token |
| 4 | **Half-state remediation (stuck pending/broken)** | Explicit state machine (`pending→applying→applied\|failed\|expired`) via Firestore txns; idempotent fixes (`rollout undo` / declarative patch); on mid-flight failure set `failed` + alert (never false success); **sweeper** expires stale actions and re-notifies |
| 5 | **LLM hallucinated diagnosis / fix** | Structured output validated against an allowlist of 3 failure classes; a fix outside the allowlist is rejected → routed to human; every fix requires human approval before execution |
| 6 | **Agent instance crashes mid-triage** | Pub/Sub redelivers (not acked until incident written); dedup guard prevents double-processing on the retry |

### 7.2 The at-most-once-effect guarantee

Pub/Sub gives *at-least-once delivery*. We convert that into *at-most-once
effect*:

1. `message_id` dedup on ingest → the same failure is never triaged twice.
2. `action_id` idempotency on remediation → the same fix is never applied twice.

Between these two keys, every consequential action is exactly-once even though
the transport is at-least-once. This is the "don't order two laptops" property.

### 7.3 Auth model summary

- **Ingest → agent:** Pub/Sub push with an authenticated OIDC token; Cloud Run requires auth.
- **Slack → approver:** public endpoint, but every request HMAC-verified + replay-protected.
- **Agent/approver → cluster:** Workload Identity via Connect Gateway; agent SA is read-only, only the approver SA may mutate.

---

## 8. Security Posture

- **Secret Manager** for Slack signing secret, Slack bot token, GitHub token, cluster credentials.
- **Least-privilege service accounts** — agent SA can read K8s + Firestore; only the approver SA can mutate the cluster.
- **Workload Identity** for GKE access (no static kubeconfig in the container).
- **Signed Slack requests** verified on the approver endpoint (§7.3).
- **Tool scoping** — the read tools and the write tool live in different services.

---

## 9. Repository Structure

```
deployguard/
├─ README.md                  # spin-up instructions (judged)
├─ docs/
│  ├─ ARCHITECTURE.md         # this file
│  ├─ PLAN.md                 # 16-day plan
│  └─ architecture.png        # the diagram for submission
├─ agent/
│  ├─ main.py                 # Cloud Run entrypoint (Pub/Sub push handler + dedup)
│  ├─ deployguard_agent.py    # ADK agent definition
│  ├─ tools/
│  │  ├─ k8s.py               # get_pod_events, get_pod_logs (read-only)
│  │  ├─ git.py               # get_deploy_diff
│  │  ├─ memory.py            # Firestore query/write
│  │  └─ slack.py             # request_approval
│  ├─ models.py               # FailureEvent, Incident, PendingAction (pydantic)
│  └─ telemetry.py            # OpenTelemetry setup
├─ approver/
│  ├─ main.py                 # Slack callback: verify sig → idempotency → apply
│  └─ apply.py                # apply_fix (rollout undo / patch) — write path
├─ sweeper/
│  └─ main.py                 # expires stale pending_actions
├─ watcher/                   # (Phase 3) in-cluster K8s event watcher
│  └─ watch.py
├─ deploy/
│  ├─ Dockerfile.agent
│  ├─ Dockerfile.approver
│  ├─ cloudrun-agent.yaml
│  └─ setup.sh                # one-shot: topic, sub, firestore, scheduler, deploy
└─ scripts/
   └─ simulate_failure.sh     # gcloud pubsub publish (demo trigger)
```

---

## 10. Tech Requirement Checklist (mandatory to pass Stage 1)

- [x] **Gemini 3.5 Flash** via Vertex AI / Gemini API — the agent's reasoning
- [x] **Google ADK** — the agent framework
- [x] **Google Cloud services** — Cloud Run + Pub/Sub + Firestore + Secret Manager + Cloud Scheduler
- [ ] (Bonus) **Gemma** for the offline signature-classification step → +0.2

---

## 11. Cost Control (keep near $0)

- Cloud Run min instances = 0 (scale to zero when idle).
- Gemini **Flash** only (no Pro).
- Firestore free tier is plenty for demo volume.
- Set a **$20 budget alert** in Cloud Console on day 1.
- **Turn everything off after recording the demo** — the video is your proof, not a live service.
