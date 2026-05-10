"""Regression tests for the no-layout-blocks sentinel path in lab extractor.

When ``extract_layout`` returns no blocks (truly blank PDF, degraded scan
yielding nothing, or corrupt bytes), the extractor previously raised
``ExtractionFailed`` and the eval boundary silently set extraction=None.
That hid the failure mode from the critic.

The sentinel path now returns a schema-conformant ``UnknownDocument`` whose
``document_kind_guess`` and ``ocr_confidence_range`` route the critic to:

* ``EMPTY_DOCUMENT`` hard_block — truly blank PDF (no pages or all empty)
* ``OCR_CONFIDENCE_LOW`` soft_warn — pages have content but layout failed
* ``UNREADABLE_DOCUMENT`` hard_block — pymupdf cannot open the bytes
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extractors.lab import _classify_no_blocks, _empty_document_sentinel, extract  # noqa: E402
from extractors.schemas import UnknownDocument  # noqa: E402


def _build_blank_pdf_bytes() -> bytes:
    """Build a minimal valid PDF with one blank page."""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()  # blank page, no text/images
    out = doc.tobytes()
    doc.close()
    return out


def _build_text_pdf_bytes() -> bytes:
    """Build a PDF with one page containing text — represents 'has content
    but extract_layout produced nothing' (degraded scan analogue)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Some content here.")
    out = doc.tobytes()
    doc.close()
    return out


@pytest.mark.hard_failure
def test_classify_blank_pdf_returns_high_confidence_empty() -> None:
    pdf = _build_blank_pdf_bytes()
    guess, ocr_range = _classify_no_blocks(pdf)
    assert guess == "pdf_blank"
    assert ocr_range == (1.0, 1.0)


@pytest.mark.hard_failure
def test_classify_text_pdf_returns_low_quality_low_confidence() -> None:
    pdf = _build_text_pdf_bytes()
    guess, ocr_range = _classify_no_blocks(pdf)
    assert guess == "low_quality_scan"
    assert ocr_range[0] < 0.6


@pytest.mark.hard_failure
def test_classify_corrupt_bytes_returns_unreadable() -> None:
    guess, ocr_range = _classify_no_blocks(b"not a pdf at all")
    assert guess == "corrupt"
    assert ocr_range == (0.0, 0.0)


@pytest.mark.hard_failure
def test_sentinel_validates_against_unknown_document_schema() -> None:
    pdf = _build_blank_pdf_bytes()
    sentinel = _empty_document_sentinel(
        pdf, patient_id="pt-1", document_reference_id="doc-1"
    )
    assert isinstance(sentinel, UnknownDocument)
    assert sentinel.kind == "unknown"
    assert len(sentinel.key_facts) == 1
    assert sentinel.key_facts[0].text == "(empty document)"
    assert len(sentinel.key_facts[0].citations) == 1
    assert sentinel.document_kind_guess == "pdf_blank"


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_extract_no_blocks_returns_sentinel_not_raises() -> None:
    pdf = _build_blank_pdf_bytes()
    with patch("extractors.lab.extract_layout", return_value=[]):
        result = await extract(
            pdf, patient_id="pt-1", document_reference_id="doc-1"
        )
    assert isinstance(result, UnknownDocument)
    assert result.document_kind_guess == "pdf_blank"
    assert result.ocr_confidence_range == (1.0, 1.0)
    assert result.key_facts[0].text == "(empty document)"


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_extract_no_blocks_low_quality_routes_to_soft_warn_signal() -> None:
    pdf = _build_text_pdf_bytes()
    with patch("extractors.lab.extract_layout", return_value=[]):
        result = await extract(
            pdf, patient_id="pt-1", document_reference_id="doc-2"
        )
    assert result.document_kind_guess == "low_quality_scan"
    assert result.ocr_confidence_range[0] < 0.6
