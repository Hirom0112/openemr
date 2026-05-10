"""Phase 1A — blank / unreadable / low-OCR taxonomy regression.

The W2 fixture corpus carries four ``blank_noise`` cases plus a
``missing_data_004_redacted_fields`` case whose ``correct_critic_decision``
rubric was structurally unreachable: the critic returned a generic
``hard_block`` with ``SCHEMA_INVALID`` for every empty/blank/encrypted
extraction, regardless of which decision the fixture expected.

Upstream extractors (e.g. ``extractors.intake._extract_intake_form_prose``
at intake.py:1810) emit a sentinel ``UnknownDocument`` with one
placeholder ``KeyFact`` whose ``text == "(empty document)"`` when no
extractable content was found. ``_detect_blank_unreadable`` distinguishes:

* encrypted / corrupt / all-noise scan -> ``hard_block`` (UNREADABLE_DOCUMENT)
* genuinely blank PDF / blank XLSX / empty DOCX (high OCR conf surface)
  -> ``hard_block`` (EMPTY_DOCUMENT)
* low OCR confidence on the empty surface (single-space PDF, redacted
  fields) -> ``soft_warn`` (OCR_CONFIDENCE_LOW)

The rubric compares only the decision string (rubrics_mechanical.py:713),
not the violation code, so these tests pin the decision and additionally
verify the code so the downstream UI can differentiate.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph.nodes.critic import critic_node
from graph.state import make_initial_state

pytestmark = pytest.mark.hard_failure


def _state(**overrides: Any) -> dict[str, Any]:
    state = make_initial_state(
        request_id="req-blank", session_id="sess-blank", provider_id="prov-blank"
    )
    state.update(overrides)  # type: ignore[arg-type]
    return state  # type: ignore[return-value]


def _sentinel_unknown(
    *,
    document_kind_guess: str,
    summary: str,
    ocr_confidence_range: tuple[float, float] = (1.0, 1.0),
) -> dict[str, Any]:
    """Build the kind of synthetic UnknownDocument extractors emit on empty input."""
    return {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/empty",
        "document_kind_guess": document_kind_guess,
        "summary": summary,
        "key_facts": [
            {
                "text": "(empty document)",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/empty",
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-empty",
                        "quote_or_value": "(empty)",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.0,
        "ocr_confidence_range": list(ocr_confidence_range),
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


# ── Hard-block cases ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blank_pdf_hard_blocks_with_empty_document() -> None:
    """blank_noise_001_blank_pdf — blank PDF, high OCR conf -> hard_block."""
    extraction = _sentinel_unknown(
        document_kind_guess="pdf_blank",
        summary="PDF contained no extractable text.",
    )
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "hard_block"
    assert "EMPTY_DOCUMENT" in out["critic_violations"]


@pytest.mark.asyncio
async def test_encrypted_pdf_hard_blocks_with_unreadable_document() -> None:
    """blank_noise_002_encrypted_pdf — encrypted stub -> hard_block."""
    extraction = _sentinel_unknown(
        document_kind_guess="encrypted",
        summary="PDF stream reports ENCRYPTED — no decryptable content.",
    )
    state = _state(extraction=extraction, doc_type_hint="lab_report")
    out = await critic_node(state)
    assert out["critic_decision"] == "hard_block"
    assert "UNREADABLE_DOCUMENT" in out["critic_violations"]


@pytest.mark.asyncio
async def test_all_noise_scan_hard_blocks_with_unreadable_document() -> None:
    """blank_noise_004_all_noise_scan — pure noise -> hard_block."""
    extraction = _sentinel_unknown(
        document_kind_guess="all_noise_scan",
        summary="Scan looks like pure noise; no text recovered.",
    )
    out = await critic_node(_state(extraction=extraction, doc_type_hint="lab_report"))
    assert out["critic_decision"] == "hard_block"
    assert "UNREADABLE_DOCUMENT" in out["critic_violations"]


@pytest.mark.asyncio
async def test_blank_xlsx_hard_blocks_with_empty_document() -> None:
    """xlsx_blank_008_empty_workbook analog — XLSX headers only -> hard_block."""
    extraction = _sentinel_unknown(
        document_kind_guess="xlsx_empty",
        summary="Workbook contained sheet headers but no data rows.",
    )
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "hard_block"
    assert "EMPTY_DOCUMENT" in out["critic_violations"]


# ── Soft-warn cases (low OCR signal on empty surface) ───────────────────────


@pytest.mark.asyncio
async def test_empty_stream_soft_warns_with_ocr_confidence_low() -> None:
    """blank_noise_003_empty_stream — single-space PDF, low OCR -> soft_warn."""
    extraction = _sentinel_unknown(
        document_kind_guess="empty_stream",
        summary="PDF stream contained a single space character.",
        ocr_confidence_range=(0.3, 0.4),
    )
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "soft_warn"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "OCR_CONFIDENCE_LOW" in codes


@pytest.mark.asyncio
async def test_redacted_fields_soft_warns_with_ocr_confidence_low() -> None:
    """missing_data_004_redacted_fields — redacted intake -> soft_warn."""
    extraction = _sentinel_unknown(
        document_kind_guess="empty_stream",
        summary="Most fields appear redacted/blacked out.",
        ocr_confidence_range=(0.4, 0.55),
    )
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "soft_warn"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "OCR_CONFIDENCE_LOW" in codes


# ── Negative cases — must NOT trip on real extractions ──────────────────────


@pytest.mark.asyncio
async def test_real_unknown_with_real_keyfact_does_not_trip_taxonomy() -> None:
    """A genuine UnknownDocument with real key_facts must pass through."""
    extraction = {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/real",
        "document_kind_guess": "consultant note",
        "summary": "Cardiology consult letter discussing recent MI.",
        "key_facts": [
            {
                "text": "Cardiology consult letter",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/real",
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-b001",
                        "quote_or_value": "Cardiology consult letter",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.7,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }
    layout = [
        {
            "bbox_id": "p1-b001",
            "page": 1,
            "bbox": [0.0, 0.0, 100.0, 20.0],
            "text": "Cardiology consult letter discussing recent MI.",
            "ocr_confidence": 0.95,
        }
    ]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    assert out["critic_decision"] == "pass"
    assert "EMPTY_DOCUMENT" not in out["critic_violations"]
    assert "UNREADABLE_DOCUMENT" not in out["critic_violations"]


# ── Real eval-cache shapes (extractor leaves `document_kind_guess="unknown"`) ──
#
# The cached eval outcomes for blank_noise_002_encrypted_pdf and
# blank_noise_004_all_noise_scan show the production extractor does NOT tag
# the document_kind_guess as "encrypted" / "all_noise_scan" — it leaves
# guess="unknown" and surfaces the boilerplate / gibberish in summary +
# key_fact text. The taxonomy detector must classify those shapes too.


def _real_extractor_unknown(summary: str, key_fact_text: str) -> dict[str, Any]:
    return {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/real-shape",
        "document_kind_guess": "unknown",  # extractor does not tag the kind
        "summary": summary,
        "key_facts": [
            {
                "text": key_fact_text,
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/real-shape",
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-b000",
                        "quote_or_value": key_fact_text[:80],
                    }
                ],
            }
        ],
        "classifier_confidence": 0.0,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_encrypted_pdf_real_shape_hard_blocks() -> None:
    """Mirror cached blank_noise_002 — guess='unknown', 'ENCRYPTED' in key_fact text."""
    extraction = _real_extractor_unknown(
        summary="Unclassified clinical document; first text regions: ENCRYPTED | This document is",
        key_fact_text="ENCRYPTED",
    )
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "hard_block"
    assert "UNREADABLE_DOCUMENT" in out["critic_violations"]


@pytest.mark.asyncio
async def test_all_noise_scan_real_shape_hard_blocks() -> None:
    """Mirror cached blank_noise_004 — guess='unknown', gibberish in key_fact."""
    extraction = _real_extractor_unknown(
        summary="Unclassified clinical document; first text regions: j8#@.. ::; ,, --- '' ?? @@ %",
        key_fact_text="j8#@.. ::; ,, --- '' ?? @@ %% && ()() //// ;;; ..--..--.. qq pp ll oo ee rr tt y",
    )
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "hard_block"
    assert "UNREADABLE_DOCUMENT" in out["critic_violations"]


@pytest.mark.asyncio
async def test_password_protected_real_shape_hard_blocks() -> None:
    """Variant — extractor surfaces 'password protected' boilerplate verbatim."""
    extraction = _real_extractor_unknown(
        summary="Unclassified clinical document; first text regions: This document is password protected.",
        key_fact_text="This document is password protected. Please contact the sender.",
    )
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "hard_block"
    assert "UNREADABLE_DOCUMENT" in out["critic_violations"]


@pytest.mark.asyncio
async def test_real_unknown_short_uppercase_word_does_not_falsely_trip() -> None:
    """A genuine short clinical fragment must NOT trip gibberish detection."""
    extraction = _real_extractor_unknown(
        summary="Unclassified clinical document; first text regions: ECG SHOWS NSR.",
        key_fact_text="ECG SHOWS NSR.",
    )
    # Has high alpha ratio (mostly letters); should NOT be flagged.
    # The single-word/short-text guard in _is_gibberish prevents false
    # positives here. Decision may still escalate via other paths, but
    # UNREADABLE_DOCUMENT must not be the cause.
    out = await critic_node(_state(extraction=extraction))
    assert "UNREADABLE_DOCUMENT" not in out["critic_violations"]
