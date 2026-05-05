"""Slice 3.8 — Finalize node + SSE-frame helper.

Pure aggregator: packs the W2State pieces into a final response envelope
on ``state["finalized"]``. The SSE wrapping happens in the route handler
(slice 3.9); the finalize concern owns the SSE *format* via :func:`sse_frame`.
"""
from __future__ import annotations

import json as _json
import logging
import time
from typing import Any

from audit import writer as audit_writer
from audit.models import AuditEvent

from ..state import W2State

logger = logging.getLogger(__name__)


async def finalize_node(state: W2State) -> dict[str, Any]:
    """Aggregate state pieces into a single ``finalized`` envelope."""
    t0 = time.monotonic()
    finalized = {
        "extraction": state.get("extraction"),
        "retrieval": state.get("retrieval"),
        "critic_decision": state.get("critic_decision"),
        "soft_warns": state.get("soft_warns") or [],
        "structured_response": state.get("structured_response"),
        "errors": state.get("errors") or [],
    }
    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "graph_finalize_complete",
        extra={
            "has_extraction": bool(state.get("extraction")),
            "critic_decision": state.get("critic_decision"),
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
        },
    )

    rid: str | None = state.get("request_id")
    try:
        from observability.json_logging import request_id_var as _rid_var
        ctx_rid = _rid_var.get()
        if ctx_rid:
            rid = ctx_rid
    except (LookupError, ImportError):
        pass

    try:
        await audit_writer.emit(
            AuditEvent(
                event_type="node_handoff",
                request_id=rid,
                session_id=state.get("session_id"),
                provider_id=state.get("provider_id"),
                patient_id=state.get("patient_id"),
                outcome="success",
                duration_ms=duration_ms,
                detail_json={
                    "from_node": "finalize",
                    "to_node": "END",
                    "critic_decision": state.get("critic_decision"),
                },
            )
        )
    except Exception as exc:  # pragma: no cover — fire-and-forget
        logger.warning(
            "graph_finalize_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )
    return {"finalized": finalized}


def sse_frame(event: str, data: dict) -> str:
    """Format a Server-Sent Event frame matching ``main.py``'s ``_sse_format``."""
    return f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"


__all__ = ["finalize_node", "sse_frame"]
