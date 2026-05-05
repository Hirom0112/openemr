"""Tests for documents/ocr.py — Slice 1.1.

Verifies layout extraction, deterministic bbox IDs, and confidence semantics.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

# Ensure agent-api/ root is importable.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.ocr import LayoutBlock, document_confidence, extract_layout  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"
BBOX_ID_RE = re.compile(r"^p\d+-b\d{3}$")

pytestmark = pytest.mark.hard_failure


def _read_fixture() -> bytes:
    if not FIXTURE.exists():
        pytest.skip(f"fixture missing: {FIXTURE}")
    return FIXTURE.read_bytes()


def test_layout_extracts_lactate_bbox() -> None:
    pdf_bytes = _read_fixture()
    blocks = extract_layout(pdf_bytes)
    assert blocks, "expected at least one layout block"

    # Lactate value 4.2 must appear in some block on page 2.
    assert any("4.2" in b.text for b in blocks if b.page == 2), (
        "expected '4.2' (lactate value) in a page-2 block; "
        f"got texts={[b.text for b in blocks if b.page == 2]}"
    )

    # Every bbox_id matches the spec format and they're all unique.
    ids = [b.bbox_id for b in blocks]
    assert len(ids) == len(set(ids)), "bbox_ids must be unique"
    for bid in ids:
        assert BBOX_ID_RE.match(bid), f"bad bbox_id format: {bid}"

    # Determinism: a second extraction yields identical output.
    blocks2 = extract_layout(pdf_bytes)
    assert [b.bbox_id for b in blocks2] == ids
    assert [b.text for b in blocks2] == [b.text for b in blocks]


def test_document_confidence_text_pdf_is_one() -> None:
    """Synthetic text-PDFs should report document confidence 1.0."""
    pdf_bytes = _read_fixture()
    blocks = extract_layout(pdf_bytes)
    conf = document_confidence(blocks)
    assert conf == pytest.approx(1.0)


def test_document_confidence_empty_is_zero() -> None:
    assert document_confidence([]) == 0.0


def test_extract_layout_accepts_png() -> None:
    """``extract_layout`` should auto-detect PNG bytes and return at least
    one LayoutBlock. Without Tesseract installed, the fallback emits a
    single page-level block with confidence 0.0 — which is exactly what the
    critic's degradation path consumes (W2_ARCHITECTURE §8.7).
    """
    png_path = (
        ROOT
        / "tests"
        / "fixtures"
        / "eval"
        / "real-examples"
        / "p03-reyes-intake.png"
    )
    if not png_path.exists():
        pytest.skip(f"png fixture missing: {png_path}")

    png_bytes = png_path.read_bytes()
    assert png_bytes.startswith(b"\x89PNG"), "fixture is not a PNG"

    blocks = extract_layout(png_bytes)
    assert blocks, "expected at least one block from PNG"
    assert all(isinstance(b, LayoutBlock) for b in blocks)
    # Single-page synthetic doc.
    assert all(b.page == 1 for b in blocks)
    # bbox_id format invariant holds for image inputs too.
    for b in blocks:
        assert BBOX_ID_RE.match(b.bbox_id), f"bad bbox_id: {b.bbox_id}"
    # Confidence is bounded; the no-tesseract fallback path puts it at 0.0.
    for b in blocks:
        assert 0.0 <= b.ocr_confidence <= 1.0
    # Document-level confidence stays in [0, 1].
    conf = document_confidence(blocks)
    assert 0.0 <= conf <= 1.0
