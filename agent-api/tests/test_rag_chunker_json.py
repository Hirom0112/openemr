"""Unit tests for ``rag.chunker.chunk_guideline_json``.

These exercise the JSON-bucket chunker against a synthetic file in a tmp
directory plus a smoke test against the real ``data/guidelines/`` corpus.
No DB, no embeddings, no PDFs — pure-Python.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.chunker import Chunk, chunk_guideline_json  # noqa: E402


def _write_bucket(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "bucket.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_chunk_json_one_chunk_per_entry(tmp_path: Path) -> None:
    payload = {
        "bucket": "demo",
        "source_date": "2024-12",
        "entries": [
            {
                "id": "demo-1",
                "title": "First recommendation",
                "snippet": "Body of first recommendation.",
                "applies_when": "adult patients",
                "source_section": "1.1",
                "condition_tags": ["t2dm", "hba1c"],
            },
            {
                "id": "demo-2",
                "title": "Second recommendation",
                "snippet": "Body of second recommendation.",
                "applies_when": "older adults",
                "source_section": "1.2",
                "condition_tags": ["ckd"],
            },
        ],
    }
    chunks = chunk_guideline_json(_write_bucket(tmp_path, payload))
    assert len(chunks) == 2
    assert all(isinstance(c, Chunk) for c in chunks)
    ids = [c.chunk_id_override for c in chunks]
    assert ids == ["demo-1", "demo-2"]


def test_chunk_json_content_includes_signal(tmp_path: Path) -> None:
    payload = {
        "bucket": "demo",
        "entries": [
            {
                "id": "demo-1",
                "title": "Title text",
                "snippet": "Snippet body text.",
                "applies_when": "adult with diabetes",
                "source_section": "S1",
                "condition_tags": ["t2dm", "metformin"],
            }
        ],
    }
    chunks = chunk_guideline_json(_write_bucket(tmp_path, payload))
    assert len(chunks) == 1
    content = chunks[0].content
    # Title, snippet, applies_when clause, and condition tags must all be in
    # content so both ts_rank and dense embedding pick them up.
    assert "Title text" in content
    assert "Snippet body text." in content
    assert "Applies when: adult with diabetes" in content
    assert "t2dm" in content and "metformin" in content
    assert chunks[0].section == "S1"
    assert chunks[0].page_number == 0


def test_chunk_json_skips_entries_without_id(tmp_path: Path) -> None:
    payload = {
        "bucket": "demo",
        "entries": [
            {"id": "", "title": "missing id", "snippet": "x"},
            {"title": "no id field", "snippet": "x"},
            {"id": "demo-good", "title": "good", "snippet": "yes"},
        ],
    }
    chunks = chunk_guideline_json(_write_bucket(tmp_path, payload))
    assert len(chunks) == 1
    assert chunks[0].chunk_id_override == "demo-good"


def test_chunk_json_handles_missing_optional_fields(tmp_path: Path) -> None:
    payload = {
        "bucket": "demo",
        "entries": [
            {
                "id": "demo-min",
                "title": "minimal",
                "snippet": "just body",
                # no applies_when, no source_section, no condition_tags
            }
        ],
    }
    chunks = chunk_guideline_json(_write_bucket(tmp_path, payload))
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunk_id_override == "demo-min"
    assert "minimal" in c.content
    assert "just body" in c.content
    # Defaults: section falls back to GUIDELINE, page_number is 0.
    assert c.section == "GUIDELINE"
    assert c.page_number == 0


def test_chunk_json_real_corpus_diabetes_bucket() -> None:
    # Smoke test against the real corpus: diabetes_t2dm.json has 8 entries
    # per index.json, and every chunk must carry its entry id.
    repo_root = Path(__file__).resolve().parent.parent
    bucket = repo_root / "data" / "guidelines" / "diabetes_t2dm.json"
    assert bucket.exists(), f"missing fixture: {bucket}"
    chunks = chunk_guideline_json(bucket)
    assert len(chunks) == 8
    ids = [c.chunk_id_override for c in chunks]
    # Spot-check a couple of well-known entry ids.
    assert "ada-2025-6.5a" in ids
    assert "ada-2025-9.6" in ids
    # Every chunk must have non-empty content and a valid section.
    for c in chunks:
        assert c.content.strip()
        assert c.section
        assert c.page_number == 0
