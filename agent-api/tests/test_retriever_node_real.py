"""Tests for the real (Slice 4.4) ``graph.nodes.retriever.retriever_node``.

Patches ``rag.retrieve.search`` so the node can be exercised without a
Postgres connection. Confirms that ``state["retrieval"]["snippets"]`` is
populated, and that an empty message short-circuits to a ``no_query``
skip without ever calling search.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

sys.path.insert(0, str(Path(__file__).parent.parent))

def _fake_snippets() -> list:
    from rag.retrieve import Snippet
    return [
        Snippet(
            chunk_id="kdigo-aki-2012-p1-000",
            source_id="kdigo-aki-2012",
            document_title="KDIGO AKI 2012",
            section="KDIGO STAGE 2 AKI CRITERIA",
            page_number=1,
            indexed_version_date=_dt.date(2012, 3, 1),
            content="Stage 2 AKI is defined by creatinine multiples of baseline.",
            relevance_score=0.88,
        )
    ]


def _retriever_node():
    # Py3.9 event-loop guard: importing graph.nodes.retriever transitively
    # loads auth.fhir_client which constructs an asyncio.Lock at module scope.
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from graph.nodes.retriever import retriever_node
    return retriever_node


def _state(message: str | None) -> dict:
    return {
        "request_id": "rid-test",
        "session_id": "sess-test",
        "provider_id": "prov-test",
        "patient_id": "pat-test",
        "message": message,
        "file_bytes_ref": None,
        "doc_type_hint": None,
        "errors": [],
    }


async def test_retriever_populates_snippets() -> None:
    fake = AsyncMock(return_value=_fake_snippets())
    with patch("rag.retrieve.search", new=fake):
        out = await _retriever_node()(_state("stage 2 aki criteria"))
    assert "retrieval" in out
    snippets = out["retrieval"]["snippets"]
    assert len(snippets) == 1
    s = snippets[0]
    assert s["chunk_id"] == "kdigo-aki-2012-p1-000"
    # date should be ISO-formatted for downstream JSON serialisation.
    assert s["indexed_version_date"] == "2012-03-01"
    fake.assert_awaited_once()


async def test_retriever_skips_when_no_message() -> None:
    fake = AsyncMock(return_value=_fake_snippets())
    with patch("rag.retrieve.search", new=fake):
        out = await _retriever_node()(_state(""))
    assert out["retrieval"]["snippets"] == []
    assert out["retrieval"]["skipped_reason"] == "no_query"
    fake.assert_not_awaited()


async def test_retriever_skips_when_message_is_whitespace() -> None:
    fake = AsyncMock(return_value=_fake_snippets())
    with patch("rag.retrieve.search", new=fake):
        out = await _retriever_node()(_state("   \n  "))
    assert out["retrieval"]["snippets"] == []
    assert out["retrieval"]["skipped_reason"] == "no_query"
    fake.assert_not_awaited()


async def test_retriever_handles_search_error() -> None:
    async def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("upstream blew up")

    with patch("rag.retrieve.search", new=_boom):
        out = await _retriever_node()(_state("any query"))
    assert out["retrieval"]["snippets"] == []
    assert out["retrieval"].get("error_type") == "RuntimeError"
