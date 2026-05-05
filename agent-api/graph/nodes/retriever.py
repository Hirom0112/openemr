"""Slice 3.5 — Evidence-retriever stub.

Phase 4 will replace this with real RAG. For now it returns an empty
snippet list and emits a node_handoff row so the audit shape is stable.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from audit.models import AuditEvent
from audit import writer as audit_writer

from ..state import W2State

logger = logging.getLogger(__name__)


async def retriever_node(state: W2State) -> dict[str, Any]:
    t0 = time.monotonic()
    logger.info(
        "graph_retriever_stub_called",
        extra={
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
        },
    )
    duration_ms = int((time.monotonic() - t0) * 1000)

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
            "from_node": "retriever",
            "to_node": "critic",
            "decision_reason": "stub: empty retrieval",
            "duration_ms": duration_ms,
        },
    )
    try:
        await audit_writer.emit(event)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "graph_retriever_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    return {"retrieval": {"snippets": [], "fallback_used": False}}


__all__ = ["retriever_node"]
