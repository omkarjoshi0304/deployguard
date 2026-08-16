"""OpenTelemetry -> Cloud Trace setup.

Every tool call and model turn becomes a span, giving judges the
"end-to-end reasoning chain" they ask for (ARCHITECTURE.md §3.6).
"""
from __future__ import annotations

import os

_initialized = False


def init_tracing() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    if os.environ.get("DISABLE_TRACING") == "1":
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(CloudTraceSpanExporter())
        )
        trace.set_tracer_provider(provider)
        print("[telemetry] Cloud Trace exporter initialized")
    except Exception as exc:  # noqa: BLE001 - tracing must never break triage
        print(f"[telemetry] disabled ({exc})")
