"""Tests for the multi-page TIFF loader (Phase 9 Slice 9.6).

Covers:

- Magic-byte recognition for both endian variants.
- Per-page bbox-aware blocks: a 4-page TIFF emits ≥1 block per page, with
  ``page=1..4`` distinct (catches the seek/iterator off-by-one trap that
  the eval rubric ``tiff_all_pages_ocrd`` exists to catch).
- 5-page Kowalski packet exercises the same coverage rule for the longer
  document.
- Bitonal-fax mode upcast: a synthetic 1-bit Pillow image fed into the
  preprocess pipeline emerges as mode ``"L"``.
- ``extract_layout`` magic-byte dispatch routes a TIFF to the new loader.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.fax_preprocess import preprocess_fax_page  # noqa: E402
from documents.ocr import _is_tiff, extract_layout  # noqa: E402
from documents.tiff_loader import (  # noqa: E402
    _TIFF_MAGIC_BE,
    _TIFF_MAGIC_LE,
    extract_tiff_layout,
    is_tiff,
)

pytestmark = pytest.mark.hard_failure

TIFF_DIR = ROOT / "tests" / "fixtures" / "w2" / "multimodal" / "tiff"
CHEN_TIFF = TIFF_DIR / "p01-chen-fax-packet.tiff"      # 4 pages
KOWALSKI_TIFF = TIFF_DIR / "p04-kowalski-fax-packet.tiff"  # 5 pages


# --------------------------------------------------------------------------- #
# Magic byte recognition
# --------------------------------------------------------------------------- #


def test_is_tiff_accepts_both_endians() -> None:
    assert is_tiff(_TIFF_MAGIC_LE + b"...rest...")
    assert is_tiff(_TIFF_MAGIC_BE + b"...rest...")
    assert not is_tiff(b"PK\x03\x04zip-not-tiff")
    assert not is_tiff(b"")


def test_extract_layout_dispatches_tiff() -> None:
    """``extract_layout`` magic-byte branch must route TIFF bytes into the
    new loader. We check the file header rather than mocking — the actual
    OCR result depends on tesseract availability, but the dispatch is
    independent of that.
    """
    raw = CHEN_TIFF.read_bytes()
    assert _is_tiff(raw), "fixture is not actually a TIFF"
    blocks = extract_layout(raw)
    # Even without pytesseract installed we get one stub block per page;
    # that's the contract that lets the post-ingest pipeline survive a
    # tooling outage instead of dropping pages silently.
    pages_seen = sorted({b.page for b in blocks})
    assert pages_seen == [1, 2, 3, 4], (
        f"chen TIFF should expose pages 1..4, got {pages_seen}"
    )


# --------------------------------------------------------------------------- #
# Per-page coverage (the rubric ``tiff_all_pages_ocrd`` enforces)
# --------------------------------------------------------------------------- #


def test_chen_4_page_packet_emits_block_per_page() -> None:
    raw = CHEN_TIFF.read_bytes()
    blocks = extract_tiff_layout(raw)
    # ≥1 block per page, page indices 1..4 distinct, no page=0.
    pages = {b.page for b in blocks}
    assert pages == {1, 2, 3, 4}
    for p in (1, 2, 3, 4):
        page_blocks = [b for b in blocks if b.page == p]
        assert page_blocks, f"page {p} produced zero blocks"


def test_kowalski_5_page_packet_emits_block_per_page() -> None:
    raw = KOWALSKI_TIFF.read_bytes()
    blocks = extract_tiff_layout(raw)
    pages = {b.page for b in blocks}
    assert pages == {1, 2, 3, 4, 5}
    for p in (1, 2, 3, 4, 5):
        page_blocks = [b for b in blocks if b.page == p]
        assert page_blocks, f"page {p} produced zero blocks"


def test_chen_bbox_ids_encode_real_page_index() -> None:
    """``bbox_id`` must start with ``p{page}-`` matching ``page`` — the
    classic off-by-one shows up as every block carrying ``p1-...``."""
    raw = CHEN_TIFF.read_bytes()
    blocks = extract_tiff_layout(raw)
    for b in blocks:
        prefix = f"p{b.page}-"
        assert b.bbox_id.startswith(prefix), (
            f"bbox_id {b.bbox_id!r} does not match page {b.page}"
        )


# --------------------------------------------------------------------------- #
# Bitonal fax preprocessing
# --------------------------------------------------------------------------- #


def test_fax_preprocess_upcasts_mode_1_to_L() -> None:
    """Mode-1 (bitonal) input must come out of preprocess_fax_page as mode
    ``"L"``. Tesseract's binarizer was tuned for L/RGB; feeding it 1-bit
    input degrades recognition substantially. Locked decision.
    """
    from PIL import Image

    img = Image.new("1", (200, 200), 1)  # all-white bitonal
    out = preprocess_fax_page(img)
    assert out.mode == "L", f"expected mode L, got {out.mode}"


def test_fax_preprocess_stamps_target_dpi() -> None:
    from PIL import Image

    img = Image.new("L", (200, 200), 255)
    out = preprocess_fax_page(img)
    dpi = out.info.get("dpi")
    assert dpi == (200, 200), f"expected dpi (200, 200), got {dpi}"


def test_fax_preprocess_upsamples_small_input() -> None:
    """Below ``_UPSAMPLE_THRESHOLD_PX`` the pipeline doubles dimensions so
    tesseract's LSTM has enough pixels per glyph."""
    from PIL import Image

    img = Image.new("L", (200, 200), 255)  # 200 px tall < 1500 threshold
    out = preprocess_fax_page(img)
    # 200 → 400 after 2× upsample. Allow the deskew rotation to not
    # alter the dimensions on a uniform white image (no content, no
    # rotation triggers).
    assert out.size[1] >= 400, f"expected upsampled height ≥400, got {out.size}"
