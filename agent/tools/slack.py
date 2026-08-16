"""Slack tool: post a diagnosis with Approve/Reject buttons.

Creates a single-use pending_action (the action_id is both an idempotency key
and a capability token) and posts an interactive message. The approver service
handles the button callback (ARCHITECTURE.md §3.3, §7).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

_SLACK = None


def _client():
    global _SLACK
    if _SLACK is None:
        from slack_sdk import WebClient

        _SLACK = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    return _SLACK


def request_approval(
    incident_id: str,
    failure_class: str,
    root_cause: str,
    confidence: float,
    fix_type: str,
    target: str,
    rationale: str,
) -> str:
    """Post the diagnosis to Slack and request human approval for the fix.

    Args:
        incident_id: the incident (Pub/Sub message) id.
        failure_class: one of bad_image_tag | missing_env_var | oom_killed | unknown.
        root_cause: one-line root cause.
        confidence: 0.0-1.0.
        fix_type: rollback | patch_env | bump_memory | none.
        target: namespace/workload the fix applies to.
        rationale: why this fix.
    """
    db = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))
    ttl_min = int(os.environ.get("APPROVAL_TTL_MINUTES", "30"))
    now = datetime.now(timezone.utc)
    action_id = str(uuid.uuid4())

    proposed_fix = {
        "fix_type": fix_type,
        "target": target,
        "rationale": rationale,
    }

    db.collection("pending_actions").document(action_id).set({
        "action_id": action_id,
        "incident_id": incident_id,
        "action": proposed_fix,
        "state": "pending",
        "created_at": now,
        "ttl": now + timedelta(minutes=ttl_min),
    })
    db.collection("incidents").document(incident_id).set(
        {
            "diagnosis": {
                "failure_class": failure_class,
                "root_cause": root_cause,
                "confidence": confidence,
            },
            "proposed_fix": proposed_fix,
            "status": "awaiting_approval",
            "updated_at": now,
        },
        merge=True,
    )

    auto = fix_type != "none"
    text = (
        f":rotating_light: *DeployGuard* — `{target}`\n"
        f"*Class:* {failure_class}  (confidence {confidence:.0%})\n"
        f"*Root cause:* {root_cause}\n"
        f"*Proposed fix:* `{fix_type}` — {rationale}"
    )

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if auto:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "value": action_id,
                    "action_id": "approve",
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "value": action_id,
                    "action_id": "reject",
                },
            ],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": "_No safe auto-fix — human investigation needed._"}],
        })

    _client().chat_postMessage(
        channel=os.environ.get("SLACK_CHANNEL", "#deployguard"),
        text="DeployGuard incident",
        blocks=blocks,
    )
    return f"posted approval request action_id={action_id}"
