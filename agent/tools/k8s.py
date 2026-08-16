"""Read-only Kubernetes diagnostic tools.

Cluster access uses GKE Connect Gateway (IAM + Workload Identity) so the managed
Cloud Run service can reach the cluster with no VPC connector or static
kubeconfig (ARCHITECTURE.md §7.1 #2).

Set MOCK_K8S=1 to return canned data for local dev / reproducible demos.
"""
from __future__ import annotations

import os

_MOCK = os.environ.get("MOCK_K8S") == "1"


def _client():
    """Build a Kubernetes API client via Connect Gateway credentials."""
    from kubernetes import client, config

    # In-cluster or Connect Gateway kubeconfig written by deploy/setup.sh.
    config.load_kube_config()  # TODO: swap for Connect Gateway loader in prod
    return client.CoreV1Api(), client.AppsV1Api()


def get_pod_events(namespace: str, pod: str) -> str:
    """Return recent Kubernetes events for a pod (reason + message).

    Args:
        namespace: the pod's namespace.
        pod: the pod name.
    """
    if _MOCK:
        return (
            "Warning  BackOff  Back-off restarting failed container\n"
            "Warning  Failed   Error: ImagePullBackOff\n"
            "Warning  Failed   Failed to pull image "
            "'gcr.io/acme/checkout:v2.3.1': not found"
        )
    core, _ = _client()
    field = f"involvedObject.name={pod}"
    events = core.list_namespaced_event(namespace, field_selector=field)
    return "\n".join(
        f"{e.type}  {e.reason}  {e.message}" for e in events.items
    ) or "no events found"


def get_pod_logs(namespace: str, pod: str, tail: int = 100) -> str:
    """Return the last N lines of a pod's container logs.

    Args:
        namespace: the pod's namespace.
        pod: the pod name.
        tail: number of trailing log lines to fetch.
    """
    if _MOCK:
        return "Error: could not pull image; manifest unknown for tag v2.3.1"
    core, _ = _client()
    try:
        return core.read_namespaced_pod_log(
            pod, namespace, tail_lines=tail
        )
    except Exception as exc:  # noqa: BLE001 - surface, never swallow
        return f"[log-fetch-error] {exc}"
