"""Sweeper (Cloud Run, triggered by Cloud Scheduler every 5 min).

Recovers actions stuck in 'pending' or 'applying' past their TTL — the
"recover if a worker loops / crashes mid-flight" safety net (ARCHITECTURE.md §7.1 #4).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask
from google.cloud import firestore

app = Flask(__name__)


def _db():
    return firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))


@app.post("/sweep")
@app.get("/sweep")
def sweep():
    now = datetime.now(timezone.utc)
    db = _db()
    stuck = (
        db.collection("pending_actions")
        .where("state", "in", ["pending", "applying"])
        .where("ttl", "<", now)
        .stream()
    )
    expired = 0
    for doc in stuck:
        doc.reference.update({"state": "expired", "expired_at": now})
        data = doc.to_dict()
        db.collection("incidents").document(data["incident_id"]).set(
            {"status": "failed", "error": "approval expired"}, merge=True
        )
        expired += 1
        # TODO: re-notify Slack that the action expired.
    return ({"expired": expired}, 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
