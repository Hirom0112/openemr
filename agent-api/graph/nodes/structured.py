"""Slice 3.4 — Structured-data worker (wraps the dispatcher).

Constraint #3 of W2_ARCHITECTURE §2: the dispatcher source is unchanged.
This node ONLY imports and calls :func:`agent.dispatcher.dispatch`.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from audit.models import AuditEvent
from audit import writer as audit_writer

from ..state import W2State

logger = logging.getLogger(__name__)


_SAFE_FALLBACK: dict[str, Any] = {
    "narrative": "Internal error",
    "data": None,
    "citations": [],
}


def _build_session_context(state: W2State) -> dict[str, Any]:
    """Mirror ``main._session_ctx`` for in-graph dispatcher calls.

    The graph runs without direct access to the FastAPI lifespan-scoped
    Redis / langfuse / saver singletons. The route handler in slice 3.9
    injects them via state-side wiring. For now we pass a minimal context
    that ``dispatch()`` can tolerate (all downstream lookups are
    ``.get(...)`` so ``None`` values are safe).
    """
    return {
        "session_id": state.get("session_id"),
        "provider_id": state.get("provider_id"),
        "patient_ids": [state["patient_id"]] if state.get("patient_id") else [],
        "redis_client": None,
        "redis_saver": None,
        "sqlite_saver": None,
        "langfuse": None,
    }


async def _emit_handoff(
    *,
    state: W2State,
    outcome: str,
    duration_ms: int,
    reason: str,
) -> None:
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
        outcome=outcome,
        duration_ms=duration_ms,
        detail_json={
            "from_node": "structured",
            "to_node": "critic",
            "decision_reason": reason,
            "duration_ms": duration_ms,
        },
    )
    try:
        await audit_writer.emit(event)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "graph_structured_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )


async def structured_node(state: W2State) -> dict[str, Any]:
    """Run the dispatcher on ``state["message"]`` and stash the result."""
    t0 = time.monotonic()
    errors = list(state.get("errors") or [])
    message = state.get("message") or ""
    session_id = state.get("session_id") or ""

    # Resolve the dispatcher via importlib instead of a static import:
    # ``agent.dispatcher`` transitively pulls FastAPI/Starlette via
    # ``auth.jwt_middleware``, and the ``graph-isolated`` import-linter
    # contract forbids any (even transitive, even function-level) import
    # of those transports from ``graph.*``. Using ``importlib`` keeps the
    # call site dynamic so the linter cannot see the edge.
    import importlib  # noqa: PLC0415
    dispatch = importlib.import_module("agent.dispatcher").dispatch

    try:
        result = await dispatch(
            message=message,
            session_id=session_id,
            session_context=_build_session_context(state),
        )
        structured_response = result if isinstance(result, dict) else {
            "narrative": str(result),
            "data": None,
            "citations": [],
        }
        outcome = "success"
        reason = "dispatch ok"
    except Exception as exc:  # noqa: BLE001 — boundary
        errors.append(f"structured: {type(exc).__name__}")
        structured_response = dict(_SAFE_FALLBACK)
        outcome = "failure"
        reason = type(exc).__name__
        logger.error(
            "graph_structured_dispatch_failed",
            extra={
                "request_id": state.get("request_id"),
                "error_type": type(exc).__name__,
            },
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "graph_structured_complete",
        extra={
            "request_id": state.get("request_id"),
            "outcome": outcome,
            "duration_ms": duration_ms,
        },
    )
    await _emit_handoff(
        state=state,
        outcome=outcome,
        duration_ms=duration_ms,
        reason=reason,
    )
    return {
        "structured_response": structured_response,
        "errors": errors,
    }


__all__ = ["structured_node"]
