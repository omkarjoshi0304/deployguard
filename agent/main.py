"""Cloud Run entrypoint for the DeployGuard agent.

Receives Pub/Sub push messages, runs the DEDUP GUARD (idempotency on the
Pub/Sub message_id), then hands the failure to the ADK triage agent.

Pub/Sub gives at-least-once delivery; the dedup guard converts that into
at-most-once *effect* — a redelivered message is a no-op (ARCHITECTURE.md §7.2).
"""
from __future__ import annotations

import base64
import binascii
import json
import os

import functions_framework
from flask import Request

from deployguard_agent import triage
from models import FailureEvent
from telemetry import init_tracing
from tools import memory

init_tracing()


def _decode_push(request: Request) -> tuple[str, dict]:
    """Return (message_id, payload) from a Pub/Sub push envelope."""
    envelope = request.get_json(silent=True)
    if not envelope or "message" not in envelope:
        raise ValueError("not a valid Pub/Sub push envelope")

    msg = envelope["message"]
    message_id = msg.get("messageId") or msg.get("message_id")
    if not message_id:
        raise ValueError("missing messageId")

    raw = base64.b64decode(msg["data"]) if msg.get("data") else b"{}"
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError(f"bad payload: {exc}") from exc
    return message_id, payload


@functions_framework.http
def handle_pubsub(request: Request):
    """HTTP handler bound to the Pub/Sub push subscription."""
    try:
        message_id, payload = _decode_push(request)
    except ValueError as exc:
        # Malformed -> ack (return 2xx) so Pub/Sub stops retrying a bad message.
        print(f"[drop] {exc}")
        return ("bad request", 204)

    # --- DEDUP GUARD -------------------------------------------------------
    # Atomically claim this message_id. If it already exists, this is a
    # redelivery -> drop it (return 200 so Pub/Sub acks).
    if not memory.claim_incident(message_id):
        print(f"[dedup] message {message_id} already processed")
        return ("duplicate", 200)

    try:
        event = FailureEvent(**payload)
    except Exception as exc:  # noqa: BLE001 - validation failure is terminal
        print(f"[invalid-event] {exc}")
        memory.mark_incident_failed(message_id, f"invalid event: {exc}")
        return ("invalid event", 200)  # ack; retrying won't help

    # --- TRIAGE ------------------------------------------------------------
    try:
        triage(message_id, event)
    except Exception as exc:  # noqa: BLE001 - surface, never swallow (§7.1 #2)
        print(f"[triage-error] {message_id}: {exc}")
        memory.mark_incident_failed(message_id, str(exc))
        # Return 500 so Pub/Sub redelivers; dedup guard makes the retry safe
        # ONLY after we clear the claim, so clear it here:
        memory.release_incident_claim(message_id)
        return ("triage failed", 500)

    return ("ok", 200)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # For local dev only; production uses gunicorn/functions-framework.
    from functions_framework import create_app

    app = create_app(target="handle_pubsub")
    app.run(host="0.0.0.0", port=port)
