"""Shared data models for DeployGuard.

These mirror the Firestore data model in docs/ARCHITECTURE.md §4.
Pydantic gives us validation at the boundary — an LLM-proposed fix that doesn't
match one of our known classes is rejected before it ever reaches the cluster.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class FailureClass(str, Enum):
    """The failure classes DeployGuard can auto-remediate. Anything else is
    diagnosed and routed to a human (no auto-fix)."""

    BAD_IMAGE_TAG = "bad_image_tag"
    MISSING_ENV_VAR = "missing_env_var"
    OOM_KILLED = "oom_killed"
    UNKNOWN = "unknown"  # -> route to human


class FixType(str, Enum):
    ROLLBACK = "rollback"          # kubectl rollout undo
    PATCH_ENV = "patch_env"        # restore env from last good revision
    BUMP_MEMORY = "bump_memory"    # raise memory limit
    NONE = "none"                  # human needed


class IncidentStatus(str, Enum):
    DIAGNOSED = "diagnosed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"


class ActionState(str, Enum):
    PENDING = "pending"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class LastDeploy(BaseModel):
    revision: str
    image: str
    commit: Optional[str] = None


class FailureEvent(BaseModel):
    """The single contract every event source produces (ARCHITECTURE.md §5)."""

    source: str  # "gke" | "github-actions" | "simulated"
    namespace: str
    workload: str
    pod: str
    reason: str
    last_deploy: LastDeploy
    observed_at: datetime


class Diagnosis(BaseModel):
    failure_class: FailureClass
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class ProposedFix(BaseModel):
    fix_type: FixType
    target: str            # namespace/workload
    patch: dict[str, Any] = Field(default_factory=dict)
    rationale: str


class Incident(BaseModel):
    message_id: str        # Pub/Sub message_id — the dedup key & doc id
    created_at: datetime
    source: str
    signature: str
    signal: dict[str, Any]
    diagnosis: Optional[Diagnosis] = None
    proposed_fix: Optional[ProposedFix] = None
    status: IncidentStatus = IncidentStatus.DIAGNOSED
    matched_runbook: Optional[str] = None
    resolution: Optional[dict[str, Any]] = None


class PendingAction(BaseModel):
    action_id: str         # idempotency key + single-use capability token
    incident_id: str
    action: ProposedFix
    state: ActionState = ActionState.PENDING
    created_at: datetime
    ttl: datetime
    applied_at: Optional[datetime] = None
    error: Optional[str] = None
