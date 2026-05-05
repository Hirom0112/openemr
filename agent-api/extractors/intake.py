"""Production intake-form extractor (Slice 4.5).

Pipeline (W2_ARCHITECTURE.md §5.3 / §7.2):

    pdf_bytes
        -> documents.ocr.extract_layout (deterministic location)
        -> classifier.classify_keywords (fast-path)
        -> (intake_form) Claude vision schema-fill via tool_use
        -> IntakeForm pydantic validation
        -> (otherwise) UnknownDocument fallback (no LLM call)

Mirrors ``extractors.lab.extract`` — same model fallback chain, same
citation contract (§8), same async + PSR-3 logging discipline. Reuses
``ExtractionFailed`` from ``extractors.lab`` so callers handle one failure
class regardless of dispatch.
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
from extractors.lab import ExtractionFailed  # re-export single failure class
from extractors.schemas import (
    Citation,
    IntakeForm,
    KeyFact,
    UnknownDocument,
)

logger = logging.getLogger(__name__)

_MODEL_CANDIDATES: Tuple[str, ...] = (
    "claude-sonnet-4-5-20250929",
    "claude-3-5-sonnet-20241022",
)

_PROMPT = """You are extracting structured intake-form data from a hospital
admission / intake document. You have two inputs:

1. One image per page of the PDF.
2. A JSON layout produced by deterministic OCR. Each block has a `bbox_id`
   (e.g. "p2-b005"), the page number, and the OCR text inside that region.

Your job: fill the IntakeForm schema by calling the `submit_intake_form` tool.

HARD RULES (the agent will reject your output otherwise):

- Use ONLY values you can locate in the OCR layout. Do NOT invent bbox_ids.
- For EVERY filled clinical field, attach a Citation with:
    source_type      = "document"
    source_id        = the document_reference_id passed to you
    page_or_section  = the page number as a string ("1", "2", ...)
    field_or_chunk_id = the bbox_id from the OCR layout (e.g. "p2-b005")
    quote_or_value   = the exact substring from THAT bbox's text that
                       contains the value. Do NOT rephrase.
- Each TextField / MedicationItem / AllergyItem / FamilyHistoryItem /
  CodeStatus must have at least one citation.
- code_status.value must be one of:
    "full_code", "DNR", "DNI", "comfort_care", "POLST", "unknown".
  Map common phrases: "Full Code"->"full_code", "DNR/DNI"->"DNR".
- Omit any optional field you cannot ground in the OCR (do not fabricate).
- Set kind="intake_form", schema_version="1.0".
- Set classifier_confidence to a float in [0,1] reflecting your certainty.
- Set ocr_confidence_range to (min_conf, max_conf) across cited blocks.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


# --------------------------------------------------------------------------- #
# Helpers (mirror lab.py — small surface area, no shared mutable state)
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


def _unknown_key_facts(
    blocks: List[LayoutBlock], document_reference_id: str
) -> List[KeyFact]:
    if not blocks:
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
    """Call Claude with the IntakeForm tool. Returns the tool input dict."""
    tool = {
        "name": "submit_intake_form",
        "description": "Submit the structured IntakeForm extracted from the document.",
        "input_schema": IntakeForm.model_json_schema(),
    }
    last_err: Exception | None = None
    for model in _MODEL_CANDIDATES:
        try:
            t0 = time.monotonic()
            resp = await client.messages.create(
                model=model,
                max_tokens=4096,
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_intake_form"},
                system=_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "extractor_claude_call_ok",
                extra={"model": model, "duration_ms": duration_ms, "tool": "intake"},
            )
            for block in resp.content:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "submit_intake_form"
                ):
                    return dict(block.input)
            last_err = RuntimeError("no tool_use block in response")
            logger.error(
                "extractor_claude_no_tool_use",
                extra={"model": model, "tool": "intake"},
            )
        except anthropic.NotFoundError as e:
            last_err = e
            logger.warning(
                "extractor_claude_model_unavailable",
                extra={"model": model, "tool": "intake"},
            )
            continue
        except Exception as e:  # noqa: BLE001 — boundary, re-wrapped below
            last_err = e
            logger.error(
                "extractor_claude_call_failed",
                extra={"model": model, "tool": "intake", "error_type": type(e).__name__},
            )
            break

    raise ExtractionFailed("vision call failed") from last_err


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def extract_intake(
    pdf_bytes: bytes,
    *,
    patient_id: str,
    document_reference_id: str,
) -> IntakeForm | UnknownDocument:
    """Run the intake extractor and return a validated IntakeForm or UnknownDocument.

    Mirrors ``extractors.lab.extract`` but for intake forms. On any non-intake
    fast-path verdict (or no verdict at all), returns ``UnknownDocument`` —
    no LLM call. Raises ``ExtractionFailed`` only when the intake path fails.
    """
    blocks = extract_layout(pdf_bytes)
    if not blocks:
        logger.error(
            "extractor_no_layout_blocks",
            extra={"document_reference_id": document_reference_id, "tool": "intake"},
        )
        raise ExtractionFailed("vision call failed")

    ocr_range = _ocr_confidence_range(blocks)
    verdict = classify_keywords(blocks)

    # Fallback path: any non-intake outcome → UnknownDocument.
    if verdict is None or verdict.kind != "intake_form":
        classifier_confidence = verdict.confidence if verdict is not None else 0.0
        guess = verdict.kind if verdict is not None else "unknown"
        logger.info(
            "extractor_unknown_fallback",
            extra={
                "document_reference_id": document_reference_id,
                "verdict_kind": guess,
                "classifier_confidence": classifier_confidence,
                "tool": "intake",
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

    # Intake path — Claude vision schema-fill.
    client = anthropic.AsyncAnthropic()
    user_content = _build_user_content(
        pdf_bytes, blocks, patient_id, document_reference_id
    )
    tool_input = await _call_claude_extract(client, user_content)

    try:
        form = IntakeForm.model_validate_json(json.dumps(tool_input))
    except Exception as e:  # noqa: BLE001 — boundary
        logger.error(
            "extractor_validation_failed",
            extra={
                "document_reference_id": document_reference_id,
                "error_type": type(e).__name__,
                "tool": "intake",
            },
        )
        raise ExtractionFailed("vision call failed") from e

    final = form.model_copy(
        update={
            "classifier_confidence": float(verdict.confidence),
            "ocr_confidence_range": ocr_range,
        }
    )
    logger.info(
        "extractor_intake_ok",
        extra={
            "document_reference_id": document_reference_id,
            "n_meds": len(final.current_medications),
            "n_allergies": len(final.allergies),
            "classifier_confidence": final.classifier_confidence,
        },
    )
    return final
