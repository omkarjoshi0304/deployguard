"""Approver service (Cloud Run).

Public endpoint that Slack calls when a human clicks Approve/Reject. It is the
ONLY service allowed to mutate the cluster. Three guards run before any change:

  1. Slack HMAC signature + timestamp verification (replay-safe) — §7.3
  2. Idempotency guard: Firestore transaction on action_id — §7.2
  3. Explicit state machine: pending -> applying -> applied|failed — §7.1 #4
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

from flask import Flask, request

from apply import apply_fix
from google.cloud import firestore

app = Flask(__name__)


def _db():
    return firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))


def _verify_slack(req) -> bool:
    """Verify the Slack signing secret HMAC and reject stale requests."""
    secret = os.environ["SLACK_SIGNING_SECRET"].encode()
    ts = req.headers.get("X-Slack-Request-Timestamp", "0")
    sig = req.headers.get("X-Slack-Signature", "")

    # Replay protection: reject anything older than 5 minutes.
    if abs(time.time() - int(ts)) > 300:
        return False

    basestring = f"v0:{ts}:{req.get_data(as_text=True)}".encode()
    expected = "v0=" + hmac.new(secret, basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _claim_action(action_id: str, approve: bool) -> dict | None:
    """Idempotency guard + state transition. Returns the action doc to apply,
    or None if it was already handled / not pending / not found."""
    ref = _db().collection("pending_actions").document(action_id)

    @firestore.transactional
    def _txn(txn):
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None
        data = snap.to_dict()
        if data.get("state") != "pending":
            return None  # already applied/rejected/expired -> no-op
        new_state = "applying" if approve else "rejected"
        txn.update(ref, {"state": new_state, "updated_at": datetime.now(timezone.utc)})
        return data if approve else None

    return _txn(_db().transaction())


@app.post("/slack/actions")
def slack_actions():
    if not _verify_slack(request):
        return ("unauthorized", 401)

    payload = json.loads(request.form["payload"])
    action = payload["actions"][0]
    action_id = action["value"]
    approve = action["action_id"] == "approve"

    claimed = _claim_action(action_id, approve)
    if claimed is None:
        # Rejected, or already handled (double-click / replay) -> safe no-op.
        return ("", 200)

    ref = _db().collection("pending_actions").document(action_id)
    try:
        outcome = apply_fix(claimed["action"], claimed["incident_id"])
        ref.update({"state": "applied", "applied_at": datetime.now(timezone.utc)})
        _db().collection("incidents").document(claimed["incident_id"]).set(
            {"status": "applied", "resolution": outcome}, merge=True
        )
    except Exception as exc:  # noqa: BLE001 - never report false success (§7.1 #4)
        ref.update({"state": "failed", "error": str(exc)})
        _db().collection("incidents").document(claimed["incident_id"]).set(
            {"status": "failed", "error": str(exc)}, merge=True
        )
    return ("", 200)


@app.get("/healthz")
def healthz():
    return ("ok", 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
