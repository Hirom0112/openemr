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

from documents.ocr import (  # noqa: E402
    BlockGranularity,
    LayoutBlock,
    _WORD_BBOX_PAD_PCT,
    document_confidence,
    extract_layout,
)

FIXTURE = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"
PNG_FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "eval"
    / "real-examples"
    / "p04-kowalski-intake.png"
)
# bbox IDs are zero-padded to at least 3 digits; LINE-granularity blocks
# use a separate counter starting at 1000 (Wave 2A), so we accept 3+ digits.
BBOX_ID_RE = re.compile(r"^p\d+-b\d{3,}$")

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


# ── Wave 2A — granularity, padding, line-emission ─────────────────────────


def _have_tesseract() -> bool:
    try:
        import pytesseract  # type: ignore  # noqa: F401
        from PIL import Image  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    # Even if pytesseract imports, the binary may be missing — try a tiny call.
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        Image.new("RGB", (10, 10))
        # version() raises if the binary is absent
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def _png_or_skip() -> bytes:
    if not PNG_FIXTURE.exists():
        pytest.skip(f"png fixture missing: {PNG_FIXTURE}")
    if not _have_tesseract():
        pytest.skip("tesseract binary / pytesseract not available on host")
    return PNG_FIXTURE.read_bytes()


def test_existing_layoutblock_construction_works_without_granularity_arg() -> None:
    """Back-compat: existing call sites construct LayoutBlock with 5 args."""
    b = LayoutBlock(
        bbox_id="p1-b000",
        page=1,
        bbox=(0.0, 0.0, 10.0, 20.0),
        text="hello",
        ocr_confidence=0.9,
    )
    assert b.granularity == BlockGranularity.WORD
    # to_dict must serialize the enum to its string value for JSON consumers.
    d = b.to_dict()
    assert d["granularity"] == "word"


def test_layout_emits_both_word_and_line_for_image() -> None:
    png_bytes = _png_or_skip()
    blocks = extract_layout(png_bytes)
    grans = {b.granularity for b in blocks}
    assert BlockGranularity.WORD in grans, (
        f"expected WORD blocks; got granularities={grans}"
    )
    assert BlockGranularity.LINE in grans, (
        f"expected LINE blocks; got granularities={grans}"
    )
    # IDs are unique across both granularities.
    ids = [b.bbox_id for b in blocks]
    assert len(ids) == len(set(ids)), "bbox_ids must be globally unique"


def test_word_bboxes_are_padded() -> None:
    """A padded bbox must be wider/taller than the raw tesseract output by
    ~2 * _WORD_BBOX_PAD_PCT (15% on each side → +30% nominal)."""
    png_bytes = _png_or_skip()
    blocks = extract_layout(png_bytes)

    # Recover a raw tesseract reading to compare against.
    import io

    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore

    img = Image.open(io.BytesIO(png_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    raw = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    # Find the first non-empty word and compare. (Skipping whitespace-only
    # entries matches the production code path.)
    raw_match = None
    for i in range(len(raw["text"])):
        word = (raw["text"][i] or "").strip()
        try:
            conf_raw = float(raw["conf"][i])
        except (TypeError, ValueError):
            conf_raw = -1.0
        if word and conf_raw >= 0:
            raw_match = (
                word,
                float(raw["left"][i]),
                float(raw["top"][i]),
                float(raw["width"][i]),
                float(raw["height"][i]),
            )
            break
    assert raw_match is not None, "tesseract returned no words on this fixture"
    word_text, raw_x, raw_y, raw_w, raw_h = raw_match

    # Find the corresponding emitted WORD block (first one matching this text).
    candidates = [
        b
        for b in blocks
        if b.granularity == BlockGranularity.WORD and b.text == word_text
    ]
    assert candidates, f"no WORD block matched raw word {word_text!r}"
    b = candidates[0]
    _bx, _by, bw, bh = b.bbox

    # Expected dimensions: width and height each grow by 2 * pct.
    expected_w = raw_w * (1.0 + 2.0 * _WORD_BBOX_PAD_PCT)
    expected_h = raw_h * (1.0 + 2.0 * _WORD_BBOX_PAD_PCT)
    assert bw == pytest.approx(expected_w, rel=1e-6), (
        f"word bbox width not padded: raw={raw_w} got={bw} expected={expected_w}"
    )
    assert bh == pytest.approx(expected_h, rel=1e-6), (
        f"word bbox height not padded: raw={raw_h} got={bh} expected={expected_h}"
    )


def test_line_bbox_is_union_of_word_bboxes() -> None:
    png_bytes = _png_or_skip()
    blocks = extract_layout(png_bytes)

    word_blocks = [b for b in blocks if b.granularity == BlockGranularity.WORD]
    line_blocks = [b for b in blocks if b.granularity == BlockGranularity.LINE]
    assert word_blocks, "no word blocks emitted"
    assert line_blocks, "no line blocks emitted"

    # The first line's text is the space-joined text of some contiguous run
    # of words. Find them and confirm the line's bbox contains every member.
    line = line_blocks[0]
    line_words = line.text.split(" ")
    # Greedy match: walk word_blocks in order looking for a contiguous slice
    # whose .text equals line_words.
    members = None
    for start in range(len(word_blocks)):
        slice_ = word_blocks[start : start + len(line_words)]
        if [w.text for w in slice_] == line_words:
            members = slice_
            break
    assert members is not None, (
        f"could not align line text {line.text!r} to a contiguous word run"
    )

    lx, ly, lw, lh = line.bbox
    lx2, ly2 = lx + lw, ly + lh
    for w in members:
        wx, wy, ww, wh = w.bbox
        wx2, wy2 = wx + ww, wy + wh
        # The LINE bbox must contain each constituent's CENTER (the LINE
        # bbox is built from the *raw* word boxes, while we're checking the
        # *padded* word boxes — center containment is the right invariant).
        cx, cy = (wx + wx2) / 2.0, (wy + wy2) / 2.0
        assert lx <= cx <= lx2, (
            f"word center x={cx} outside line x-range [{lx}, {lx2}]"
        )
        assert ly <= cy <= ly2, (
            f"word center y={cy} outside line y-range [{ly}, {ly2}]"
        )


def test_pdf_layout_emits_line_granularity() -> None:
    pdf_bytes = _read_fixture()
    blocks = extract_layout(pdf_bytes)
    assert blocks, "expected at least one PDF layout block"
    assert all(
        b.granularity == BlockGranularity.LINE for b in blocks
    ), (
        "PDF text-layer blocks must be tagged LINE; "
        f"got {set(b.granularity for b in blocks)}"
    )
