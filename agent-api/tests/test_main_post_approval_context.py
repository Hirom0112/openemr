"""Tests for ``POST /document/{id}/post-approval-context`` — fact_citations.

Covers the deterministic citation-resolution map the frontend uses to open
the bbox viewer / read-only review panel on synthesis-card chip clicks.
The post-approval route fans out RAG against approved facts; we patch the
MySQL/Postgres readers + the retriever so the route is exercisable without
a database in scope.
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
            section="HOUR-1 BUNDLE",
            page_number=3,
            indexed_version_date=_dt.date(2021, 10, 1),
            content="Lactate >2 mmol/L should prompt source control...",
            relevance_score=0.91,
        )
    ]


def _fake_obs_rows() -> list[dict]:
    """One approved Observation row matching the readback shape."""
    return [
        {
            "id": 4242,
            "fhir_resource": {
                "id": "copilot-457-24323-8-alt",
                "resourceType": "Observation",
                "code": {
                    "coding": [
                        {"system": "http://loinc.org", "code": "2524-7", "display": "Lactate"}
                    ]
                },
                "valueQuantity": {"value": 4.2, "unit": "mmol/L"},
            },
            "loinc_code": "2524-7",
            "display": "Lactate",
            "value": 4.2,
        }
    ]


def test_post_approval_context_returns_fact_citations_for_obs_and_guidelines() -> None:
    """fact_citations map covers every emitted citation_id token.

    The synthesis emits ``fact:obs:{row_id}`` and ``guideline:{chunk_id}``
    tokens; the frontend uses fact_citations to deterministically open
    a bbox overlay. Verify both shapes.
    """
    _ensure_event_loop()
    import main as main_mod

    obs_rows = _fake_obs_rows()
    snippets = _fake_snippets()

    obs_mock = AsyncMock(return_value=obs_rows)
    rag_mock = AsyncMock(return_value=snippets)
    pg_pool_mock = AsyncMock(return_value=None)

    with patch("observations.writer.read_observations_for_document", new=obs_mock), \
         patch("rag.retrieve.search", new=rag_mock), \
         patch("audit.writer.get_pool", new=pg_pool_mock):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/copilot-457/post-approval-context",
            json={"patient_id": "p-1"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "fact_citations" in body, "expected fact_citations key in response"
    fc = body["fact_citations"]
    assert isinstance(fc, dict)

    # Observation citation: token is `fact:obs:{row_id}`.
    obs_cid = "fact:obs:4242"
    assert obs_cid in fc, f"missing obs citation: {sorted(fc.keys())}"
    obs_entry = fc[obs_cid]
    assert obs_entry["source_type"] == "observation"
    # source_id should be a FHIR ref derived from the resource id.
    assert obs_entry["source_id"] == "Observation/copilot-457-24323-8-alt"
    assert obs_entry["field_or_chunk_id"] == "4242"
    assert "Lactate" in (obs_entry["quote_or_value"] or "")
    # bbox/page absent (DB readback gap documented in the writer).
    assert obs_entry["bbox"] is None
    assert obs_entry["page"] is None

    # Guideline citation token.
    guide_cid = "guideline:ssc-2021-p1-000"
    assert guide_cid in fc
    g = fc[guide_cid]
    assert g["source_type"] == "guideline"
    assert g["source_id"] == "ssc-2021"
    assert g["field_or_chunk_id"] == "ssc-2021-p1-000"
    # page_or_section prefers section name over page number.
    assert g["page_or_section"] == "HOUR-1 BUNDLE"
    assert g["page"] == 3
    assert "Lactate" in g["quote_or_value"]


def test_post_approval_context_hydrates_obs_bbox_from_citations_column() -> None:
    """When the obs row carries a non-empty citations list, fact_citations
    for that obs token must surface bbox/page/field_or_chunk_id/quote from
    the first citation — this is what closes the loop so synthesis-card
    fact:obs chips draw the source rectangle on the PDF.
    """
    _ensure_event_loop()
    import main as main_mod

    obs_rows = [
        {
            "id": 4242,
            "fhir_resource": {
                "id": "copilot-457-24323-8-alt",
                "resourceType": "Observation",
                "code": {
                    "coding": [
                        {
                            "system": "http://loinc.org",
                            "code": "2524-7",
                            "display": "Lactate",
                        }
                    ]
                },
                "valueQuantity": {"value": 4.2, "unit": "mmol/L"},
            },
            "loinc_code": "2524-7",
            "display": "Lactate",
            "value": 4.2,
            "citations": [
                {
                    "source_type": "document",
                    "source_id": "DocumentReference/copilot-457",
                    "page_or_section": "p2",
                    "page": 2,
                    "bbox": [0.12, 0.34, 0.56, 0.42],
                    "field_or_chunk_id": "lactate-line-3",
                    "quote_or_value": "Lactate 4.2 mmol/L",
                }
            ],
        }
    ]
    snippets: list = []

    obs_mock = AsyncMock(return_value=obs_rows)
    rag_mock = AsyncMock(return_value=snippets)
    pg_pool_mock = AsyncMock(return_value=None)

    with patch("observations.writer.read_observations_for_document", new=obs_mock), \
         patch("rag.retrieve.search", new=rag_mock), \
         patch("audit.writer.get_pool", new=pg_pool_mock):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/copilot-457/post-approval-context",
            json={"patient_id": "p-1"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    fc = body["fact_citations"]
    obs_entry = fc["fact:obs:4242"]

    # Routing source_type stays "observation" (chip handler routes on this).
    assert obs_entry["source_type"] == "observation"
    # bbox/page propagated from the citations column — this is the bug fix.
    assert obs_entry["bbox"] == [0.12, 0.34, 0.56, 0.42]
    assert obs_entry["page"] == 2
    assert obs_entry["page_or_section"] == "p2"
    # field_or_chunk_id prefers the citation's value (frontend keys the
    # readOnly review panel by it), not the integer row id.
    assert obs_entry["field_or_chunk_id"] == "lactate-line-3"
    assert obs_entry["quote_or_value"] == "Lactate 4.2 mmol/L"


def test_post_approval_context_obs_falls_back_when_citations_missing() -> None:
    """No citations column → preserve the prior fallback shape (bbox=None,
    page=None, field_or_chunk_id = row id as string, quote = display+value).
    Guards regression in the path that existed before the fix.
    """
    _ensure_event_loop()
    import main as main_mod

    rows_no_cits = _fake_obs_rows()  # no "citations" key
    obs_mock = AsyncMock(return_value=rows_no_cits)
    rag_mock = AsyncMock(return_value=[])
    pg_pool_mock = AsyncMock(return_value=None)

    with patch("observations.writer.read_observations_for_document", new=obs_mock), \
         patch("rag.retrieve.search", new=rag_mock), \
         patch("audit.writer.get_pool", new=pg_pool_mock):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/copilot-457/post-approval-context",
            json={"patient_id": "p-1"},
        )

    assert resp.status_code == 200, resp.text
    fc = resp.json()["fact_citations"]
    obs_entry = fc["fact:obs:4242"]
    assert obs_entry["bbox"] is None
    assert obs_entry["page"] is None
    assert obs_entry["field_or_chunk_id"] == "4242"
    assert obs_entry["source_id"] == "Observation/copilot-457-24323-8-alt"
    assert "Lactate" in obs_entry["quote_or_value"]


def test_post_approval_context_handles_no_approved_facts() -> None:
    """When there are no approved obs/intake rows fact_citations stays empty."""
    _ensure_event_loop()
    import main as main_mod

    obs_mock = AsyncMock(return_value=[])
    rag_mock = AsyncMock(return_value=[])
    pg_pool_mock = AsyncMock(return_value=None)

    with patch("observations.writer.read_observations_for_document", new=obs_mock), \
         patch("rag.retrieve.search", new=rag_mock), \
         patch("audit.writer.get_pool", new=pg_pool_mock):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/copilot-99/post-approval-context",
            json={"patient_id": "p-1"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # fact_citations always present (frontend assumes the key exists).
    assert body["fact_citations"] == {}
    assert body["guidelines"] == []
