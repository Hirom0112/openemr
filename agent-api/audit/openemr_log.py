"""HIPAA-style audit emission for agent tool calls.

Why this exists
---------------
Every tool call the agent makes against patient data needs an audit trail
(who, what, when, outcome, duration).  OpenEMR has a MySQL ``log`` table
designed for this; the agent-api Python service is a separate container
and intentionally does not import OpenEMR PHP code, so we cannot write
that table directly today.

Implementation choice (option "c" from the task brief): emit a structured
log line on a dedicated ``agent.audit`` logger and bump a Prometheus
counter labelled by outcome.  Downstream observability (Loki, Promtail,
Datadog, etc.) can scrape the ``audit.tool_call`` events into an audit
sink with no changes to the call sites.  Swapping the implementation to
direct MySQL or an HTTP POST to OpenEMR later is a one-file change —
:func:`emit_audit_event` is the only public surface.

Failure handling
----------------
Audit emission must NEVER cause a tool call to fail.  All work in
:func:`emit_audit_event` is wrapped and logged at WARNING on failure —
the dispatcher continues regardless.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from prometheus_client import Counter

# Dedicated logger so audit events can be filtered separately from the
# operational ``agent.tool`` log channel.
_audit_logger = logging.getLogger("agent.audit")

# Operational logger — ONLY for audit-emission failures; callers should
# never see anything from ``agent.audit`` itself unless the audit event
# was the thing being emitted.
_logger = logging.getLogger(__name__)

AuditOutcome = Literal["ok", "error", "blocked"]

agent_audit_events_total = Counter(
    "agent_audit_events_total",
    "Total audit events emitted by the dispatcher, by outcome.",
    ["outcome"],
)


def emit_audit_event(
    *,
    session_id: str,
    provider_id: str | None,
    tool_name: str,
    outcome: AuditOutcome,
    duration_ms: int,
    patient_id: str | None = None,
    failure_class: str | None = None,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one audit event.  Safe to call from any tool-call path.

    Never raises — any internal failure is logged at WARNING on the
    operational logger and the audit metric is incremented under the
    ``error`` outcome so observability can detect a broken audit pipeline.
    """
    try:
        payload: dict[str, Any] = {
            "event": "audit.tool_call",
            "session_id": session_id,
            "provider_id": provider_id,
            "tool": tool_name,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        if patient_id is not None:
            payload["patient_id"] = patient_id
        if failure_class is not None:
            payload["failure_class"] = failure_class
        if request_id is not None:
            payload["request_id"] = request_id
        if extra:
            payload["extra"] = extra

        _audit_logger.info("audit.tool_call", extra=payload)
        agent_audit_events_total.labels(outcome=outcome).inc()
    except Exception as exc:  # pragma: no cover — defensive
        try:
            agent_audit_events_total.labels(outcome="error").inc()
        except Exception:
            pass
        _logger.warning(
            "audit_emit_failed",
            extra={
                "session_id": session_id,
                "tool": tool_name,
                "error": str(exc),
            },
        )
