"""Production lab extractor (Slice 1.3).

Pipeline (W2_ARCHITECTURE.md §5.3):

    pdf_bytes
        -> documents.ocr.extract_layout (deterministic location)
        -> classifier.classify_keywords (fast-path)
        -> (lab_report) Claude vision schema-fill via tool_use
        -> LabReport pydantic validation
        -> (otherwise) UnknownDocument fallback (no LLM call this slice)

Async (FastAPI-friendly) via ``anthropic.AsyncAnthropic``.  PSR-3 logging
only — never log prompt or completion text (W2_ARCHITECTURE §9.2).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, List, Tuple

import anthropic
import pymupdf

from documents.ocr import LayoutBlock, extract_layout
from extractors.classifier import classify_keywords
from extractors.schemas import (
    Citation,
    ExtractionResult,
    KeyFact,
    LabReport,
    UnknownDocument,
)

logger = logging.getLogger(__name__)

# Model preferences with fallbacks per task brief / spike.
_MODEL_CANDIDATES: Tuple[str, ...] = (
    "claude-sonnet-4-5-20250929",
    "claude-3-5-sonnet-20241022",
)

_PROMPT = """You are extracting structured lab data from an outside-hospital
laboratory report. You have two inputs:

1. One image per page of the PDF.
2. A JSON layout produced by deterministic OCR. Each block has a `bbox_id`
   (e.g. "p2-b005"), the page number, and the OCR text inside that region.

Your job: fill the LabReport schema by calling the `submit_lab_report` tool.

HARD RULES (the agent will reject your output otherwise):

- Use ONLY values you can locate in the OCR layout. Do NOT invent bbox_ids.
- For EVERY filled clinical field, attach a Citation with:
    source_type      = "document"
    source_id        = the document_reference_id passed to you
    page_or_section  = the page number as a string ("1", "2", ...)
    field_or_chunk_id = the bbox_id from the OCR layout (e.g. "p2-b005")
    quote_or_value   = the exact substring from THAT bbox's text that
                       contains the value. Do NOT rephrase.
- Each LabValue.citations must have at least one citation.
- For abnormal_flag, map: "HH"->"critical_high", "LL"->"critical_low",
  "H"->"high", "L"->"low", blank->"normal".
- normalized_test_name: lowercase test name (e.g. "lactate", "wbc",
  "creatinine", "sodium").
- Set kind="lab_report", schema_version="1.0".
- Set classifier_confidence to a float in [0,1] reflecting your certainty.
- Set ocr_confidence_range to (min_conf, max_conf) across cited blocks.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


class ExtractionFailed(Exception):
    """Generic extractor failure — never carries upstream exception text.

    The original cause is preserved via ``__cause__`` for log correlation but
    must NOT be surfaced to API clients (W2_ARCHITECTURE §9.2: no PHI / no
    raw vendor errors leaking upstream).
    """


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _render_pages_to_png(pdf_bytes: bytes) -> List[bytes]:
    pngs: List[bytes] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
            pngs.append(pix.tobytes("png"))
    return pngs


def _layout_to_prompt_json(blocks: List[LayoutBlock]) -> str:
    return json.dumps(
        [
            {
                "bbox_id": b.bbox_id,
                "page": b.page,
                "text": b.text,
                "ocr_confidence": b.ocr_confidence,
            }
            for b in blocks
        ],
        ensure_ascii=False,
    )


def _build_user_content(
    pdf_bytes: bytes,
    blocks: List[LayoutBlock],
    patient_id: str,
    document_reference_id: str,
) -> List[dict[str, Any]]:
    pngs = _render_pages_to_png(pdf_bytes)
    content: List[dict[str, Any]] = []
    for png in pngs:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": (
                f"patient_id = {patient_id}\n"
                f"document_reference_id = {document_reference_id}\n"
                f"current_utc = {datetime.now(timezone.utc).isoformat()}\n\n"
                "OCR layout JSON (the only source of truth for bbox_ids):\n"
                f"{_layout_to_prompt_json(blocks)}"
            ),
        }
    )
    return content


def _ocr_confidence_range(blocks: List[LayoutBlock]) -> Tuple[float, float]:
    if not blocks:
        return (0.0, 0.0)
    confs = [b.ocr_confidence for b in blocks]
    return (min(confs), max(confs))


def _unknown_summary(blocks: List[LayoutBlock]) -> str:
    """Build a 1-sentence permissive summary from the first few blocks.

    Pure text concatenation — no LLM call.  Phase 3 replaces this with the
    LLM classifier path; for this slice we just need a non-empty string so
    the schema validates.
    """
    snippets: List[str] = []
    for b in blocks[:3]:
        text = " ".join(b.text.split())
        if text:
            snippets.append(text)
        if len(snippets) == 3:
            break
    if not snippets:
        return "Document has no extractable text."
    joined = " | ".join(snippets)
    if len(joined) > 240:
        joined = joined[:237] + "..."
    return f"Unclassified clinical document; first text regions: {joined}"


def _index_blocks(blocks: List[LayoutBlock]) -> dict[str, LayoutBlock]:
    """Build a bbox_id -> LayoutBlock map for O(1) citation hydration."""
    return {b.bbox_id: b for b in blocks}


def _hydrate_citation(cit: Citation, block_index: dict[str, LayoutBlock]) -> Citation:
    """Return a copy of ``cit`` with ``bbox`` / ``page`` populated from the
    OCR layout block keyed by ``cit.field_or_chunk_id``.

    If the lookup fails, log a structured warning and return the citation
    untouched — never invent coordinates (W2_ARCHITECTURE §8 fidelity).
    """
    block = block_index.get(cit.field_or_chunk_id)
    if block is None:
        logger.warning(
            "extractor_citation_bbox_lookup_failed",
            extra={
                "field_or_chunk_id": cit.field_or_chunk_id,
                "source_id": cit.source_id,
            },
        )
        return cit
    return cit.model_copy(update={"bbox": block.bbox, "page": block.page})


def _hydrate_lab_report_citations(
    report: LabReport, blocks: List[LayoutBlock]
) -> LabReport:
    """Walk every LabValue.citations and stamp bbox/page from the layout."""
    block_index = _index_blocks(blocks)
    new_values = [
        v.model_copy(
            update={
                "citations": [_hydrate_citation(c, block_index) for c in v.citations]
            }
        )
        for v in report.values
    ]
    return report.model_copy(update={"values": new_values})


def _unknown_key_facts(blocks: List[LayoutBlock], document_reference_id: str) -> List[KeyFact]:
    """Build at least one KeyFact (the schema requires non-empty key_facts).

    We cite the first non-empty layout block. This keeps the citation chain
    intact even on the fallback path.
    """
    if not blocks:
        # No blocks = no citations = we cannot construct a KeyFact, but the
        # caller short-circuits before this; defensive return.
        return []
    first = blocks[0]
    snippet = " ".join(first.text.split())
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return [
        KeyFact(
            text=snippet or "(empty block)",
            citations=[
                Citation(
                    source_type="document",
                    source_id=document_reference_id,
                    page_or_section=str(first.page),
                    field_or_chunk_id=first.bbox_id,
                    quote_or_value=first.text,
                    bbox=first.bbox,
                    page=first.page,
                )
            ],
        )
    ]


# --------------------------------------------------------------------------- #
# Claude vision call (async)
# --------------------------------------------------------------------------- #


async def _call_claude_extract(
    client: anthropic.AsyncAnthropic,
    user_content: List[dict[str, Any]],
) -> dict[str, Any]:
    """Call Claude with the LabReport tool. Returns the tool input dict.

    Tries ``_MODEL_CANDIDATES`` in order until one succeeds.  Raises
    ``ExtractionFailed`` if every model is unavailable or no tool_use block
    is returned.  Never logs prompt or completion text.
    """
    tool = {
        "name": "submit_lab_report",
        "description": "Submit the structured LabReport extracted from the document.",
        "input_schema": LabReport.model_json_schema(),
    }
    last_err: Exception | None = None
    for model in _MODEL_CANDIDATES:
        try:
            t0 = time.monotonic()
            resp = await client.messages.create(
                model=model,
                max_tokens=4096,
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_lab_report"},
                system=_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "extractor_claude_call_ok",
                extra={"model": model, "duration_ms": duration_ms},
            )
            for block in resp.content:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "submit_lab_report"
                ):
                    return dict(block.input)
            last_err = RuntimeError("no tool_use block in response")
            logger.error(
                "extractor_claude_no_tool_use",
                extra={"model": model},
            )
        except anthropic.NotFoundError as e:
            last_err = e
            logger.warning(
                "extractor_claude_model_unavailable",
                extra={"model": model},
            )
            continue
        except Exception as e:  # noqa: BLE001 — boundary, re-wrapped below
            last_err = e
            logger.error(
                "extractor_claude_call_failed",
                extra={"model": model, "error_type": type(e).__name__},
            )
            # Don't try further candidates for non-NotFound errors.
            break

    raise ExtractionFailed("vision call failed") from last_err


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def extract(
    pdf_bytes: bytes,
    *,
    patient_id: str,
    document_reference_id: str,
) -> ExtractionResult:
    """Run the production extractor and return a validated ExtractionResult.

    On the lab path, returns a ``LabReport``.  On any non-lab fast-path
    verdict (or no verdict at all), returns an ``UnknownDocument`` with a
    permissive 1-sentence summary — no LLM call (Phase 3 deferred).

    Raises ``ExtractionFailed`` only when the lab path fails (Claude
    unavailable / non-recoverable error / schema validation error).  Never
    surfaces the underlying exception message.
    """
    blocks = extract_layout(pdf_bytes)
    if not blocks:
        logger.error(
            "extractor_no_layout_blocks",
            extra={"document_reference_id": document_reference_id},
        )
        raise ExtractionFailed("vision call failed")

    ocr_range = _ocr_confidence_range(blocks)
    verdict = classify_keywords(blocks)

    # Fallback path: any non-lab outcome → UnknownDocument.
    if verdict is None or verdict.kind != "lab_report":
        classifier_confidence = verdict.confidence if verdict is not None else 0.0
        guess = verdict.kind if verdict is not None else "unknown"
        logger.info(
            "extractor_unknown_fallback",
            extra={
                "document_reference_id": document_reference_id,
                "verdict_kind": guess,
                "classifier_confidence": classifier_confidence,
            },
        )
        return UnknownDocument(
            kind="unknown",
            schema_version="1.0",
            patient_id=patient_id,
            document_reference_id=document_reference_id,
            document_kind_guess=guess,
            summary=_unknown_summary(blocks),
            key_facts=_unknown_key_facts(blocks, document_reference_id),
            classifier_confidence=classifier_confidence,
            ocr_confidence_range=ocr_range,
            extracted_at=datetime.now(timezone.utc),
        )

    # Lab path — Claude vision schema-fill.
    client = anthropic.AsyncAnthropic()
    user_content = _build_user_content(
        pdf_bytes, blocks, patient_id, document_reference_id
    )
    tool_input = await _call_claude_extract(client, user_content)

    try:
        # Round-trip through JSON so strict mode accepts ISO datetime / list-as-tuple.
        report = LabReport.model_validate_json(json.dumps(tool_input))
    except Exception as e:  # noqa: BLE001 — boundary
        logger.error(
            "extractor_validation_failed",
            extra={
                "document_reference_id": document_reference_id,
                "error_type": type(e).__name__,
            },
        )
        raise ExtractionFailed("vision call failed") from e

    # Stamp computed fields (overrides whatever the model reported) and
    # hydrate every citation with bbox/page from the OCR layout so the UI
    # can highlight cited regions without a separate layout map.
    final = _hydrate_lab_report_citations(
        report.model_copy(
            update={
                "classifier_confidence": float(verdict.confidence),
                "ocr_confidence_range": ocr_range,
            }
        ),
        blocks,
    )
    logger.info(
        "extractor_lab_ok",
        extra={
            "document_reference_id": document_reference_id,
            "n_values": len(final.values),
            "classifier_confidence": final.classifier_confidence,
        },
    )
    return final
