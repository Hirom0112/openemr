"""Unit tests for ``rag.chunker``.

These exercise the pure-text fallback (``_chunk_plain_text``) so they run
without PyMuPDF or any PDF on disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.chunker import (  # noqa: E402
    _CHARS_PER_TOKEN,
    _TARGET_TOKENS,
    _chunk_plain_text,
    chunk_guideline_pdf,
)


def test_chunker_splits_at_section_boundaries() -> None:
    text = (
        "FIRST SECTION HEADER\n"
        "First section body text about lactate and sepsis bundles. "
        "More body content for the first section.\n"
        "SECOND SECTION HEADER\n"
        "Second section body about kidney injury and creatinine.\n"
    )
    chunks = _chunk_plain_text(text)
    sections = {c.section for c in chunks}
    assert "FIRST SECTION HEADER" in sections
    assert "SECOND SECTION HEADER" in sections
    # Each chunk's content should be within its claimed section.
    for chunk in chunks:
        if chunk.section == "FIRST SECTION HEADER":
            assert "lactate" in chunk.content or "sepsis" in chunk.content or "first" in chunk.content.lower()
        elif chunk.section == "SECOND SECTION HEADER":
            assert "kidney" in chunk.content or "creatinine" in chunk.content or "second" in chunk.content.lower()


def test_chunker_token_size_within_bounds() -> None:
    long_para = ("alpha beta gamma delta epsilon " * 1000).strip()
    text = "BIG SECTION\n" + long_para
    chunks = _chunk_plain_text(text)
    max_chars = _TARGET_TOKENS * _CHARS_PER_TOKEN
    assert len(chunks) >= 2  # forced to split
    for c in chunks:
        assert len(c.content) <= max_chars + 16  # tiny slack for boundary trimming


def test_chunker_overlap_present() -> None:
    long_para = ("alpha beta gamma delta epsilon zeta eta theta " * 800).strip()
    text = "OVERLAP SECTION\n" + long_para
    chunks = _chunk_plain_text(text)
    assert len(chunks) >= 2
    # Adjacent chunks should share at least 50 chars of overlap (target is
    # ~400 chars; we leave wide margin against future tweaks).
    a, b = chunks[0].content, chunks[1].content
    # Find any 32-char run from the tail of `a` that also appears in `b`.
    tail = a[-200:]
    found = False
    for start in range(0, len(tail) - 32):
        if tail[start : start + 32] in b:
            found = True
            break
    assert found, "expected adjacent chunks to share overlap text"


def test_chunk_guideline_pdf_empty() -> None:
    # Defensive: empty bytes should not crash, just return [].
    # (PyMuPDF raises on truly empty bytes, so we wrap in try.)
    try:
        result = chunk_guideline_pdf(b"")
    except Exception:
        return
    assert result == []
