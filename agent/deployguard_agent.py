"""The DeployGuard ADK triage agent.

Root agent (Gemini 3.5 Flash) that orchestrates read-only diagnostic tools,
consults incident memory, produces a structured diagnosis + proposed fix, and
requests human approval via Slack. It NEVER mutates the cluster — that is the
approver service's job (separation of decide vs. act, ARCHITECTURE.md §3).

Verified against google-adk==2.7.0.
"""
from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from models import FailureEvent
from tools import git, k8s, memory, slack

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

INSTRUCTION = """
You are DeployGuard, an autonomous on-call SRE. A Kubernetes deployment has
failed. Diagnose the root cause and propose a fix — you do NOT apply it.

Follow this procedure:
1. Call get_pod_events and get_pod_logs to gather evidence.
2. Call get_deploy_diff to see what changed in the last deployment.
3. Call query_incident_memory with a short failure signature to check whether
   we have seen this pattern before.
4. Classify the failure into EXACTLY ONE of:
   - bad_image_tag   (ImagePullBackOff / ErrImagePull)   -> fix: rollback
   - missing_env_var (crash + missing/empty env in logs) -> fix: patch_env
   - oom_killed      (OOMKilled in events)               -> fix: bump_memory
   - unknown         (anything else)                     -> fix: none (human)
5. If the class is not one of the three known classes, set fix to 'none' and
   explain what a human should check. Do not invent a fix.
6. Call request_approval with your diagnosis and proposed fix. This posts to
   Slack with Approve/Reject buttons. Your job ends there.

Be concise and evidence-driven. Never claim certainty you don't have; report a
confidence score.
"""


def build_agent() -> Agent:
    return Agent(
        name="deployguard",
        model=MODEL,
        instruction=INSTRUCTION,
        tools=[
            k8s.get_pod_events,
            k8s.get_pod_logs,
            git.get_deploy_diff,
            memory.query_incident_memory,
            slack.request_approval,
        ],
    )


APP_NAME = "deployguard"
USER_ID = "pubsub"  # single synthetic user; sessions are keyed by incident


def triage(message_id: str, event: FailureEvent) -> None:
    """Run one triage turn for a failure event."""
    agent = build_agent()
    session_service = InMemorySessionService()
    # Runner.run() does not auto-create sessions — create one explicitly,
    # keyed by the (already deduped) Pub/Sub message_id.
    session_service.create_session_sync(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=message_id,
    )
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    prompt = (
        f"A deployment failed. Incident id: {message_id}.\n"
        f"Event:\n{event.model_dump_json(indent=2)}\n\n"
        "Diagnose and request approval for a fix."
    )
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    # Runner.run() is a sync generator over ADK Events; tool calls (memory
    # writes, Slack post) happen as a side effect of driving it to completion.
    #
    # IMPORTANT: ADK does not raise on model/tool failure — it reports it via
    # error_code/error_message on the final Event. main.py relies on triage()
    # raising to mark the incident failed and trigger a Pub/Sub retry, so we
    # translate that condition into a raised exception here (never swallow,
    # ARCHITECTURE.md §7.1 #2).
    for adk_event in runner.run(
        user_id=USER_ID,
        session_id=message_id,
        new_message=message,
    ):
        if adk_event.is_final_response():
            if adk_event.error_code:
                raise RuntimeError(
                    f"agent run failed: {adk_event.error_code} "
                    f"{adk_event.error_message}"
                )
            print(f"[triage-done] {message_id}")
