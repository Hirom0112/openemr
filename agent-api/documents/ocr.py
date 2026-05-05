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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Iterable, List, Tuple

import pymupdf  # PyMuPDF

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LayoutBlock:
    """A single OCR/layout region with a stable, page-scoped identifier."""

    bbox_id: str  # format: "p{page}-b{idx:03d}", e.g. "p2-b017"
    page: int  # 1-indexed
    bbox: Tuple[float, float, float, float]  # (x, y, w, h) in PDF points
    text: str
    ocr_confidence: float  # 0.0 - 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
                    )
                )
                idx += 1
    return blocks


def _extract_image_layout(image_bytes: bytes, *, filetype: str) -> List[LayoutBlock]:
    """Extract layout from a single-page raster image (PNG/JPEG).

    PyMuPDF rasterizes the image into a one-page synthetic doc. The text-layer
    path is empty (it's a raw image). We attempt Tesseract via ``pytesseract``;
    if unavailable we emit a single page-level block with confidence 0.0 so
    callers can degrade gracefully.
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
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as exc:  # noqa: BLE001 — tesseract optional
        logger.info(
            "ocr_image_no_tesseract",
            extra={
                "filetype": filetype,
                "error_type": type(exc).__name__,
            },
        )
        return [
            LayoutBlock(
                bbox_id=_format_bbox_id(1, 0),
                page=1,
                bbox=(0.0, 0.0, page_w, page_h),
                text="",
                ocr_confidence=0.0,
            )
        ]

    blocks: List[LayoutBlock] = []
    idx = 0
    n = len(data.get("text", []))
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
        blocks.append(
            LayoutBlock(
                bbox_id=_format_bbox_id(1, idx),
                page=1,
                bbox=(x, y, w, h),
                text=word,
                ocr_confidence=conf,
            )
        )
        idx += 1

    if not blocks:
        # Tesseract ran but found nothing — degrade like the no-tesseract path.
        return [
            LayoutBlock(
                bbox_id=_format_bbox_id(1, 0),
                page=1,
                bbox=(0.0, 0.0, page_w, page_h),
                text="",
                ocr_confidence=0.0,
            )
        ]
    return blocks


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
