"""OCR / layout extraction layer (Slice 1.1).

The split between OCR (location) and Claude vision (meaning) is the central
anti-hallucination defense (W2_ARCHITECTURE.md §5.3). This module is the
deterministic location half: PyMuPDF enumerates where text lives on the page
and emits stable bbox IDs.

For text-PDFs, per-block OCR confidence is 1.0. For raw-image inputs (PNG),
PyMuPDF rasterizes a single page from the image. If pytesseract is available
we run OCR and emit per-word LayoutBlocks with the engine's reported
confidence; otherwise we emit a single page-level block with confidence 0.0
so the critic's degradation path (W2_ARCHITECTURE §8.7) can soft-warn.

Wave 2A — granularity: layout extraction now emits both word-level and
line-level LayoutBlocks tagged with a typed ``BlockGranularity`` enum.
Word-level bboxes are padded by ``_WORD_BBOX_PAD_PCT`` to compensate for
tesseract's tendency to report x-height-only boxes. Line-level bboxes are
the un-padded union of their constituent words. Downstream citation
resolution still defaults to word-level — Wave 2B switches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Iterable, List, Tuple

import pymupdf  # PyMuPDF

logger = logging.getLogger(__name__)


class BlockGranularity(str, Enum):
    """Granularity tag for a LayoutBlock.

    WORD — single tesseract word (or PDF text-fragment); bbox is padded.
    LINE — union of words on a tesseract text line, OR a PDF paragraph
           block from ``page.get_text("blocks")`` (which is line-or-larger
           in practice). LINE bboxes are NOT padded.
    """

    WORD = "word"
    LINE = "line"


# Pads each tesseract word bbox by this fraction on every side; tesseract
# reports x-height-only boxes for italic/cursive text, so without padding
# the rendered overlay misses ascenders/descenders. LINE-level bboxes are
# already line-bounding by construction and are NOT padded.
_WORD_BBOX_PAD_PCT: float = 0.15


@dataclass(frozen=True)
class LayoutBlock:
    """A single OCR/layout region with a stable, page-scoped identifier."""

    bbox_id: str  # format: "p{page}-b{idx:03d}", e.g. "p2-b017"
    page: int  # 1-indexed
    bbox: Tuple[float, float, float, float]  # (x, y, w, h) in PDF points
    text: str
    ocr_confidence: float  # 0.0 - 1.0
    granularity: BlockGranularity = BlockGranularity.WORD

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # asdict on a str-Enum returns the Enum instance; coerce to its
        # string value so JSON encoders downstream don't have to special-case.
        gran = d.get("granularity")
        if isinstance(gran, BlockGranularity):
            d["granularity"] = gran.value
        return d


def _format_bbox_id(page: int, idx: int) -> str:
    return f"p{page}-b{idx:03d}"


# Magic-byte signatures for the image formats we accept as "image input".
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC_PREFIX = b"\xff\xd8\xff"


def _is_png(data: bytes) -> bool:
    return data.startswith(_PNG_MAGIC)


def _is_jpeg(data: bytes) -> bool:
    return data.startswith(_JPEG_MAGIC_PREFIX)


def _extract_pdf_layout(pdf_bytes: bytes) -> List[LayoutBlock]:
    blocks: List[LayoutBlock] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_index, page in enumerate(doc, start=1):
            # get_text("blocks") returns: (x0, y0, x1, y1, text, block_no, block_type)
            # These are paragraph-level groupings — line-or-larger in practice —
            # so we tag them as LINE granularity.
            raw_blocks = page.get_text("blocks")
            idx = 0
            for rb in raw_blocks:
                x0, y0, x1, y1, text, _block_no, block_type = rb[:7]
                if block_type != 0:
                    continue
                cleaned = (text or "").strip()
                if not cleaned:
                    continue
                blocks.append(
                    LayoutBlock(
                        bbox_id=_format_bbox_id(page_index, idx),
                        page=page_index,
                        bbox=(
                            float(x0),
                            float(y0),
                            float(x1 - x0),
                            float(y1 - y0),
                        ),
                        text=cleaned,
                        ocr_confidence=1.0,
                        granularity=BlockGranularity.LINE,
                    )
                )
                idx += 1
    return blocks


def _pad_word_bbox(
    x: float, y: float, w: float, h: float, pct: float
) -> Tuple[float, float, float, float]:
    """Symmetrically pad a (x, y, w, h) box by ``pct`` on every side, clamped to >= 0."""
    px = w * pct
    py = h * pct
    nx = max(0.0, x - px)
    ny = max(0.0, y - py)
    nw = w + 2.0 * px
    nh = h + 2.0 * py
    return (nx, ny, nw, nh)


def _extract_image_layout(image_bytes: bytes, *, filetype: str) -> List[LayoutBlock]:
    """Extract layout from a single-page raster image (PNG/JPEG).

    PyMuPDF rasterizes the image into a one-page synthetic doc. The text-layer
    path is empty (it's a raw image). We attempt Tesseract via ``pytesseract``;
    if unavailable we emit a single page-level block with confidence 0.0 so
    callers can degrade gracefully.

    Wave 2A: emits both WORD-level (padded) and LINE-level (un-padded union)
    LayoutBlocks. Words are grouped into lines by tesseract's
    ``block_num``/``par_num``/``line_num`` keys when present; when those keys
    are absent (older pytesseract / unusual builds), we log a warning and
    fall back to a y-band overlap heuristic — never silently skip lines.
    """
    # Determine page dimensions deterministically from the image.
    with pymupdf.open(stream=image_bytes, filetype=filetype) as doc:
        page = doc[0]
        rect = page.rect
        page_w = float(rect.width)
        page_h = float(rect.height)

    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        import io

        img = Image.open(io.BytesIO(image_bytes))
        # Force load and a sane RGB-ish mode for Tesseract.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # TODO(post-2A): tesseract PSM mode + DPI uplift evaluation —
        # separate slice with its own eval; default PSM (3 = fully automatic
        # page segmentation) and source DPI are intentionally unchanged here.
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as exc:  # noqa: BLE001 — tesseract optional
        logger.info(
            "ocr_image_no_tesseract",
            extra={
                "filetype": filetype,
                "error_type": type(exc).__name__,
            },
        )
        # Synthetic fallback: tag as WORD so consumers expecting at least one
        # word-level block keep working; the text is empty so it carries no
        # spatial-citation weight either way.
        return [
            LayoutBlock(
                bbox_id=_format_bbox_id(1, 0),
                page=1,
                bbox=(0.0, 0.0, page_w, page_h),
                text="",
                ocr_confidence=0.0,
                granularity=BlockGranularity.WORD,
            )
        ]

    # ── word-level pass ────────────────────────────────────────────────────
    word_blocks: List[LayoutBlock] = []
    # Parallel arrays preserved so we can group into lines below.
    word_meta: List[Tuple[int, int, int, float, float, float, float, str, float]] = []
    idx = 0
    n = len(data.get("text", []))
    has_group_keys = all(k in data for k in ("block_num", "par_num", "line_num"))
    if not has_group_keys:
        logger.warning(
            "ocr_tesseract_missing_group_keys",
            extra={"have_keys": sorted(data.keys())},
        )

    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        try:
            conf_raw = float(data["conf"][i])
        except (TypeError, ValueError):
            conf_raw = -1.0
        if conf_raw < 0:
            # Tesseract uses -1 to mean "no word here". Skip.
            continue
        # pytesseract conf is 0-100; normalize.
        conf = max(0.0, min(1.0, conf_raw / 100.0))
        x = float(data["left"][i])
        y = float(data["top"][i])
        w = float(data["width"][i])
        h = float(data["height"][i])
        padded = _pad_word_bbox(x, y, w, h, _WORD_BBOX_PAD_PCT)
        word_blocks.append(
            LayoutBlock(
                bbox_id=_format_bbox_id(1, idx),
                page=1,
                bbox=padded,
                text=word,
                ocr_confidence=conf,
                granularity=BlockGranularity.WORD,
            )
        )
        block_num = int(data["block_num"][i]) if has_group_keys else -1
        par_num = int(data["par_num"][i]) if has_group_keys else -1
        line_num = int(data["line_num"][i]) if has_group_keys else -1
        # Track the RAW (un-padded) bbox for line-union math.
        word_meta.append((block_num, par_num, line_num, x, y, w, h, word, conf))
        idx += 1

    if not word_blocks:
        # Tesseract ran but found nothing — degrade like the no-tesseract path.
        return [
            LayoutBlock(
                bbox_id=_format_bbox_id(1, 0),
                page=1,
                bbox=(0.0, 0.0, page_w, page_h),
                text="",
                ocr_confidence=0.0,
                granularity=BlockGranularity.WORD,
            )
        ]

    # ── line-level pass ────────────────────────────────────────────────────
    # Group by (block_num, par_num, line_num) when present; otherwise fall
    # back to a y-band overlap heuristic. Never silently drop the line pass.
    line_groups: List[List[Tuple[int, int, int, float, float, float, float, str, float]]] = []
    if has_group_keys:
        from collections import OrderedDict

        bucket: "OrderedDict[Tuple[int, int, int], List[Tuple[int, int, int, float, float, float, float, str, float]]]" = OrderedDict()
        for meta in word_meta:
            key = (meta[0], meta[1], meta[2])
            bucket.setdefault(key, []).append(meta)
        line_groups = list(bucket.values())
    else:
        # y-overlap fallback: words whose vertical band overlaps share a line.
        # Sorted by y, then a greedy single-pass merge.
        sorted_meta = sorted(word_meta, key=lambda m: (m[4], m[3]))
        current: List[Tuple[int, int, int, float, float, float, float, str, float]] = []
        cur_y0 = 0.0
        cur_y1 = 0.0
        for meta in sorted_meta:
            _, _, _, _x, y, _w, h, _word, _c = meta
            wy0, wy1 = y, y + h
            if not current:
                current = [meta]
                cur_y0, cur_y1 = wy0, wy1
                continue
            # Overlap iff intervals share any vertical extent (>= 30% of the
            # smaller height — tolerates sub-pixel jitter without merging
            # adjacent lines).
            overlap = max(0.0, min(cur_y1, wy1) - max(cur_y0, wy0))
            min_h = max(1.0, min(cur_y1 - cur_y0, wy1 - wy0))
            if overlap / min_h >= 0.3:
                current.append(meta)
                cur_y0 = min(cur_y0, wy0)
                cur_y1 = max(cur_y1, wy1)
            else:
                line_groups.append(current)
                current = [meta]
                cur_y0, cur_y1 = wy0, wy1
        if current:
            line_groups.append(current)

    line_blocks: List[LayoutBlock] = []
    # Use a separate ID range (>= 1000) so word and line IDs never collide.
    LINE_IDX_BASE = 1000
    for line_idx, group in enumerate(line_groups):
        if not group:
            continue
        xs0 = [m[3] for m in group]
        ys0 = [m[4] for m in group]
        xs1 = [m[3] + m[5] for m in group]
        ys1 = [m[4] + m[6] for m in group]
        x0 = min(xs0)
        y0 = min(ys0)
        x1 = max(xs1)
        y1 = max(ys1)
        # Preserve word order within a line. For the group-keys path the
        # order from the DICT is already left-to-right; for the y-overlap
        # fallback we sort by x.
        if has_group_keys:
            ordered = group
        else:
            ordered = sorted(group, key=lambda m: m[3])
        text = " ".join(m[7] for m in ordered)
        confs = [m[8] for m in ordered]
        mean_conf = sum(confs) / len(confs) if confs else 0.0
        line_blocks.append(
            LayoutBlock(
                bbox_id=_format_bbox_id(1, LINE_IDX_BASE + line_idx),
                page=1,
                bbox=(x0, y0, x1 - x0, y1 - y0),
                text=text,
                ocr_confidence=mean_conf,
                granularity=BlockGranularity.LINE,
            )
        )

    return word_blocks + line_blocks


def extract_layout(doc_bytes: bytes) -> List[LayoutBlock]:
    """Extract layout blocks from a PDF or raster image.

    Auto-detects content type from magic bytes:
      * PDF: per-block text from the embedded text layer (confidence 1.0).
      * PNG / JPEG: rasterized via PyMuPDF, OCR'd via pytesseract if available;
        otherwise a single page-level block with confidence 0.0 is returned
        (the critic's degradation path will soft-warn on low confidence).

    Args:
        doc_bytes: raw PDF or image bytes.

    Returns:
        List of LayoutBlock, ordered page-then-block-index.
    """
    if _is_png(doc_bytes):
        blocks = _extract_image_layout(doc_bytes, filetype="png")
    elif _is_jpeg(doc_bytes):
        blocks = _extract_image_layout(doc_bytes, filetype="jpeg")
    else:
        blocks = _extract_pdf_layout(doc_bytes)

    logger.info(
        "ocr_layout_extracted",
        extra={
            "n_blocks": len(blocks),
            "n_pages": blocks[-1].page if blocks else 0,
            "n_word_blocks": sum(
                1 for b in blocks if b.granularity == BlockGranularity.WORD
            ),
            "n_line_blocks": sum(
                1 for b in blocks if b.granularity == BlockGranularity.LINE
            ),
        },
    )
    return blocks


def document_confidence(blocks: Iterable[LayoutBlock]) -> float:
    """Document-level OCR confidence = mean of per-block ocr_confidence.

    Returns 0.0 for empty input (caller should treat this as "no text found").
    """
    confs = [b.ocr_confidence for b in blocks]
    if not confs:
        return 0.0
    return sum(confs) / len(confs)
