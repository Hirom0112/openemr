"""Tests for ``POST /document/post-ingest-context``.

We patch ``rag.retrieve.search`` so the route can be exercised without
Postgres or Voyage in scope. Mirrors the ``test_evidence_search_route``
patterns.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402


def _ensure_event_loop() -> None:
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _fake_snippets() -> list:
    from rag.retrieve import Snippet
    return [
        Snippet(
            chunk_id="ssc-2021-p1-000",
            source_id="ssc-2021",
            document_title="Surviving Sepsis Campaign 2021",
            section="SEPSIS HOUR-1 BUNDLE",
            page_number=1,
            indexed_version_date=_dt.date(2021, 10, 1),
            content="x" * 600,  # >400 to assert truncation
            relevance_score=0.92,
        )
    ]


def _lab_extraction_with_high_lactate() -> dict:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "p-1",
        "document_reference_id": "doc-1",
        "values": [
            {
                "test_name": "Lactate",
                "normalized_test_name": "lactate",
                "value": "4.2",
                "unit": "mmol/L",
                "abnormal_flag": "high",
                "citations": [{"source_type": "document"}],
            },
            {
                "test_name": "Sodium",
                "normalized_test_name": "sodium",
                "value": "140",
                "unit": "mmol/L",
                "abnormal_flag": "normal",
                "citations": [{"source_type": "document"}],
            },
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.9, 0.99],
        "extracted_at": "2024-01-01T00:00:00Z",
    }


def test_post_ingest_lab_returns_guidelines() -> None:
    _ensure_event_loop()
    import main as main_mod

    fake = _fake_snippets()
    mock = AsyncMock(return_value=fake)
    with patch("rag.retrieve.search", new=mock):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/post-ingest-context",
            json={
                "extraction": _lab_extraction_with_high_lactate(),
                "patient_id": "p-1",
                "document_reference_id": "doc-1",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query_used"], "expected non-empty query"
    assert "lactate" in body["query_used"].lower()
    # Retriever invoked exactly once.
    assert mock.await_count == 1
    assert len(body["guidelines"]) == 1
    g = body["guidelines"][0]
    assert g["chunk_id"] == "ssc-2021-p1-000"
    # 400-char truncation applied.
    assert len(g["content"]) == 400
    assert "lactate" in body["summary"].lower() or "abnormal" in body["summary"].lower()
    assert body["metadata"]["patient_id"] == "p-1"
    assert body["metadata"]["document_reference_id"] == "doc-1"
    assert "request_id" in body["metadata"]


def test_post_ingest_unrecognised_extraction_returns_empty_guidelines() -> None:
    _ensure_event_loop()
    import main as main_mod

    mock = AsyncMock(return_value=[])
    with patch("rag.retrieve.search", new=mock):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/post-ingest-context",
            json={
                "extraction": {"kind": "totally_unknown_shape"},
                "patient_id": "p-1",
                "document_reference_id": "doc-2",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["guidelines"] == []
    assert body["query_used"] == ""
    # Retriever NOT called when query is empty.
    assert mock.await_count == 0


def test_post_ingest_lab_with_no_abnormal_returns_empty_query() -> None:
    _ensure_event_loop()
    import main as main_mod

    mock = AsyncMock(return_value=[])
    extraction = _lab_extraction_with_high_lactate()
    for v in extraction["values"]:
        v["abnormal_flag"] = "normal"

    with patch("rag.retrieve.search", new=mock):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/post-ingest-context",
            json={
                "extraction": extraction,
                "patient_id": "p-1",
                "document_reference_id": "doc-3",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["query_used"] == ""
    assert body["guidelines"] == []
    assert "none flagged abnormal" in body["summary"].lower()
    assert mock.await_count == 0


def test_post_ingest_route_registered() -> None:
    _ensure_event_loop()
    import main as main_mod
    paths = {r.path for r in main_mod.app.routes}
    assert "/document/post-ingest-context" in paths
