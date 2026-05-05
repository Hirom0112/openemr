"""Slice 3.9 — Demographics graph node (wrong-patient detection wrapper).

Pure-function comparator lives in ``demographics.check.check_demographics``;
this module is a thin async wrapper that:

  * resolves the chart Patient via an injected ``fhir_patient_provider`` (or
    the production :data:`auth.fhir_client.fhir_client` fallback),
  * extracts ``{mrn, name, dob}`` from the FHIR Patient JSON,
  * pulls document demographics from ``state["extraction"]`` if present,
  * runs the comparator and stamps the verdict on ``state["demographic_check"]``,
  * emits a ``demographic_check`` audit row carrying ONLY the categorical
    decision + reason_code (no names, MRNs, or DOBs),
  * always sets ``state["next_node"] = "critic"``.

The node is non-fatal: missing extraction, missing patient_id, or missing
document demographics simply skip the comparator and hand off to the critic
unmodified. The critic will fold a hard-block verdict into its decision
when this node sets one (see ``graph.nodes.critic._check_document_path``
demographic fold-in branch).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from agent.metrics import agent_w2_demographic_checks_total
from audit import writer as audit_writer
from audit.models import AuditEvent
from demographics.check import check_demographics

from ..state import W2State

logger = logging.getLogger(__name__)


def _extract_chart_demographics(patient: dict[str, Any]) -> dict[str, str]:
    """Pull ``{mrn, name, dob}`` from a FHIR R4 Patient resource.

    Mirrors the parsing in :mod:`briefing.context_builder` so the chart-side
    representation matches across the codebase.
    """
    names = patient.get("name") or []
    name_str = ""
    if names:
        n = names[0] or {}
        given = " ".join(n.get("given") or [])
        family = n.get("family") or ""
        name_str = f"{given} {family}".strip()

    dob = patient.get("birthDate") or ""

    mrn = ""
    for ident in patient.get("identifier") or []:
        codings = (ident.get("type") or {}).get("coding") or [{}]
        if codings and codings[0].get("code") == "MR":
            mrn = ident.get("value") or ""
            break
    if not mrn:
        # Fallback: use the FHIR resource id when no MR-coded identifier exists.
        mrn = patient.get("id") or ""

    return {"mrn": mrn, "name": name_str, "dob": dob}


async def demographics_node(
    state: W2State,
    *,
    fhir_patient_provider: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run the wrong-patient comparator and stamp ``state["demographic_check"]``.

    Always returns ``{"next_node": "critic"}`` regardless of the verdict —
    the critic owns the final decision. Failures here are logged + soft.
    """
    t0 = time.monotonic()
    extraction = state.get("extraction")
    patient_id = state.get("patient_id")
    out: dict[str, Any] = {"next_node": "critic"}

    # ── Skip when we have nothing to compare ────────────────────────────────
    if extraction is None or not patient_id:
        logger.info(
            "graph_demographics_skipped",
            extra={
                "request_id": state.get("request_id"),
                "session_id": state.get("session_id"),
                "reason": "no_extraction_or_patient",
            },
        )
        return out

    # LabReport / UnknownDocument extractors do not currently emit a
    # ``demographics`` block (W2 §5.6: doc-side demographics extraction
    # lands later). Soft-skip when the extraction has no demographics.
    doc_demo = extraction.get("demographics") if isinstance(extraction, dict) else None
    if not doc_demo:
        logger.info(
            "graph_demographics_skipped",
            extra={
                "request_id": state.get("request_id"),
                "session_id": state.get("session_id"),
                "reason": "DEMOGRAPHICS_NOT_EXTRACTED",
            },
        )
        return out

    # ── Resolve chart Patient ───────────────────────────────────────────────
    try:
        if fhir_patient_provider is not None:
            chart_patient = await fhir_patient_provider(patient_id)
        else:
            from auth.fhir_client import fhir_client  # local import — leaf boundary
            chart_patient = await fhir_client.get_patient(patient_id)
    except Exception as exc:  # noqa: BLE001 — fail soft, hand off to critic
        logger.warning(
            "graph_demographics_fhir_fetch_failed",
            extra={
                "request_id": state.get("request_id"),
                "session_id": state.get("session_id"),
                "error_type": type(exc).__name__,
            },
        )
        return out

    chart = _extract_chart_demographics(chart_patient)
    result = check_demographics(
        document_demographics=doc_demo,
        chart_patient=chart,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)

    out["demographic_check"] = {
        "decision": result.decision,
        "reason_code": result.reason_code,
        "message": result.message,
    }

    # The extractor schemas (LabReport / UnknownDocument) forbid extras, so
    # downstream critic schema-validation would reject any document-side
    # demographics payload that happens to ride inside ``extraction``. Pop it
    # back out once we've consumed it so the critic sees the clean shape.
    if isinstance(extraction, dict) and "demographics" in extraction:
        cleaned = dict(extraction)
        cleaned.pop("demographics", None)
        out["extraction"] = cleaned

    logger.info(
        "graph_demographics_decision",
        extra={
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
            "decision": result.decision,
            "reason_code": result.reason_code,
            "duration_ms": duration_ms,
        },
    )

    # ── Audit row — categorical fields only, no PHI ─────────────────────────
    rid: str | None = state.get("request_id")
    try:
        from observability.json_logging import request_id_var as _rid_var
        ctx_rid = _rid_var.get()
        if ctx_rid:
            rid = ctx_rid
    except (LookupError, ImportError):
        pass

    event = AuditEvent(
        event_type="demographic_check",
        request_id=rid,
        session_id=state.get("session_id"),
        provider_id=state.get("provider_id"),
        patient_id=state.get("patient_id"),
        outcome="success" if result.decision != "hard_block" else "denied",
        duration_ms=duration_ms,
        detail_json={
            "decision": result.decision,
            "reason_code": result.reason_code,
        },
    )
    try:
        await audit_writer.emit(event)
    except Exception as exc:  # pragma: no cover — fire-and-forget
        logger.warning(
            "graph_demographics_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    # ── Metric inc (paired with one structured log event) ───────────────────
    try:
        agent_w2_demographic_checks_total.labels(outcome=result.decision).inc()
        logger.info(
            "graph_demographics_metric",
            extra={
                "outcome": result.decision,
                "reason_code": result.reason_code,
            },
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "graph_demographics_metric_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    return out


__all__ = ["demographics_node"]
