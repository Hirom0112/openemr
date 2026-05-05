"""Phase-3 polygon-field contract tests.

LayoutBlock gained an Optional ``polygon`` field that preserves the source
4-point quad emitted by detectors that have one (paddle). Engines without
polygon source data (tesseract, PDF text-layer extraction) must leave it
None — fabricating a polygon would defeat the whole point of preserving
detector-native shape for IoU consumers in Phase-3.

These tests are isolated from paddlepaddle/paddleocr: we exercise the
PaddleEngine.extract_image method by stubbing the lazy-loaded ``ocr``
attribute on an instance so we never import the real wheels.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.hard_failure

# tests/ live next to the package roots; make them importable.
_AGENT_API = Path(__file__).resolve().parents[1]
if str(_AGENT_API) not in sys.path:
    sys.path.insert(0, str(_AGENT_API))

from documents.ocr import (  # noqa: E402
    BlockGranularity,
    LayoutBlock,
    _tesseract_extract_image,
)
from documents.ocr_paddle import PaddleEngine  # noqa: E402


# A 1x1 PNG (8-bit RGB, white). Used solely to satisfy PyMuPDF's image
# decoder so the paddle adapter can run; the stubbed ocr() return value is
# what actually drives the test.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDAT\x08\x99c\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3X^\xe2"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _StubPaddleOCR:
    """Mimics paddleocr.PaddleOCR with a deterministic return."""

    def __init__(self, payload: List[Any]) -> None:
        self._payload = payload

    def ocr(self, _arr: Any, cls: bool = True) -> List[Any]:  # noqa: ARG002
        return self._payload


def _make_paddle(payload: List[Any]) -> PaddleEngine:
    eng = PaddleEngine()
    eng._ocr = _StubPaddleOCR(payload)  # type: ignore[attr-defined]
    return eng


def test_paddle_line_block_carries_source_polygon() -> None:
    """A paddle line block keeps the detector's 4-point quad verbatim."""
    quad = [[10.0, 20.0], [110.0, 22.0], [112.0, 50.0], [12.0, 48.0]]
    raw = [[[quad, ("hello world", 0.95)]]]
    blocks = _make_paddle(raw).extract_image(_TINY_PNG, filetype="png")

    line_blocks = [b for b in blocks if b.granularity == BlockGranularity.LINE]
    assert len(line_blocks) == 1
    line = line_blocks[0]

    assert line.polygon is not None
    assert len(line.polygon) == 4
    # Same coords as input, normalised to tuples-of-floats.
    assert line.polygon == (
        (10.0, 20.0),
        (110.0, 22.0),
        (112.0, 50.0),
        (12.0, 48.0),
    )


def test_paddle_word_blocks_have_no_fabricated_polygon() -> None:
    """Word-level paddle blocks are positional approximations — they MUST
    leave polygon=None rather than invent one from the line quad."""
    quad = [[10.0, 20.0], [200.0, 20.0], [200.0, 50.0], [10.0, 50.0]]
    raw = [[[quad, ("foo bar baz", 0.9)]]]
    blocks = _make_paddle(raw).extract_image(_TINY_PNG, filetype="png")

    word_blocks = [b for b in blocks if b.granularity == BlockGranularity.WORD]
    assert len(word_blocks) >= 1
    for w in word_blocks:
        assert w.polygon is None, (
            f"word block {w.bbox_id} has fabricated polygon {w.polygon!r}"
        )


def test_tesseract_blocks_have_none_polygon() -> None:
    """Tesseract has no polygon source — every block.polygon must be None.

    Also a regression guard: bboxes must be unchanged by the polygon-field
    addition. We compare against a known-shape from the synthetic fallback
    (no pytesseract → single page-level block).
    """
    # PyMuPDF can decode our 1x1 PNG. If pytesseract is installed locally
    # the path goes through real OCR and we just assert the polygon-None
    # invariant on whatever blocks come back. Either way: never a polygon.
    blocks = _tesseract_extract_image(_TINY_PNG, filetype="png")
    assert blocks, "tesseract path must always emit at least one block"
    for b in blocks:
        assert b.polygon is None, (
            f"tesseract block {b.bbox_id} unexpectedly has polygon {b.polygon!r}"
        )
        # bbox shape unchanged: still a 4-tuple of floats.
        assert isinstance(b.bbox, tuple) and len(b.bbox) == 4
        for v in b.bbox:
            assert isinstance(v, float)


def test_layout_block_to_dict_round_trips_polygon() -> None:
    """to_dict() must surface polygon (None or 4-tuple) for downstream JSON."""
    poly = ((1.0, 2.0), (3.0, 2.0), (3.0, 5.0), (1.0, 5.0))
    with_poly = LayoutBlock(
        bbox_id="p1-b000",
        page=1,
        bbox=(1.0, 2.0, 2.0, 3.0),
        text="hi",
        ocr_confidence=0.9,
        granularity=BlockGranularity.LINE,
        polygon=poly,
    )
    d = with_poly.to_dict()
    assert "polygon" in d
    assert d["polygon"] == poly

    without_poly = LayoutBlock(
        bbox_id="p1-b001",
        page=1,
        bbox=(0.0, 0.0, 1.0, 1.0),
        text="hi",
        ocr_confidence=0.9,
        granularity=BlockGranularity.WORD,
    )
    d2 = without_poly.to_dict()
    assert "polygon" in d2
    assert d2["polygon"] is None
