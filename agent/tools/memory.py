"""Firestore-backed incident memory + the dedup / idempotency primitives.

Collections (ARCHITECTURE.md §4):
  incidents/{message_id}       - one per failure; message_id is the dedup key
  runbooks/{runbook_id}        - known-pattern library
  pending_actions/{action_id}  - approval + idempotency state machine
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from google.cloud import firestore

_DB = None


def _db() -> "firestore.Client":
    global _DB
    if _DB is None:
        _DB = firestore.Client(
            database=os.environ.get("FIRESTORE_DATABASE", "(default)")
        )
    return _DB


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Dedup guard (called from main.py) ------------------------------------
def claim_incident(message_id: str) -> bool:
    """Atomically create the incident doc. Returns False if it already exists
    (i.e. this is a Pub/Sub redelivery and should be dropped)."""
    ref = _db().collection("incidents").document(message_id)

    @firestore.transactional
    def _txn(txn):
        snap = ref.get(transaction=txn)
        if snap.exists:
            return False
        txn.set(ref, {
            "message_id": message_id,
            "created_at": _now(),
            "status": "processing",
        })
        return True

    return _txn(_db().transaction())


def release_incident_claim(message_id: str) -> None:
    """Delete a claim so a Pub/Sub redelivery can retry after a crash."""
    _db().collection("incidents").document(message_id).delete()


def mark_incident_failed(message_id: str, error: str) -> None:
    _db().collection("incidents").document(message_id).set(
        {"status": "failed", "error": error, "updated_at": _now()},
        merge=True,
    )


def save_diagnosis(message_id: str, diagnosis: dict, proposed_fix: dict) -> None:
    _db().collection("incidents").document(message_id).set(
        {
            "diagnosis": diagnosis,
            "proposed_fix": proposed_fix,
            "status": "awaiting_approval",
            "updated_at": _now(),
        },
        merge=True,
    )


# --- Agent tool ------------------------------------------------------------
def query_incident_memory(signature: str) -> str:
    """Look up whether we've seen a similar failure before.

    Args:
        signature: a short fingerprint of the failure (e.g. reason + image).

    Returns a short summary of the best-matching past incident/runbook, or a
    note that this is novel.
    """
    hits = (
        _db()
        .collection("runbooks")
        .where("signature_pattern", "==", signature)
        .limit(1)
        .stream()
    )
    for doc in hits:
        d = doc.to_dict()
        return (
            f"KNOWN pattern (seen {d.get('hit_count', 1)}x): "
            f"class={d.get('failure_class')} "
            f"recommended_fix={d.get('recommended_fix')}"
        )
    return "novel failure — no matching runbook"
