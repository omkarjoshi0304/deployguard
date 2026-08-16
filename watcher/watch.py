"""In-cluster Kubernetes event watcher (Phase 3).

Runs as a Deployment inside GKE, watches for CrashLoopBackOff / failed rollouts,
and publishes a FailureEvent (the §5 contract) to Pub/Sub. This is the "real"
event source; the simulated source (scripts/simulate_failure.sh) produces the
same contract, so the agent is unchanged.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from google.cloud import pubsub_v1
from kubernetes import client, config, watch

PROJECT_ID = os.environ["PROJECT_ID"]
TOPIC = os.environ.get("PUBSUB_TOPIC", "deploy-failures")

_INTERESTING = {"BackOff", "Failed", "FailedCreate"}


def _publish(event: dict) -> None:
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(PROJECT_ID, TOPIC)
    publisher.publish(topic_path, json.dumps(event).encode()).result()


def main() -> None:
    config.load_incluster_config()
    core = client.CoreV1Api()
    w = watch.Watch()
    print("[watcher] watching cluster events...")
    for ev in w.stream(core.list_event_for_all_namespaces):
        obj = ev["object"]
        if obj.reason not in _INTERESTING:
            continue
        involved = obj.involved_object
        failure = {
            "source": "gke",
            "namespace": involved.namespace or "default",
            "workload": involved.name or "unknown",
            "pod": involved.name or "unknown",
            "reason": obj.reason,
            "last_deploy": {"revision": "unknown", "image": "unknown"},
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        print(f"[watcher] publishing failure: {obj.reason} {involved.name}")
        _publish(failure)


if __name__ == "__main__":
    main()
