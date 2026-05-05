"""Tests for the production lab extractor (Slice 1.3).

Covers:

- The deterministic keyword fast-path classifier (no I/O).
- The UnknownDocument fallback path — verifies *no* Anthropic call happens.
- The lab happy-path end-to-end against the real Anthropic API (live; skipped
  cleanly when ``ANTHROPIC_API_KEY`` is unset).
- Validation-error path: a malformed tool_use payload must surface as
  ``ExtractionFailed`` with no PHI in the message.
"""

from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure agent-api/ root is importable for direct pytest invocations.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.ocr import LayoutBlock, extract_layout  # noqa: E402
from extractors import (  # noqa: E402
    ClassifierVerdict,
    ExtractionFailed,
    classify_keywords,
    extract,
)
from extractors.schemas import LabReport, UnknownDocument  # noqa: E402

pytestmark = pytest.mark.hard_failure

LAB_FIXTURE = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _block(text: str, idx: int = 0, page: int = 1) -> LayoutBlock:
    return LayoutBlock(
        bbox_id=f"p{page}-b{idx:03d}",
        page=page,
        bbox=(0.0, 0.0, 100.0, 20.0),
        text=text,
        ocr_confidence=1.0,
    )


def _build_pdf(text_lines: list[str]) -> bytes:
    """Minimal text-PDF builder using reportlab — keeps fixtures inline."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 720
    for line in text_lines:
        c.drawString(72, y, line)
        y -= 24
    c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #


def test_keyword_classifier_lab_report_match() -> None:
    layout = [
        _block("Patient: Jane Doe", idx=0),
        _block("LACTATE 4.2 mmol/L", idx=1),
        _block("Reference Range: 0.5 - 2.2", idx=2),
    ]
    verdict = classify_keywords(layout)
    assert verdict is not None
    assert verdict.kind == "lab_report"
    # 2+ matches (LACTATE + REFERENCE RANGE) → 0.95
    assert verdict.confidence == pytest.approx(0.95)
    assert "lab keyword" in verdict.reason


def test_keyword_classifier_intake_form_match() -> None:
    layout = [
        _block("ADMISSION HISTORY", idx=0),
        _block("Patient identifiers redacted", idx=1),
    ]
    verdict = classify_keywords(layout)
    assert verdict is not None
    assert verdict.kind == "intake_form"
    assert verdict.confidence == pytest.approx(0.80)


def test_keyword_classifier_no_match_returns_none() -> None:
    layout = [
        _block("Welcome to the spring newsletter", idx=0),
        _block("This is not a clinical document at all", idx=1),
    ]
    assert classify_keywords(layout) is None


# --------------------------------------------------------------------------- #
# UnknownDocument fallback — no Anthropic call expected.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extract_returns_unknown_for_non_lab() -> None:
    pdf_bytes = _build_pdf(
        ["DISCHARGE SUMMARY", "patient is doing well"]
    )
    # Sanity: layout has no lab/intake keywords.
    blocks = extract_layout(pdf_bytes)
    assert classify_keywords(blocks) is None

    with patch("extractors.lab.anthropic.AsyncAnthropic") as mock_client_cls:
        result = await extract(
            pdf_bytes,
            patient_id="pt-test-001",
            document_reference_id="doc-test-001",
        )
        # The client constructor must never have been invoked on the unknown path.
        mock_client_cls.assert_not_called()

    assert isinstance(result, UnknownDocument)
    assert result.kind == "unknown"
    assert result.summary  # non-empty
    assert result.classifier_confidence == 0.0
    assert result.document_kind_guess == "unknown"

    # Fallback path must still carry bbox/page on its synthetic citation —
    # the frontend highlight overlay relies on these fields being populated.
    assert result.key_facts, "fallback should produce at least one KeyFact"
    cit = result.key_facts[0].citations[0]
    assert cit.bbox is not None and len(cit.bbox) == 4
    assert all(isinstance(v, float) for v in cit.bbox)
    assert isinstance(cit.page, int) and cit.page >= 1


# --------------------------------------------------------------------------- #
# Validation error → ExtractionFailed (no PHI in message).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extract_validation_error_raises_extraction_failed() -> None:
    pdf_bytes = _build_pdf(
        [
            "LABORATORY REPORT",
            "LACTATE 4.2 mmol/L",
            "Reference Range: 0.5 - 2.2",
        ]
    )

    # Build a fake tool_use block whose input is missing the required `kind`.
    bad_tool_use = SimpleNamespace(
        type="tool_use",
        name="submit_lab_report",
        input={
            # intentionally missing "kind"
            "schema_version": "1.0",
            "patient_id": "pt-x",
            "document_reference_id": "doc-x",
            "values": [],
            "classifier_confidence": 0.9,
            "ocr_confidence_range": [1.0, 1.0],
            "extracted_at": "2026-05-04T00:00:00+00:00",
        },
    )
    fake_resp = SimpleNamespace(content=[bad_tool_use])

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_resp)

    with patch(
        "extractors.lab.anthropic.AsyncAnthropic", return_value=fake_client
    ):
        with pytest.raises(ExtractionFailed) as excinfo:
            await extract(
                pdf_bytes,
                patient_id="pt-test-002",
                document_reference_id="doc-test-002",
            )

    msg = str(excinfo.value)
    # Generic message — no PHI / no schema field names from the malformed payload.
    assert msg == "vision call failed"
    assert "pt-test-002" not in msg
    assert "LACTATE" not in msg.upper()


# --------------------------------------------------------------------------- #
# Live happy-path E2E
# --------------------------------------------------------------------------- #


@pytest.mark.live_api
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
@pytest.mark.asyncio
async def test_extract_lab_happy_path_e2e() -> None:
    if not LAB_FIXTURE.exists():
        pytest.skip(f"fixture missing: {LAB_FIXTURE}")

    pdf_bytes = LAB_FIXTURE.read_bytes()
    blocks = extract_layout(pdf_bytes)
    bbox_ids = {b.bbox_id for b in blocks}
    bbox_text = {b.bbox_id: b.text for b in blocks}

    result = await extract(
        pdf_bytes,
        patient_id="pt-live-001",
        document_reference_id="doc-live-001",
    )

    assert isinstance(result, LabReport)
    assert len(result.values) >= 3, f"expected >=3 LabValue, got {len(result.values)}"

    # Locate Lactate.
    lactate = next(
        (
            v
            for v in result.values
            if "lactate" in v.normalized_test_name.lower()
            or "lactate" in v.test_name.lower()
        ),
        None,
    )
    assert lactate is not None, "Lactate not found in extracted lab values"
    assert lactate.value.strip() == "4.2"

    # At least one of lactate's citations must resolve and be a substring.
    norm = lambda s: re.sub(r"\s+", " ", s.lower()).strip()  # noqa: E731
    matched_citation = False
    for c in lactate.citations:
        if c.field_or_chunk_id in bbox_ids:
            block_text = bbox_text[c.field_or_chunk_id]
            if norm(c.quote_or_value) in norm(block_text):
                matched_citation = True
                break
    assert matched_citation, (
        "Lactate must have at least one citation whose field_or_chunk_id "
        "is in the layout AND whose quote_or_value is a substring of that block."
    )

    # Every citation whose bbox_id resolves into the layout must carry a
    # 4-float bbox tuple and a positive page — that's what the UI overlay
    # consumes (no separate layout map is shipped).
    for v in result.values:
        for c in v.citations:
            if c.field_or_chunk_id in bbox_ids:
                assert c.bbox is not None and len(c.bbox) == 4
                assert all(isinstance(coord, float) for coord in c.bbox)
                assert isinstance(c.page, int) and c.page >= 1
