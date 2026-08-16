"""Deployment-diff tool: what changed in the last deploy.

For the demo this reads the last-good vs. current revision from the Deployment's
rollout history; a fuller version could diff the git commits between them.

Set MOCK_GIT=1 for canned data.
"""
from __future__ import annotations

import os

_MOCK = os.environ.get("MOCK_GIT") == "1"


def get_deploy_diff(namespace: str, workload: str) -> str:
    """Summarize the difference between the current and last-good revision.

    Args:
        namespace: the workload's namespace.
        workload: the Deployment name.
    """
    if _MOCK:
        return (
            "revision 42 (current) changed image tag "
            "v2.3.0 -> v2.3.1 (commit a1b2c3d); no env or resource changes"
        )
    from kubernetes import client, config

    config.load_kube_config()  # TODO: Connect Gateway loader
    apps = client.AppsV1Api()
    dep = apps.read_namespaced_deployment(workload, namespace)
    containers = dep.spec.template.spec.containers
    images = ", ".join(c.image for c in containers)
    return f"current revision images: {images}"
