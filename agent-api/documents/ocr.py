"""OCR / layout extraction layer (Slice 1.1).

The split between OCR (location) and Claude vision (meaning) is the central
anti-hallucination defense (W2_ARCHITECTURE.md §5.3). This module is the
deterministic location half: PyMuPDF enumerates where text lives on the page
and emits stable bbox IDs.

For text-PDFs, per-block OCR confidence is 1.0. For scanned PDFs (future:
pytesseract), per-block confidence reflects the OCR engine's reported score.
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


def extract_layout(pdf_bytes: bytes) -> List[LayoutBlock]:
    """Extract layout blocks from a PDF.

    Uses PyMuPDF's natural per-page block ordering for deterministic
    bbox IDs. For text-PDFs, blocks come from the embedded text layer
    and ocr_confidence is 1.0.

    Args:
        pdf_bytes: raw PDF bytes.

    Returns:
        List of LayoutBlock, ordered page-then-block-index.
    """
    blocks: List[LayoutBlock] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_index, page in enumerate(doc, start=1):
            # get_text("blocks") returns: (x0, y0, x1, y1, text, block_no, block_type)
            raw_blocks = page.get_text("blocks")
            # Stable order: PyMuPDF returns blocks in reading order. We keep that
            # and assign deterministic indices per page.
            idx = 0
            for rb in raw_blocks:
                x0, y0, x1, y1, text, _block_no, block_type = rb[:7]
                # block_type 0 == text; skip image blocks for now.
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
