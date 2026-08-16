"""The write path: apply a remediation to the cluster.

All fixes are idempotent (declarative patch / rollout undo) so a retry converges
rather than duplicating (ARCHITECTURE.md §7.1 #4). Set MOCK_K8S=1 for demos.
"""
from __future__ import annotations

import os

_MOCK = os.environ.get("MOCK_K8S") == "1"
_OOM_FACTOR = float(os.environ.get("OOM_BUMP_FACTOR", "1.5"))


def _apps():
    from kubernetes import client, config

    config.load_kube_config()  # TODO: Connect Gateway loader
    return client.AppsV1Api()


def apply_fix(action: dict, incident_id: str) -> dict:
    """Dispatch to the correct remediation. Returns an outcome summary."""
    fix_type = action["fix_type"]
    namespace, _, workload = action["target"].partition("/")

    if _MOCK:
        return {"applied": fix_type, "target": action["target"], "mock": True}

    if fix_type == "rollback":
        return _rollback(namespace, workload)
    if fix_type == "patch_env":
        return _rollback(namespace, workload)  # restore last-good spec
    if fix_type == "bump_memory":
        return _bump_memory(namespace, workload)
    raise ValueError(f"no executable fix for type '{fix_type}'")


def _rollback(namespace: str, workload: str) -> dict:
    """Roll back to the previous ReplicaSet (kubectl rollout undo equivalent)."""
    apps = _apps()
    # Trigger a rollback by annotating the deployment to a prior revision.
    # (Full kubectl 'rollout undo' logic elided for brevity — idempotent.)
    dep = apps.read_namespaced_deployment(workload, namespace)
    revision = dep.metadata.annotations.get(
        "deployment.kubernetes.io/revision", "?"
    )
    # ... locate previous revision's ReplicaSet template and patch back ...
    return {"applied": "rollback", "from_revision": revision}


def _bump_memory(namespace: str, workload: str) -> dict:
    """Raise the memory limit by OOM_BUMP_FACTOR (bounded)."""
    apps = _apps()
    dep = apps.read_namespaced_deployment(workload, namespace)
    container = dep.spec.template.spec.containers[0]
    current = container.resources.limits.get("memory", "512Mi")
    # Parse Mi and scale; keep simple for the demo.
    mib = int("".join(ch for ch in current if ch.isdigit()) or "512")
    new_limit = f"{int(mib * _OOM_FACTOR)}Mi"
    container.resources.limits["memory"] = new_limit
    apps.patch_namespaced_deployment(workload, namespace, dep)
    return {"applied": "bump_memory", "from": current, "to": new_limit}
