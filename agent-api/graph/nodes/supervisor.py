"""Slice 3.2 — Supervisor routing node.

Pure async function. Decides the next graph node based on the W2State.
Never raises; audit emit failures are swallowed and logged.

Routing rules (W2_ARCHITECTURE.md §5.2):
  * file_bytes_ref present                          -> extractor
  * message present AND extraction already populated -> retriever
  * message present                                  -> structured
  * otherwise                                        -> finalize
"""
from __future__ import annotations

import logging
import time
from typing import Any

from audit.models import AuditEvent
from audit import writer as audit_writer

from ..state import W2State

logger = logging.getLogger(__name__)


def _decide(state: W2State) -> tuple[str, str]:
    """Return (next_node, decision_reason)."""
    file_ref = state.get("file_bytes_ref")
    message = (state.get("message") or "").strip()
    extraction = state.get("extraction")

    if file_ref:
        return "extractor", "file_bytes_ref present"
    if message and extraction:
        return "retriever", "message with prior extraction"
    if message:
        return "structured", "message without extraction"
    return "finalize", "no inputs"


async def supervisor(state: W2State) -> dict[str, Any]:
    """Route to the next node and emit one audit row."""
    t0 = time.monotonic()
    next_node, reason = _decide(state)
    duration_ms = int((time.monotonic() - t0) * 1000)

    logger.info(
        "graph_supervisor_routed",
        extra={
            "to_node": next_node,
            "reason": reason,
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
        },
    )

    # Pull request_id from ambient ContextVar so the audit row correlates
    # with the inbound HTTP request. Fall back to the state value when no
    # middleware has run (test contexts).
    rid: str | None = state.get("request_id")
    try:
        from observability.json_logging import request_id_var as _rid_var
        ctx_rid = _rid_var.get()
        if ctx_rid:
            rid = ctx_rid
    except (LookupError, ImportError):
        pass

    event = AuditEvent(
        event_type="node_handoff",
        request_id=rid,
        session_id=state.get("session_id"),
        provider_id=state.get("provider_id"),
        patient_id=state.get("patient_id"),
        outcome="success",
        duration_ms=duration_ms,
        detail_json={
            "from_node": "supervisor",
            "to_node": next_node,
            "decision_reason": reason,
            "duration_ms": duration_ms,
        },
    )
    try:
        await audit_writer.emit(event)
    except Exception as exc:  # pragma: no cover — emit() is fire-and-forget
        logger.warning(
            "graph_supervisor_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    return {"next_node": next_node}


__all__ = ["supervisor"]
