"""Slice 3.3 — Intake-extractor node.

Wraps :func:`extractors.lab.extract` for the LangGraph pipeline. Raw bytes
do NOT live in the graph state — only an opaque ``file_bytes_ref`` does.
The route handler (slice 3.9) injects a ``file_bytes_provider`` callback
that resolves the ref to bytes (e.g. from a session-keyed Redis blob).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from dataclasses import asdict

from audit.models import AuditEvent
from audit import writer as audit_writer
from documents.ocr import extract_layout
from extractors.lab import ExtractionFailed, extract

from ..state import W2State

logger = logging.getLogger(__name__)


async def _emit_handoff(
    *,
    state: W2State,
    to_node: str,
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
            "from_node": "intake_extractor",
            "to_node": to_node,
            "decision_reason": reason,
            "duration_ms": duration_ms,
        },
    )
    try:
        await audit_writer.emit(event)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "graph_extractor_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )


async def extractor_node(
    state: W2State,
    *,
    file_bytes_provider: Callable[[str], Awaitable[bytes]] | None = None,
) -> dict[str, Any]:
    """Resolve ``file_bytes_ref`` → bytes → run :func:`extract` → state["extraction"].

    Parameters
    ----------
    state:
        The W2 graph state. ``file_bytes_ref`` must be set when called by
        the supervisor.
    file_bytes_provider:
        Async callable that maps the opaque ref to raw PDF bytes. The route
        handler in slice 3.9 wires up the production provider that pulls
        from a session-keyed Redis blob.  TODO(slice-3.9): wire production
        provider.
    """
    t0 = time.monotonic()
    file_ref = state.get("file_bytes_ref")
    errors = list(state.get("errors") or [])

    if file_bytes_provider is None or not file_ref:
        errors.append("extractor: no file bytes")
        duration_ms = int((time.monotonic() - t0) * 1000)
        await _emit_handoff(
            state=state,
            to_node="finalize",
            outcome="failure",
            duration_ms=duration_ms,
            reason="missing file_bytes_ref or provider",
        )
        return {"errors": errors, "next_node": "finalize"}

    # Preserve any layout the caller pre-seeded (used by graph e2e tests
    # that mock `extract`); only overwrite when our own parse succeeds.
    ocr_layout: list[dict[str, Any]] | None = state.get("ocr_layout")
    try:
        pdf_bytes = await file_bytes_provider(file_ref)
        # Surface the OCR layout into graph state so the critic's
        # citation-resolvability and fidelity checks can reference real
        # bbox_ids. Cheap re-parse: PyMuPDF text-PDF parse is sub-100ms;
        # the lab extractor will parse again internally — accepted cost
        # for keeping the lab module's signature stable. Layout-parse
        # failure is non-fatal: the lab extractor's own parse will catch
        # genuinely-malformed PDFs and surface ExtractionFailed; a
        # transient layout error here just means the critic falls back
        # to whatever layout is already in state (typically empty).
        try:
            layout_blocks = extract_layout(pdf_bytes)
            ocr_layout = [asdict(b) for b in layout_blocks]
        except Exception as layout_exc:  # noqa: BLE001 — boundary
            logger.warning(
                "graph_extractor_layout_parse_failed",
                extra={"error_type": type(layout_exc).__name__},
            )
        extraction = await extract(
            pdf_bytes,
            patient_id=state.get("patient_id") or "",
            document_reference_id=f"draft-{state.get('request_id')}",
        )
        extraction_dict = extraction.model_dump(mode="json")
    except ExtractionFailed:
        errors.append("extractor: extraction failed")
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "graph_extractor_failed",
            extra={
                "request_id": state.get("request_id"),
                "duration_ms": duration_ms,
            },
        )
        await _emit_handoff(
            state=state,
            to_node="finalize",
            outcome="failure",
            duration_ms=duration_ms,
            reason="ExtractionFailed",
        )
        return {"errors": errors, "next_node": "finalize"}
    except Exception as exc:  # noqa: BLE001 — boundary
        errors.append(f"extractor: {type(exc).__name__}")
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "graph_extractor_unexpected_failure",
            extra={
                "request_id": state.get("request_id"),
                "error_type": type(exc).__name__,
                "duration_ms": duration_ms,
            },
        )
        await _emit_handoff(
            state=state,
            to_node="finalize",
            outcome="failure",
            duration_ms=duration_ms,
            reason="unexpected_error",
        )
        return {"errors": errors, "next_node": "finalize"}

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "graph_extractor_complete",
        extra={
            "request_id": state.get("request_id"),
            "kind": extraction_dict.get("kind"),
            "duration_ms": duration_ms,
        },
    )
    # Hand off to demographics; the supervisor on the NEXT pass owns the
    # actual routing decision. The demographics node is owned by a parallel
    # agent — until it lands, build.py routes "demographics" to the critic
    # passthrough (see graph.build).
    await _emit_handoff(
        state=state,
        to_node="demographics",
        outcome="success",
        duration_ms=duration_ms,
        reason="extraction complete",
    )
    update: dict[str, Any] = {
        "extraction": extraction_dict,
        "errors": errors,
        "next_node": "demographics",
    }
    if ocr_layout is not None:
        update["ocr_layout"] = ocr_layout
    return update


__all__ = ["extractor_node"]
