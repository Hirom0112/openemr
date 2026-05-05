"""Slice 3.6 — Critic node.

Single mode, two response classes (W2_ARCHITECTURE §5.8):

* **Hard-block** — schema invalid, citation missing/unresolvable/fabricated,
  or a §5.6 hard-block from the demographic comparator.
* **Soft-warn** — surfaced via ``state["soft_warns"]`` (low OCR confidence,
  unknown classifier, demographic soft-warn, stale guideline).

Two evaluation paths converge here:

* **Structured-data path** (``state["structured_response"]`` set) — reuses
  the existing W1 :func:`verification.dispatcher_response.verify_dispatcher_response`
  and translates its result into a critic verdict. The W1 stripping behaviour
  is preserved by replacing ``structured_response`` with the modified one.
* **Document path** (``state["extraction"]`` set) — schema validity, then
  citation presence, then citation resolvability against the layout, then
  citation fidelity (skipped on low document-level OCR confidence per §8.7),
  then folding in the demographic check.

Failure-closed boundary: any unexpected ``Exception`` at the entry point
turns into a ``hard_block`` with violation ``"CRITIC_ERROR"``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from audit import writer as audit_writer
from audit.models import AuditEvent
from extractors.schemas import LabReport, UnknownDocument
from pydantic import ValidationError
from verification.dispatcher_response import (
    VerificationResult,
    verify_dispatcher_response,
)

from ..state import W2State

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

_OCR_CONFIDENCE_THRESHOLD = 0.6  # §8.7
_GUIDELINE_STALE_AFTER = timedelta(days=24 * 30)  # ≈ 24 months, §6 / §16

_NUM_TOKEN_RE = re.compile(r"[·,]")
_WS_RE = re.compile(r"\s+")


# ── Normalization (§8.6 / §10.4) ─────────────────────────────────────────────


def _normalize(text: str) -> str:
    """Case-fold, whitespace-collapse, normalize numeric separators."""
    folded = _NUM_TOKEN_RE.sub(".", text).lower()
    return _WS_RE.sub(" ", folded).strip()


def _is_normalized_substring(quote: str, block_text: str) -> bool:
    return _normalize(quote) in _normalize(block_text)


# ── Structured-data path ─────────────────────────────────────────────────────


def _from_w1_result(result: VerificationResult) -> tuple[str, list[str]]:
    """Translate a W1 :class:`VerificationResult` into a W2 critic verdict.

    Returns ``(decision, violations)``.
    """
    if result.blocked:
        violations = list(result.violations)
        if result.physician_message:
            violations.append(result.physician_message)
        return "hard_block", violations
    if not result.passed:
        return "soft_warn", list(result.violations)
    return "pass", []


# ── Document path ────────────────────────────────────────────────────────────


def _validate_schema(extraction: dict[str, Any]) -> tuple[Any | None, str | None]:
    """Return ``(parsed_model, error_str)``. ``parsed_model`` is None on failure."""
    kind = extraction.get("kind")
    try:
        if kind == "lab_report":
            return LabReport.model_validate_json(json.dumps(extraction)), None
        if kind == "unknown":
            return UnknownDocument.model_validate_json(json.dumps(extraction)), None
        return None, f"unsupported_kind:{kind!r}"
    except ValidationError as exc:
        return None, str(exc)


def _gather_cited_items(model: Any) -> list[Any]:
    """Return the list of items that must each carry citations.

    For LabReport that's ``values``; for UnknownDocument that's ``key_facts``.
    """
    if isinstance(model, LabReport):
        return list(model.values)
    if isinstance(model, UnknownDocument):
        return list(model.key_facts)
    return []


def _layout_index(ocr_layout: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Index ocr_layout by ``bbox_id`` for O(1) citation resolution."""
    if not ocr_layout:
        return {}
    return {block["bbox_id"]: block for block in ocr_layout if "bbox_id" in block}


def _document_ocr_confidence(ocr_layout: list[dict[str, Any]] | None) -> float:
    """Min of per-block ocr_confidence — fail closed. Returns 1.0 if no layout."""
    if not ocr_layout:
        return 1.0
    confs = [
        float(b.get("ocr_confidence", 1.0))
        for b in ocr_layout
        if "ocr_confidence" in b
    ]
    if not confs:
        return 1.0
    return min(confs)


def _check_document_path(
    extraction: dict[str, Any],
    ocr_layout: list[dict[str, Any]] | None,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Run the document-path checks.

    Returns ``(decision, violations, soft_warns_to_append)``.
    """
    soft_warns: list[dict[str, Any]] = []

    # 1. Schema validity ─────────────────────────────────────────────────────
    model, schema_err = _validate_schema(extraction)
    if model is None:
        return "hard_block", ["SCHEMA_INVALID"], soft_warns

    # 2. Citation presence ───────────────────────────────────────────────────
    items = _gather_cited_items(model)
    for item in items:
        # Pydantic min_length=1 already enforces this, but the check is cheap
        # and catches future schema relaxations or hand-built dicts that
        # bypassed validation.
        citations = getattr(item, "citations", None) or []
        if len(citations) == 0:
            return "hard_block", ["CITATION_MISSING"], soft_warns

    # 3. Citation resolvability ──────────────────────────────────────────────
    layout = _layout_index(ocr_layout)
    for item in items:
        for citation in item.citations:
            if citation.source_type != "document":
                continue  # guideline citations resolved against guideline corpus, not here
            if citation.field_or_chunk_id not in layout:
                return "hard_block", ["CITATION_UNRESOLVABLE"], soft_warns

    # 4. Citation fidelity (§8.7 — skip on low confidence) ───────────────────
    doc_conf = _document_ocr_confidence(ocr_layout)
    if doc_conf < _OCR_CONFIDENCE_THRESHOLD:
        soft_warns.append(
            {
                "code": "OCR_LOW_CONFIDENCE",
                "message": (
                    "Scan quality low — citations are best-effort; "
                    "value-fidelity check disabled. Verify against source."
                ),
            }
        )
    else:
        for item in items:
            for citation in item.citations:
                if citation.source_type != "document":
                    continue
                block = layout.get(citation.field_or_chunk_id)
                if block is None:
                    return "hard_block", ["CITATION_UNRESOLVABLE"], soft_warns
                if not _is_normalized_substring(
                    citation.quote_or_value, str(block.get("text", ""))
                ):
                    return "hard_block", ["CITATION_FIDELITY_FAILED"], soft_warns

    return "pass", [], soft_warns


# ── Soft-warn folding ────────────────────────────────────────────────────────


def _stale_guideline_softwarns(
    retrieval: dict[str, Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    if not retrieval:
        return []
    snippets = retrieval.get("snippets") or []
    cutoff = now - _GUIDELINE_STALE_AFTER
    for snippet in snippets:
        idv = snippet.get("indexed_version_date")
        if not idv:
            continue
        try:
            idv_date = datetime.fromisoformat(str(idv)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if idv_date < cutoff:
            return [
                {
                    "code": "GUIDELINE_OUTDATED",
                    "message": (
                        "Guideline citation may be superseded — most recent "
                        f"indexed version is from {idv}."
                    ),
                }
            ]
    return []


# ── Public entry point ───────────────────────────────────────────────────────


async def critic_node(state: W2State) -> dict[str, Any]:
    """Evaluate the upstream worker output and emit one audit row."""
    t0 = time.monotonic()
    decision: str = "pass"
    violations: list[str] = []
    soft_warns: list[dict[str, Any]] = list(state.get("soft_warns") or [])
    out: dict[str, Any] = {}

    try:
        # ── Structured-data path ────────────────────────────────────────────
        structured = state.get("structured_response")
        if structured is not None:
            result = verify_dispatcher_response(
                response=structured,
                fhir_context={"resources": {}},
                patient_id=state.get("patient_id"),
            )
            decision, violations = _from_w1_result(result)
            out["structured_response"] = result.modified_response

        # ── Document path ───────────────────────────────────────────────────
        extraction = state.get("extraction")
        if extraction is not None and decision != "hard_block":
            doc_decision, doc_violations, doc_softs = _check_document_path(
                extraction, state.get("ocr_layout")
            )
            if doc_decision == "hard_block":
                decision = "hard_block"
                violations.extend(doc_violations)
            elif doc_decision == "soft_warn" and decision == "pass":
                decision = "soft_warn"
                violations.extend(doc_violations)
            soft_warns.extend(doc_softs)

        # ── Demographic fold-in ─────────────────────────────────────────────
        demo = state.get("demographic_check")
        if demo:
            demo_decision = demo.get("decision")
            if demo_decision == "hard_block":
                decision = "hard_block"
                code = demo.get("reason_code") or "DEMOGRAPHIC_HARD_BLOCK"
                if code not in violations:
                    violations.append(code)
            elif demo_decision == "soft_warn":
                soft_warns.append(
                    {
                        "code": demo.get("reason_code") or "DEMOGRAPHIC_SOFT_WARN",
                        "message": demo.get("message") or "",
                    }
                )

        # ── Stale-guideline soft-warn ───────────────────────────────────────
        soft_warns.extend(
            _stale_guideline_softwarns(state.get("retrieval"), datetime.now(timezone.utc))
        )

    except Exception as exc:  # noqa: BLE001 — fail closed at the boundary
        logger.exception(
            "graph_critic_error",
            extra={
                "request_id": state.get("request_id"),
                "session_id": state.get("session_id"),
                "error_type": type(exc).__name__,
            },
        )
        decision = "hard_block"
        violations = ["CRITIC_ERROR"]

    duration_ms = int((time.monotonic() - t0) * 1000)

    out["critic_decision"] = decision
    out["critic_violations"] = violations
    out["soft_warns"] = soft_warns

    logger.info(
        "graph_critic_decision",
        extra={
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
            "decision": decision,
            "violation_count": len(violations),
            "soft_warn_count": len(soft_warns),
            "duration_ms": duration_ms,
        },
    )

    # Emit one node_handoff audit row (critic → finalize). detail_json carries
    # the decision + violation codes only; no clinical text.
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
        outcome="success" if decision != "hard_block" else "denied",
        duration_ms=duration_ms,
        detail_json={
            "from_node": "critic",
            "to_node": "finalize",
            "decision": decision,
            "violation_codes": list(violations),
            "soft_warn_codes": [w.get("code") for w in soft_warns if w.get("code")],
        },
    )
    try:
        await audit_writer.emit(event)
    except Exception as exc:  # pragma: no cover — emit() is fire-and-forget
        logger.warning(
            "graph_critic_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    return out


__all__ = ["critic_node"]
