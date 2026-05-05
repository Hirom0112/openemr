"""Section-aware PDF chunker for the guideline corpus.

The chunker walks each page, infers section boundaries from heuristic
heading detection (PyMuPDF font-size jumps where available; otherwise an
ALL-CAPS regex), and emits ~512-token windows with ~100 tokens of
overlap between adjacent windows in the same section.

Token approximation: 4 characters per token. We deliberately do not pull
``tiktoken`` here — it adds 50 MB to the runtime image for a precision
the retrieval pipeline does not need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    """One unit of indexed text. Source location lives at the indexer."""

    section: str
    page_number: int
    content: str


# ── Tunables ────────────────────────────────────────────────────────────────


_CHARS_PER_TOKEN = 4
_TARGET_TOKENS = 512
_OVERLAP_TOKENS = 100

_TARGET_CHARS = _TARGET_TOKENS * _CHARS_PER_TOKEN
_OVERLAP_CHARS = _OVERLAP_TOKENS * _CHARS_PER_TOKEN


# An ALL-CAPS line of >=3 characters with optional digits / spaces.
# Matches "SEPSIS HOUR-1 BUNDLE", "KDIGO STAGE 2 AKI CRITERIA", etc.
_HEADING_REGEX = re.compile(r"^[A-Z][A-Z0-9\s\-/]{2,}$")


def _is_heading_text(line: str) -> bool:
    """Return True when ``line`` looks like an ALL-CAPS section heading."""
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) < 3 or len(stripped) > 80:
        return False
    return bool(_HEADING_REGEX.match(stripped))


# ── Heading detection via PyMuPDF (preferred) ──────────────────────────────


def _extract_pages_with_headings(pdf_bytes: bytes) -> list[list[tuple[str, str]]]:
    """Per page, return a list of ``(role, text)`` where role in ``{"heading","body"}``.

    Tries PyMuPDF font-size analysis first; falls back to ALL-CAPS regex
    line detection on pages where dict-mode extraction yields no spans.
    """
    import pymupdf as _fitz  # type: ignore[import-not-found]

    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages: list[list[tuple[str, str]]] = []

        # Compute median font size across the document so an "ALL-CAPS body"
        # in a page-header band cannot get mis-classified as a heading.
        all_sizes: list[float] = []
        for page in doc:
            try:
                page_dict = page.get_text("dict")
            except Exception:
                continue
            for block in page_dict.get("blocks", []) or []:
                for line in block.get("lines", []) or []:
                    for span in line.get("spans", []) or []:
                        size = float(span.get("size") or 0.0)
                        text = (span.get("text") or "").strip()
                        if size > 0 and text:
                            all_sizes.append(size)

        median_size = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 11.0
        # A span is a heading if its font is >= 1.4x the median AND its text
        # is heading-shaped (ALL CAPS or ends without a sentence terminator).
        heading_size_threshold = median_size * 1.4

        for page in doc:
            page_items: list[tuple[str, str]] = []
            try:
                page_dict = page.get_text("dict")
            except Exception:
                page_dict = {"blocks": []}

            had_spans = False
            for block in page_dict.get("blocks", []) or []:
                for line in block.get("lines", []) or []:
                    spans = line.get("spans", []) or []
                    if not spans:
                        continue
                    had_spans = True
                    line_text = "".join((s.get("text") or "") for s in spans).strip()
                    if not line_text:
                        continue
                    max_size = max(float(s.get("size") or 0.0) for s in spans)
                    is_heading = (
                        max_size >= heading_size_threshold
                        and _is_heading_text(line_text)
                    )
                    if is_heading:
                        page_items.append(("heading", line_text))
                    else:
                        page_items.append(("body", line_text))

            if not had_spans:
                # Fallback: use plain text + regex.
                try:
                    raw = page.get_text("text") or ""
                except Exception:
                    raw = ""
                for raw_line in raw.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if _is_heading_text(line):
                        page_items.append(("heading", line))
                    else:
                        page_items.append(("body", line))

            pages.append(page_items)
        return pages
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ── Pure-text fallback (no PDF) — used by tests ─────────────────────────────


def _chunk_plain_text(
    text: str,
    *,
    page_number: int = 1,
    initial_section: str = "INTRO",
) -> list[Chunk]:
    """Section-aware chunker over a plain string. Used by unit tests."""
    items: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _is_heading_text(line):
            items.append(("heading", line))
        else:
            items.append(("body", line))
    return _items_to_chunks(items, page_number=page_number, initial_section=initial_section)


def _items_to_chunks(
    items: list[tuple[str, str]],
    *,
    page_number: int,
    initial_section: str,
) -> list[Chunk]:
    """Group ``(role, text)`` items into section buckets, then window each."""
    sections: list[tuple[str, list[str]]] = [(initial_section, [])]
    for role, text in items:
        if role == "heading":
            sections.append((text, []))
        else:
            sections[-1][1].append(text)

    chunks: list[Chunk] = []
    for section_name, body_lines in sections:
        if not body_lines:
            continue
        joined = " ".join(body_lines).strip()
        if not joined:
            continue
        for window in _slide_window(joined):
            chunks.append(
                Chunk(
                    section=section_name,
                    page_number=page_number,
                    content=window,
                )
            )
    return chunks


def _slide_window(text: str) -> list[str]:
    """Return overlapping char-windows over ``text`` sized to ~512 tokens."""
    if len(text) <= _TARGET_CHARS:
        return [text]
    out: list[str] = []
    step = _TARGET_CHARS - _OVERLAP_CHARS
    if step <= 0:
        step = _TARGET_CHARS
    start = 0
    while start < len(text):
        end = min(start + _TARGET_CHARS, len(text))
        window = text[start:end].strip()
        if window:
            out.append(window)
        if end >= len(text):
            break
        start += step
    return out


# ── Public API ──────────────────────────────────────────────────────────────


def chunk_guideline_pdf(pdf_bytes: bytes) -> list[Chunk]:
    """Chunk a guideline PDF into section-anchored windows.

    Each chunk carries the section it falls under and the 1-indexed page
    number it originated from. Sections that span multiple pages produce
    chunks on each page they touch — the page number is the page where
    the body text was actually rendered.
    """
    pages = _extract_pages_with_headings(pdf_bytes)
    if not pages:
        return []

    chunks: list[Chunk] = []
    # Carry the most recent heading across page boundaries so a section
    # that spans pages 2-3 still attributes its page-3 body to the right
    # section instead of resetting to "INTRO".
    last_section = "INTRO"
    for page_idx, items in enumerate(pages, start=1):
        page_chunks = _items_to_chunks(
            items, page_number=page_idx, initial_section=last_section
        )
        chunks.extend(page_chunks)
        # Update last_section to the final section seen on this page, if any.
        for role, text in items:
            if role == "heading":
                last_section = text
    return chunks


__all__ = ["Chunk", "chunk_guideline_pdf"]
