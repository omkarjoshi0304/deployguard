"""DeployGuard agent tools. Read-only diagnostics + memory + Slack.

The write path (apply_fix) deliberately lives in the approver service, not
here — read tools and the write tool are isolated by process (ARCHITECTURE.md §7.3).
"""
