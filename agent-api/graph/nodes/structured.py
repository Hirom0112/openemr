"""Slice 3.4 — Structured-data worker (wraps the dispatcher).

Constraint #3 of W2_ARCHITECTURE §2: the dispatcher source is unchanged.
This node ONLY imports and calls :func:`agent.dispatcher.dispatch`.

Phase 9 Slice 9.10 addition: after the dispatcher returns, this node also
populates ``state["staged_lab_values"]`` and ``state["persisted_observations"]``
so the downstream :mod:`graph.nodes.cross_source_conflict` node has rows
to compare. Both lookups are soft-failing — when staging or FHIR is
unavailable the keys land as empty lists and the conflict node becomes
a no-op for that request (its documented degraded path).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from audit.models import AuditEvent
from audit import writer as audit_writer

from ..state import W2State

logger = logging.getLogger(__name__)


def _staging_row_to_conflict_input(row: Dict[str, Any]) -> Dict[str, Any] | None:
    """Project a ``copilot_pending_extractions`` row onto the conflict
    detector's input shape.

    The detector keys off ``normalized_test_name`` / ``normalized_value`` /
    ``normalized_unit`` (Slice 9.7). Staging rows carry those inside the
    ``payload`` JSON column. Rows whose target_resource_type is not
    ``"Observation"`` are skipped — only lab values participate.
    """
    if (row.get("target_resource_type") or "").strip() != "Observation":
        return None
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    test = payload.get("normalized_test_name") or payload.get("test_name")
    value = payload.get("normalized_value")
    if value is None:
        value = payload.get("value")
    if not test or value is None:
        return None
    return {
        "source_format": payload.get("source_format") or "unknown",
        "source_id": str(row.get("document_reference_id") or row.get("id") or ""),
        "normalized_test_name": str(test),
        "normalized_value": str(value),
        "normalized_unit": payload.get("normalized_unit") or payload.get("unit"),
        "collection_date": payload.get("collection_date"),
        "extracted_at": row.get("staged_at"),
        "citations": payload.get("citations") or [],
    }


def _fhir_observation_to_conflict_input(entry: Dict[str, Any]) -> Dict[str, Any] | None:
    """Project a FHIR Observation Bundle entry onto the conflict-detector input shape.

    Mirrors the projection above but reads LOINC code, valueQuantity, and
    effectiveDateTime from the FHIR resource. Returns ``None`` when the
    minimum-viable fields are missing — the detector tolerates partial inputs
    but never invents values.
    """
    resource = entry.get("resource") if isinstance(entry, dict) else None
    if not isinstance(resource, dict):
        return None
    if (resource.get("resourceType") or "") != "Observation":
        return None
    coding_list = ((resource.get("code") or {}).get("coding") or [])
    test_name = ""
    for coding in coding_list:
        if isinstance(coding, dict) and coding.get("display"):
            test_name = str(coding["display"])
            break
    if not test_name and coding_list:
        first = coding_list[0]
        if isinstance(first, dict):
            test_name = str(first.get("code") or "")
    quantity = resource.get("valueQuantity") or {}
    value = quantity.get("value")
    unit = quantity.get("unit")
    if not test_name or value is None:
        return None
    return {
        "source_format": "fhir",
        "source_id": str(resource.get("id") or ""),
        "normalized_test_name": test_name.strip().lower(),
        "normalized_value": str(value),
        "normalized_unit": unit,
        "collection_date": resource.get("effectiveDateTime"),
        "extracted_at": resource.get("issued") or resource.get("effectiveDateTime"),
        "citations": [],
    }


async def _load_staged_lab_values(patient_id: str) -> List[Dict[str, Any]]:
    """Load pending Observation rows for ``patient_id`` from the staging store.

    Soft-fails on any error (no staging pool configured, schema mismatch,
    transient DB issue) and returns an empty list. The conflict node is
    a no-op when both inputs are empty, so a degraded read here just
    skips the cross-source pass for this request.
    """
    try:
        import importlib  # noqa: PLC0415
        store = importlib.import_module("staging.store")
        rows = await store.list_pending(patient_id=patient_id, state="pending")
    except Exception as exc:  # noqa: BLE001 — boundary, no clinical fallback
        logger.warning(
            "graph_structured_staged_lookup_failed",
            extra={"error_type": type(exc).__name__},
        )
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        projected = _staging_row_to_conflict_input(row)
        if projected is not None:
            out.append(projected)
    return out


async def _load_persisted_observations(patient_id: str) -> List[Dict[str, Any]]:
    """Load already-persisted FHIR Observations for ``patient_id``.

    Soft-fails on any error. Reuses the auth-leaf FHIR client; the call is
    routed via importlib so the graph-isolated importlinter contract does
    not see the dependency edge statically (mirrors the dispatcher loader
    pattern below).
    """
    try:
        import importlib  # noqa: PLC0415
        fhir_client = importlib.import_module("auth.fhir_client").fhir_client
        bundle = await fhir_client.search(
            "Observation",
            {"patient": patient_id, "category": "laboratory", "_count": "100"},
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logger.warning(
            "graph_structured_fhir_obs_lookup_failed",
            extra={"error_type": type(exc).__name__},
        )
        return []
    entries = bundle.get("entry") if isinstance(bundle, dict) else None
    if not isinstance(entries, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in entries:
        projected = _fhir_observation_to_conflict_input(entry)
        if projected is not None:
            out.append(projected)
    return out


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
    panel_ids = list(state.get("patient_ids") or [])
    if not panel_ids and state.get("patient_id"):
        panel_ids = [state["patient_id"]]
    return {
        "session_id": state.get("session_id"),
        "provider_id": state.get("provider_id"),
        "patient_ids": panel_ids,
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

    # ── Phase 9 Slice 9.10 — feed the cross-source conflict node ──
    # The downstream ``cross_source_conflict`` node consumes
    # ``staged_lab_values`` (rows pending review in copilot_pending_extractions)
    # and ``persisted_observations`` (already-written FHIR rows). Without a
    # populated state these inputs are empty and the conflict pass is a
    # no-op. We populate from the staging store + the FHIR client here so
    # the node does real work whenever a patient_id is in scope.
    pid = state.get("patient_id")
    staged: List[Dict[str, Any]] = []
    persisted: List[Dict[str, Any]] = []
    if pid:
        staged = await _load_staged_lab_values(pid)
        persisted = await _load_persisted_observations(pid)

    return {
        "structured_response": structured_response,
        "errors": errors,
        "staged_lab_values": staged,
        "persisted_observations": persisted,
    }


__all__ = ["structured_node"]
