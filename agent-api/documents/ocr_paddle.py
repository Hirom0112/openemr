"""PaddleOCR adapter for the OCR engine dispatcher (Wave 2A).

PaddleOCR returns line-granularity results: each detected text region is
``[quad_points, (text, confidence)]`` where ``quad_points`` is a 4-corner
polygon. We:

  1. Convert each polygon to an axis-aligned bbox (x, y, w, h).
  2. Emit a LINE-granularity LayoutBlock for the whole region.
  3. Split the line text on whitespace and approximate per-word bboxes by
     splitting the line bbox proportionally to character counts. This is
     an approximation: paddle's detector does not surface true word-level
     boxes natively. The approximation is acceptable for downstream bbox
     citation because the line bbox always contains the truth and the
     repointer (Wave 2B) snaps to overlapping candidates.

Imports of ``paddleocr`` / ``paddlepaddle`` are deferred to first call so
tesseract-only deployments do not pay the cold-start cost (~30s + ~1GB RAM
on first model download).
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Any, List, Tuple

from documents.ocr import (
    BlockGranularity,
    LayoutBlock,
    _format_bbox_id,
    _pad_word_bbox,
    _WORD_BBOX_PAD_PCT,
)

logger = logging.getLogger(__name__)


class PaddleEngine:
    """OCREngine adapter for PaddleOCR.

    Heavy model objects are lazy-loaded once per process and reused across
    calls (PaddleOCR is thread-safe for inference once initialized).
    """

    name: str = "paddleocr"

    def __init__(self) -> None:
        self._ocr: Any = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> Any:
        if self._ocr is not None:
            return self._ocr
        with self._lock:
            if self._ocr is not None:
                return self._ocr
            # Lazy import — keeps tesseract-only images cheap.
            from paddleocr import PaddleOCR  # type: ignore

            logger.info("paddleocr_initializing")
            # use_angle_cls=True handles rotated text; lang="en" matches our
            # current corpus (intake forms + lab PDFs are English-only).
            self._ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
            logger.info("paddleocr_ready")
            return self._ocr

    def extract_image(self, image_bytes: bytes, *, filetype: str) -> List[LayoutBlock]:
        ocr = self._ensure_loaded()

        # Decode via PIL → numpy (paddle accepts ndarray directly).
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore

        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        arr = np.asarray(img)

        # PaddleOCR returns: [ [ [quad, (text, conf)], ... ] ]   (1 page)
        # Older versions return [ [quad, (text, conf)], ... ] without the
        # outer list. Normalise.
        try:
            raw = ocr.ocr(arr, cls=True)
        except Exception:
            logger.exception("paddleocr_ocr_call_failed")
            raise

        if not raw:
            return _empty_block()
        page0 = raw[0] if isinstance(raw[0], list) else raw
        if not page0:
            return _empty_block()

        word_blocks: List[LayoutBlock] = []
        line_blocks: List[LayoutBlock] = []
        word_idx = 0
        LINE_IDX_BASE = 1000

        for line_idx, region in enumerate(page0):
            try:
                quad, payload = region[0], region[1]
                text = (payload[0] or "").strip()
                conf = float(payload[1])
            except (IndexError, TypeError, ValueError):
                continue
            if not text:
                continue

            x0, y0, x1, y1 = _quad_to_bbox(quad)
            if x1 <= x0 or y1 <= y0:
                continue
            line_w = x1 - x0
            line_h = y1 - y0

            line_blocks.append(
                LayoutBlock(
                    bbox_id=_format_bbox_id(1, LINE_IDX_BASE + line_idx),
                    page=1,
                    bbox=(x0, y0, line_w, line_h),
                    text=text,
                    ocr_confidence=max(0.0, min(1.0, conf)),
                    granularity=BlockGranularity.LINE,
                )
            )

            # Approximate per-word bboxes by splitting the line bbox along x
            # in proportion to each word's character count (incl. one space
            # between words). PaddleOCR's detector does not surface true
            # word-level boxes; this is the documented approximation.
            words = text.split()
            if not words:
                continue
            total_chars = sum(len(w) for w in words) + max(0, len(words) - 1)
            if total_chars <= 0:
                continue
            cursor = x0
            for w in words:
                share = (len(w) + 1) / total_chars  # +1 for trailing space
                w_width = line_w * share
                wx, wy, ww, wh = _pad_word_bbox(
                    cursor, y0, w_width, line_h, _WORD_BBOX_PAD_PCT
                )
                word_blocks.append(
                    LayoutBlock(
                        bbox_id=_format_bbox_id(1, word_idx),
                        page=1,
                        bbox=(wx, wy, ww, wh),
                        text=w,
                        ocr_confidence=max(0.0, min(1.0, conf)),
                        granularity=BlockGranularity.WORD,
                    )
                )
                cursor += w_width
                word_idx += 1

        if not word_blocks:
            return _empty_block()
        return word_blocks + line_blocks


def _quad_to_bbox(quad: Any) -> Tuple[float, float, float, float]:
    """Convert a 4-point polygon to an axis-aligned (x0, y0, x1, y1)."""
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return (min(xs), min(ys), max(xs), max(ys))


def _empty_block() -> List[LayoutBlock]:
    """Same degraded shape as tesseract emits when nothing is found."""
    # We can't know page dims here without reopening the image; (0,0,0,0) is
    # acceptable because callers only consult the bbox when text is present.
    return [
        LayoutBlock(
            bbox_id=_format_bbox_id(1, 0),
            page=1,
            bbox=(0.0, 0.0, 0.0, 0.0),
            text="",
            ocr_confidence=0.0,
            granularity=BlockGranularity.WORD,
        )
    ]
