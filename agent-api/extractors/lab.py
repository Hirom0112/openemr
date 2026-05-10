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
from extractors.prompt_registry import get_prompt
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

# Wave 2D: prompt sourced from the per-class registry.
_PROMPT = get_prompt("lab_report_tabular")


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
    # Wave 2B: propagate the LayoutBlock's polygon (if any) onto the
    # citation. ``LayoutBlock.polygon`` is a tuple-of-tuples; convert to
    # a list-of-tuples so the pydantic schema (which uses List) is happy.
    poly = getattr(block, "polygon", None)
    polygon_list = [tuple(p) for p in poly] if poly else None
    return cit.model_copy(
        update={
            "bbox": block.bbox,
            "page": block.page,
            "polygon": polygon_list,
        }
    )


def _hydrate_lab_report_citations(
    report: LabReport, blocks: List[LayoutBlock]
) -> LabReport:
    """Walk every LabValue.citations AND every demographics sub-field's
    citations, stamping bbox/page from the layout. Without demographics
    hydration the bbox layer can't draw a highlight when the user clicks
    a Name/DOB/MRN card on a lab-report review panel — the LLM's
    field_or_chunk_id is set but bbox/page stay None.
    """
    block_index = _index_blocks(blocks)
    new_values = [
        v.model_copy(
            update={
                "citations": [_hydrate_citation(c, block_index) for c in v.citations]
            }
        )
        for v in report.values
    ]
    update: dict = {"values": new_values}
    demo = getattr(report, "patient_demographics", None)
    if demo is not None:
        new_demo_fields: dict = {}
        for sub in ("name", "dob", "sex", "mrn", "address"):
            tf = getattr(demo, sub, None)
            if tf is None:
                continue
            new_demo_fields[sub] = tf.model_copy(
                update={
                    "citations": [
                        _hydrate_citation(c, block_index) for c in tf.citations
                    ]
                }
            )
        if new_demo_fields:
            update["patient_demographics"] = demo.model_copy(update=new_demo_fields)
    return report.model_copy(update=update)


_IMAGING_RE = __import__("re").compile(
    r"\b(IMAGING|RADIOLOGY|MRI|CT SCAN|X[\-\s]?RAY|ULTRASOUND|"
    r"IMPRESSION:|FINDINGS:|TECHNIQUE:|RADIOLOGIST|"
    r"CONTRAST|DICOM|CHEST X|ABDOMEN.*PELVIS|"
    r"NORMAL EXAM|UNREMARKABLE)\b",
    __import__("re").IGNORECASE,
)


def _has_imaging_keywords(blocks: List[LayoutBlock]) -> bool:
    """True when the document carries radiology/imaging language.

    Used by the lab-extractor fallback path to distinguish imaging-report
    fixtures (where the keyword classifier returns no lab/intake verdict)
    from synthetic bbox_gt fixtures (also no verdict, but no medical
    text either). Letting the critic differentiate ``document_kind_guess``
    avoids both: (a) firing wrong_type_hint on bbox_gt synthetics, and
    (b) silently swallowing wrong_type_hint on real imaging fixtures.
    """
    for b in blocks:
        if _IMAGING_RE.search(b.text):
            return True
    return False


def _classify_no_blocks(pdf_bytes: bytes) -> Tuple[str, Tuple[float, float]]:
    """Classify a PDF that yielded zero layout blocks.

    Returns ``(document_kind_guess, ocr_confidence_range)`` shaped so the
    critic's ``_detect_blank_unreadable`` lands on the right decision:

    * ``("pdf_blank", (1.0, 1.0))`` — pymupdf opens the bytes and finds zero
      pages, or all pages return empty text + zero images. The document is
      structurally empty; no OCR was actually attempted on content, so we
      report high confidence that there is nothing to read. Critic routes
      to ``EMPTY_DOCUMENT`` hard_block (matches ``_BLANK_GUESS_TOKENS``).
    * ``("low_quality_scan", (0.1, 0.3))`` — pymupdf opens with pages that
      contain text or images yet ``extract_layout`` produced nothing. The
      document had bytes but the OCR/layout pipeline could not recover them
      (degraded scan, redacted fields). Critic routes to
      ``OCR_CONFIDENCE_LOW`` soft_warn (range[0] < 0.6).
    * ``("corrupt", (0.0, 0.0))`` — pymupdf raises on open. Document bytes
      are present but unreadable. Critic routes to ``UNREADABLE_DOCUMENT``
      hard_block (matches ``_UNREADABLE_GUESS_TOKENS``).
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 — boundary; pymupdf raises generic
        return "corrupt", (0.0, 0.0)
    try:
        page_count = doc.page_count
        if page_count == 0:
            return "pdf_blank", (1.0, 1.0)
        total_text_len = 0
        has_images = False
        for page in doc:
            total_text_len += len(page.get_text() or "")
            if page.get_images():
                has_images = True
        if total_text_len == 0 and not has_images:
            return "pdf_blank", (1.0, 1.0)
        return "low_quality_scan", (0.1, 0.3)
    finally:
        doc.close()


def _empty_document_sentinel(
    pdf_bytes: bytes,
    *,
    patient_id: str,
    document_reference_id: str,
) -> UnknownDocument:
    """Emit a sentinel UnknownDocument when no layout blocks were recovered.

    Replaces the prior ``raise ExtractionFailed`` path so the critic gets a
    chance to classify *why* extraction yielded nothing (truly blank vs.
    degraded scan vs. corrupt bytes) and emit the right decision, rather
    than the eval boundary catching the exception and silently nulling the
    extraction. Schema-conformant: ``key_facts`` carries a single sentinel
    placeholder so ``UnknownDocument`` validates and the critic's
    ``_is_sentinel_unknown`` recognises the shape.
    """
    guess, ocr_range = _classify_no_blocks(pdf_bytes)
    sentinel_citation = Citation(
        source_type="document",
        source_id=document_reference_id,
        page_or_section=None,
        field_or_chunk_id="empty",
        quote_or_value="(no extractable content)",
    )
    sentinel_keyfact = KeyFact(
        text="(empty document)",
        citations=[sentinel_citation],
    )
    return UnknownDocument(
        kind="unknown",
        schema_version="1.0",
        patient_id=patient_id,
        document_reference_id=document_reference_id,
        document_kind_guess=guess,
        summary="Document yielded no layout blocks during extraction.",
        key_facts=[sentinel_keyfact],
        classifier_confidence=0.0,
        ocr_confidence_range=ocr_range,
        extracted_at=datetime.now(timezone.utc),
    )


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
                temperature=0,
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
        logger.warning(
            "extractor_no_layout_blocks_emitting_sentinel",
            extra={"document_reference_id": document_reference_id},
        )
        return _empty_document_sentinel(
            pdf_bytes,
            patient_id=patient_id,
            document_reference_id=document_reference_id,
        )

    ocr_range = _ocr_confidence_range(blocks)
    verdict = classify_keywords(blocks)

    # Fallback path: any non-lab outcome → UnknownDocument.
    if verdict is None or verdict.kind != "lab_report":
        classifier_confidence = verdict.confidence if verdict is not None else 0.0
        guess = verdict.kind if verdict is not None else "unknown"
        # Imaging-report fallback: when neither lab nor intake keywords
        # fire but radiology/imaging keywords do, label the guess so the
        # critic's wrong-type-hint detector can recognize the disagreement
        # (hint=lab_report on an imaging report → soft_warn). Without this,
        # imaging_report fixtures land at guess="unknown" and are
        # indistinguishable from synthetic bbox_gt fixtures, which the
        # detector intentionally skips.
        if guess == "unknown" and _has_imaging_keywords(blocks):
            guess = "imaging_report"
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
