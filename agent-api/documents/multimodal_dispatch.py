"""Phase 3 Item 2 — Shared multimodal extraction dispatcher.

Single source of truth for "given raw bytes + detected format, produce a
typed extraction object". Used by:

* The HTTP ``/document/ingest`` route (``main._dispatch_multimodal_ingest``):
  the route still owns FHIR write + claim + staging + audit emission, and
  calls into this module ONLY for the parser/extractor invocation.
* The LangGraph ``intake_extractor`` node (``graph.nodes.extractor``): the
  node calls in with ``dry_run=True`` so no FHIR / DB side-effects fire,
  and consumes the resulting extraction dict directly into ``state["extraction"]``.

Why a separate coroutine instead of moving ``_dispatch_multimodal_ingest``
wholesale? The HTTP dispatcher entangles parser invocation with FHIR
writes, idempotent claims, observation staging, audit emission, and
``HTTPException`` raises — all of which depend on the FastAPI request
lifecycle and would break the ``graph-isolated`` import contract
(``graph.* must not import fastapi/starlette``). This module exposes the
narrow "bytes → typed extraction" surface; both callers compose the rest
themselves.

Returns a discriminated dict shape so the graph state ``extraction``
field stays JSON-serialisable through LangGraph's checkpoint pipeline.
The dict's ``kind`` discriminator is one of:

* ``"lab_report"``        — HL7 ORU^R01, XLSX Labs_Trend (single-lane)
* ``"intake_form"``       — DOCX referral, XLSX (single-lane fallback),
                            TIFF when the classifier identifies an intake
* ``"unknown"``           — DOCX with no recoverable structure, TIFF that
                            classifier punts on
* ``"demographic_update"``— HL7 ADT^A08
* ``"workbook"``          — XLSX (multi-lane wrapper, see
                            extractors.schemas.WorkbookExtraction)

Failure modes raise ``MultimodalParseError`` (a plain ``Exception``
subclass) — callers translate to HTTP status / graph errors.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from extractors.schemas import (
    IntakeForm,
    LabReport,
    UnknownDocument,
    WorkbookExtraction,
)

logger = logging.getLogger(__name__)


__all__ = [
    "MultimodalParseError",
    "ParseMultimodalResult",
    "parse_multimodal",
    "parse_multimodal_full",
    "extraction_to_dict",
]


@dataclass(frozen=True)
class ParseMultimodalResult:
    """Result of :func:`parse_multimodal_full`.

    Carries both the JSON-serialisable ``extraction`` dict (for graph state)
    AND the raw parser/extractor return value (for HTTP-side staging,
    quarantine, audit, FHIR persist). Callers that only need the dict
    should keep using :func:`parse_multimodal`, which returns just the
    dict for backward compatibility with the LangGraph extractor node.

    Why a wrapper instead of a tuple? The HTTP dispatcher needs:

    * ``parsed_raw`` — for HL7 ``isinstance(parsed, DemographicUpdateEvent)``
      branching, ``parsed.values``, ``parsed.event_type``, ``parsed.control_id``.
    * For DOCX/TIFF — Pydantic attribute access (``extraction.demographics``,
      ``.allergies``, ``.kind``, ``.classifier_confidence``,
      ``.ocr_confidence_range``) plus ``model_dump`` for persist.

    A dict alone loses the typed surface; a raw object alone forces every
    caller to recompute ``model_dump``. We return both — the cost is one
    extra reference, the gain is no duplication of parser-call logic.
    """

    extraction_dict: Dict[str, Any]
    parsed_raw: Any  # ParsedHL7 union | LabReport | IntakeForm | UnknownDocument | WorkbookExtraction
    detected_format: str
    duration_ms: int


class MultimodalParseError(Exception):
    """Raised when a multimodal parser fails or the format is unsupported.

    The HTTP dispatcher translates to ``HTTPException(400|415|500)`` based
    on ``code``; the graph node logs and routes to ``finalize`` with an
    ``errors`` entry.
    """

    def __init__(self, code: str, *, status: int = 400, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.status = status
        self.detail = detail or code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extraction_to_dict(extraction: Any) -> Dict[str, Any]:
    """Coerce any parser/extractor return into a JSON-serialisable dict.

    Pydantic models go through ``model_dump(mode="json")``; dataclasses
    via ``asdict``; dicts pass through. Anything else raises so we surface
    contract drift rather than silently losing fields.
    """
    if hasattr(extraction, "model_dump") and callable(extraction.model_dump):
        return extraction.model_dump(mode="json")  # type: ignore[no-any-return]
    if is_dataclass(extraction):
        return asdict(extraction)
    if isinstance(extraction, dict):
        return dict(extraction)
    raise TypeError(
        f"Unsupported extraction type: {type(extraction).__name__}"
    )


def _wrap_workbook(
    parsed_wb: Any,
    *,
    patient_id: str,
    document_reference_id: str,
) -> WorkbookExtraction:
    """Promote a ``ParsedWorkbook`` into the discriminated ``WorkbookExtraction``.

    Carries through the embedded ``intake_form`` / ``lab_reports`` /
    ``pending_tasks`` Pydantic instances unchanged. The wrapper carries
    the patient + document identity (so eval rubrics keyed off those
    fields work uniformly) plus the workbook-level confidence stub.
    XLSX is text-only (no OCR), so ``ocr_confidence_range`` is fixed at
    (1.0, 1.0) — every cell that openpyxl returns is an exact verbatim.
    """
    return WorkbookExtraction(
        kind="workbook",
        schema_version="1.0",
        patient_id=patient_id,
        document_reference_id=document_reference_id,
        intake_form=parsed_wb.intake_form,
        lab_reports=list(parsed_wb.lab_reports or []),
        pending_tasks=list(parsed_wb.pending_tasks or []),
        classifier_confidence=1.0,
        ocr_confidence_range=(1.0, 1.0),
        extracted_at=datetime.now(timezone.utc),
    )


async def parse_multimodal_full(
    *,
    detected_format: str,
    raw_bytes: bytes,
    patient_id: str,
    document_reference_id: str,
    dry_run: bool = False,
    request_id: Optional[str] = None,
) -> ParseMultimodalResult:
    """Parser/extractor dispatch returning BOTH dict + raw object.

    The HTTP ``/document/ingest`` route uses this entry point so it can
    keep the existing isinstance-branching, Pydantic attribute access, and
    staging fan-out semantics while sourcing the parsed extraction from
    the same code as the LangGraph extractor node.

    See :class:`ParseMultimodalResult` for shape, :func:`parse_multimodal`
    for the dict-only convenience used by the graph node.
    """
    return await _parse_multimodal_impl(
        detected_format=detected_format,
        raw_bytes=raw_bytes,
        patient_id=patient_id,
        document_reference_id=document_reference_id,
        dry_run=dry_run,
        request_id=request_id,
    )


async def parse_multimodal(
    *,
    detected_format: str,
    raw_bytes: bytes,
    patient_id: str,
    document_reference_id: str,
    dry_run: bool = False,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse ``raw_bytes`` per ``detected_format`` and return an extraction dict.

    Parameters
    ----------
    detected_format:
        One of ``"hl7"``, ``"xlsx"``, ``"docx"``, ``"tiff"``. Caller must
        have already run ``documents.format_detect.detect_ingest_format``;
        any other value raises ``MultimodalParseError("unsupported_format")``.
    raw_bytes:
        Source bytes — same buffer the HTTP route received. Parsers stream
        in-memory; no temp files are written.
    patient_id, document_reference_id:
        Identity carried onto every emitted extraction + Citation.
    dry_run:
        Reserved for parity with the HTTP dispatcher's contract — none of
        the parsers in this module write to FHIR or staging tables, so the
        flag is currently a no-op (parser invocation is itself
        side-effect-free). Kept on the signature so callers don't need to
        change when future per-format adapters acquire write side-effects.
    request_id:
        Ambient request id used for structured logging only. Production
        callers should rely on the ``observability.json_logging.request_id_var``
        ContextVar; this parameter exists so test fixtures can override it.

    Returns
    -------
    A discriminated dict with ``kind`` ∈
    {``lab_report``, ``intake_form``, ``unknown``, ``demographic_update``,
    ``workbook``}.

    Raises
    ------
    MultimodalParseError
        On parser failure or unsupported format. ``code`` carries a
        short snake_case discriminator the caller can mark on metrics.
    """
    result = await _parse_multimodal_impl(
        detected_format=detected_format,
        raw_bytes=raw_bytes,
        patient_id=patient_id,
        document_reference_id=document_reference_id,
        dry_run=dry_run,
        request_id=request_id,
    )
    return result.extraction_dict


async def _parse_multimodal_impl(
    *,
    detected_format: str,
    raw_bytes: bytes,
    patient_id: str,
    document_reference_id: str,
    dry_run: bool,
    request_id: Optional[str],
) -> ParseMultimodalResult:
    """Internal — actually run the parser dispatch.

    Returns both the JSON-serialisable dict (graph state) AND the raw
    parser/extractor object (HTTP-side staging / quarantine / persist).
    See :class:`ParseMultimodalResult` for the rationale on the dual-shape
    return.

    The XLSX branch deliberately calls the side-effect-free
    :func:`parsers.xlsx.parse_xlsx` (NOT ``parse_and_stage``); the HTTP
    dispatcher composes its own staging fan-out on top because staging is
    HTTP-only behavior. ``parse_and_stage`` and this function both
    delegate to ``parse_xlsx`` — there is no parser-call duplication
    between them.
    """
    t0 = time.perf_counter()
    fmt = (detected_format or "").lower()

    if fmt == "hl7":
        from parsers.hl7 import dispatch as _hl7_dispatch
        from parsers.hl7.exceptions import (
            ParserMalformedError,
            ParserUnsupportedError,
        )

        try:
            parsed = _hl7_dispatch.parse_hl7(
                raw_bytes,
                document_reference_id=document_reference_id,
                patient_id=patient_id,
            )
        except ParserMalformedError as exc:
            raise MultimodalParseError(
                "hl7_malformed", status=400, detail="HL7 v2 message malformed"
            ) from exc
        except ParserUnsupportedError as exc:
            raise MultimodalParseError(
                "hl7_unsupported",
                status=415,
                detail="HL7 v2 message type unsupported",
            ) from exc

        out = extraction_to_dict(parsed)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "multimodal_parse_complete",
            extra={
                "request_id": request_id,
                "format": "hl7",
                "kind": out.get("kind"),
                "duration_ms": duration_ms,
                "dry_run": dry_run,
            },
        )
        return ParseMultimodalResult(
            extraction_dict=out,
            parsed_raw=parsed,
            detected_format="hl7",
            duration_ms=duration_ms,
        )

    if fmt == "xlsx":
        from parsers.xlsx import (
            XlsxMacroRejected,
            XlsxMalformedError,
            XlsxMergedCellsRejected,
            parse_xlsx,
        )

        try:
            parsed_wb = parse_xlsx(
                raw_bytes,
                document_reference_id=document_reference_id,
                patient_id=patient_id,
            )
        except (XlsxMacroRejected, XlsxMergedCellsRejected) as exc:
            raise MultimodalParseError(
                "xlsx_rejected", status=400, detail=str(exc) or "XLSX rejected"
            ) from exc
        except XlsxMalformedError as exc:
            raise MultimodalParseError(
                "xlsx_malformed", status=400, detail="XLSX workbook malformed"
            ) from exc

        wrapped = _wrap_workbook(
            parsed_wb,
            patient_id=patient_id,
            document_reference_id=document_reference_id,
        )
        out = extraction_to_dict(wrapped)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "multimodal_parse_complete",
            extra={
                "request_id": request_id,
                "format": "xlsx",
                "kind": out.get("kind"),
                "lab_reports": len(parsed_wb.lab_reports or []),
                "pending_tasks": len(parsed_wb.pending_tasks or []),
                "intake_form_present": parsed_wb.intake_form is not None,
                "duration_ms": duration_ms,
                "dry_run": dry_run,
            },
        )
        return ParseMultimodalResult(
            extraction_dict=out,
            parsed_raw=parsed_wb,
            detected_format="xlsx",
            duration_ms=duration_ms,
        )

    if fmt == "docx":
        # DOCX prose-mode intake extraction. Calls the existing async
        # extractor that handles paragraph load + Claude-driven prose
        # extraction + ICD-10 grounding. This DOES make an LLM call —
        # callers running offline (no ANTHROPIC_API_KEY) will see a
        # ``MultimodalParseError("docx_extract_failed")`` instead of an
        # empty extraction. The eval suite documents this expectation in
        # the live-eval gate.
        from extractors import intake as _intake

        try:
            extraction = await _intake.extract_intake_from_docx(
                raw_bytes,
                patient_id=patient_id,
                document_reference_id=document_reference_id,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            raise MultimodalParseError(
                "docx_extract_failed",
                status=500,
                detail="DOCX extraction failed",
            ) from exc

        out = extraction_to_dict(extraction)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "multimodal_parse_complete",
            extra={
                "request_id": request_id,
                "format": "docx",
                "kind": out.get("kind"),
                "duration_ms": duration_ms,
                "dry_run": dry_run,
            },
        )
        return ParseMultimodalResult(
            extraction_dict=out,
            parsed_raw=extraction,
            detected_format="docx",
            duration_ms=duration_ms,
        )

    if fmt == "tiff":
        # TIFF flow: extract layout via the dedicated loader (multi-page
        # OCR), classify with the keyword classifier, then dispatch to the
        # appropriate vision-mode extractor (LabReport vs IntakeForm).
        # Both extractors call Claude and require ANTHROPIC_API_KEY; same
        # offline caveat as DOCX.
        from documents.tiff_loader import extract_tiff_layout
        from extractors import intake as _intake
        from extractors import lab as _lab
        from extractors.classifier import classify_keywords as _classify_keywords

        try:
            layout_blocks = extract_tiff_layout(raw_bytes)
        except Exception as exc:  # noqa: BLE001 — boundary
            raise MultimodalParseError(
                "tiff_layout_failed",
                status=400,
                detail="TIFF layout extraction failed",
            ) from exc

        verdict = _classify_keywords(layout_blocks) if layout_blocks else None
        try:
            if verdict is not None and verdict.kind == "intake_form":
                extraction = await _intake.extract_intake(
                    raw_bytes,
                    patient_id=patient_id,
                    document_reference_id=document_reference_id,
                )
            else:
                extraction = await _lab.extract(
                    raw_bytes,
                    patient_id=patient_id,
                    document_reference_id=document_reference_id,
                )
        except Exception as exc:  # noqa: BLE001 — boundary
            raise MultimodalParseError(
                "tiff_extract_failed",
                status=500,
                detail="TIFF extraction failed",
            ) from exc

        out = extraction_to_dict(extraction)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "multimodal_parse_complete",
            extra={
                "request_id": request_id,
                "format": "tiff",
                "kind": out.get("kind"),
                "page_count": (
                    max((b.page or 1) for b in layout_blocks)
                    if layout_blocks
                    else 0
                ),
                "duration_ms": duration_ms,
                "dry_run": dry_run,
            },
        )
        return ParseMultimodalResult(
            extraction_dict=out,
            parsed_raw=extraction,
            detected_format="tiff",
            duration_ms=duration_ms,
        )

    raise MultimodalParseError(
        "unsupported_format",
        status=415,
        detail=f"Unsupported document format: {fmt or '(empty)'}",
    )
