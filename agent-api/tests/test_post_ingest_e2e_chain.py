"""End-to-end smoke: post-ingest-context → document chat chain.

Exercises the full Sara flow that wasn't previously gated:

    /document/ingest (assumed) → /document/post-ingest-context → /document/{id}/chat

Skips /document/ingest itself (covered by other integration tests + needs
FHIR/audit/Redis). Starts from a realistic ingest response payload, runs it
through the post-ingest endpoint, then feeds the resulting guidelines into
the chat endpoint and asserts the contract holds end-to-end.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402


def _fake_snippets() -> list:
    from rag.retrieve import Snippet
    return [
        Snippet(
            chunk_id="ssc-2021-p1-000",
            source_id="ssc-2021",
            document_title="Surviving Sepsis Campaign 2021",
            section="HOUR-1 BUNDLE",
            page_number=1,
            indexed_version_date=_dt.date(2021, 10, 1),
            content=(
                "Initiate broad-spectrum antibiotics within 1 hour of recognition. "
                "Obtain blood cultures before antibiotics when feasible. "
                "Begin 30 mL/kg crystalloid for hypotension or lactate >= 4."
            ),
            relevance_score=0.92,
        ),
        Snippet(
            chunk_id="ssc-2021-p2-001",
            source_id="ssc-2021",
            document_title="Surviving Sepsis Campaign 2021",
            section="LACTATE GUIDANCE",
            page_number=2,
            indexed_version_date=_dt.date(2021, 10, 1),
            content="Re-measure lactate within 2-4 hours if initial value > 2 mmol/L.",
            relevance_score=0.88,
        ),
    ]


def _realistic_ingest_response() -> dict:
    """Mirrors the shape /document/ingest emits for a high-lactate lab PDF.

    Field set is intentionally minimal but matches the contract the UI
    consumes (extraction kind/values/citations + document_reference_id +
    metadata.fhir_write_path).
    """
    return {
        "document_reference_id": "abc-123",
        "extraction_id": "ext-001",
        "extraction": {
            "kind": "lab_report",
            "schema_version": "1.0",
            "patient_id": "patient-42",
            "document_reference_id": "abc-123",
            "classifier_confidence": 0.93,
            "ocr_confidence_range": [0.91, 0.99],
            "values": [
                {
                    "test_name": "Lactate",
                    "normalized_test_name": "lactate",
                    "value": "4.2",
                    "unit": "mmol/L",
                    "abnormal_flag": "high",
                    "citations": [{"source_type": "document", "page_number": 1}],
                },
                {
                    "test_name": "WBC",
                    "normalized_test_name": "wbc",
                    "value": "18.4",
                    "unit": "10^3/uL",
                    "abnormal_flag": "high",
                    "citations": [{"source_type": "document", "page_number": 1}],
                },
            ],
        },
        "citations": [],
        "bbox_layout": [],
        "soft_warns": [],
        "metadata": {
            "fhir_write_path": "live",
            "request_id": "req-test-001",
            "size_bytes": 12345,
            "page_count": 1,
        },
    }


def test_post_ingest_then_chat_chain() -> None:
    """Real flow: take ingest output → context endpoint → chat endpoint.

    Asserts:
      - post-ingest produces a non-empty query, calls retriever, returns
        guidelines + summary.
      - chat endpoint accepts that exact guidelines list as input and
        produces an answer with parsed citations.
      - The chunk_id from the retriever survives end-to-end (guideline in
        post-ingest output → cited in chat output).
    """
    import main  # noqa: F401  side-effect import registers routes

    ingest_resp = _realistic_ingest_response()
    doc_ref_id = ingest_resp["document_reference_id"]
    extraction = ingest_resp["extraction"]
    patient_id = extraction["patient_id"]

    # Mock retriever with two realistic guideline snippets.
    rag_mock = AsyncMock(return_value=_fake_snippets())

    # Mock Anthropic so the chat endpoint doesn't need a real API key.
    # Answer references the chunk_id from the retriever output to assert
    # the chain wires through correctly.
    fake_message = SimpleNamespace(content=[SimpleNamespace(text=(
        "Patient has elevated lactate (4.2 mmol/L) and leukocytosis. "
        "Per [G:ssc-2021-p1-000], initiate broad-spectrum antibiotics within "
        "1 hour and re-measure lactate per [G:ssc-2021-p2-001]. "
        "Source value: [D:lactate]."
    ), type="text")])
    fake_anthropic_client = MagicMock()
    fake_anthropic_client.messages = MagicMock()
    fake_anthropic_client.messages.create = AsyncMock(return_value=fake_message)

    with patch("rag.retrieve.search", rag_mock), \
         patch("anthropic.AsyncAnthropic", return_value=fake_anthropic_client):
        client = TestClient(main.app)

        # Step 1: post-ingest-context
        ctx_resp = client.post(
            "/document/post-ingest-context",
            json={
                "extraction": extraction,
                "patient_id": patient_id,
                "document_reference_id": doc_ref_id,
            },
        )
        assert ctx_resp.status_code == 200, ctx_resp.text
        ctx = ctx_resp.json()

        # Contract assertions on post-ingest output
        assert ctx["summary"], "summary must be non-empty"
        assert ctx["query_used"], "query must be derived from extraction"
        assert "lactate" in ctx["query_used"].lower(), \
            f"high-lactate lab should drive query; got {ctx['query_used']!r}"
        assert len(ctx["guidelines"]) == 2, \
            f"expected 2 snippets from mocked retriever, got {len(ctx['guidelines'])}"
        guideline_ids = [g["chunk_id"] for g in ctx["guidelines"]]
        assert "ssc-2021-p1-000" in guideline_ids
        assert rag_mock.await_count == 1, "retriever must be called exactly once"

        # Step 2: chat endpoint, feeding post-ingest output as context
        chat_resp = client.post(
            f"/document/{doc_ref_id}/chat",
            json={
                "patient_id": patient_id,
                "question": "What's the immediate next step for this patient?",
                "extraction": extraction,
                "guidelines": ctx["guidelines"],
                "history": [],
            },
        )
        assert chat_resp.status_code == 200, chat_resp.text
        chat = chat_resp.json()

        # Contract assertions on chat output
        assert chat["answer"], "chat must return non-empty answer"
        assert "antibiotics" in chat["answer"].lower(), "answer should reflect guideline"
        assert "G:ssc-2021-p1-000" in chat["citations_used"], \
            f"chunk_id from step 1 must round-trip to step 2 citations: {chat['citations_used']!r}"
        assert "D:lactate" in chat["citations_used"], \
            "doc-field citation must be parsed from answer"

        # Anthropic was actually invoked
        assert fake_anthropic_client.messages.create.call_count == 1


def test_post_ingest_chain_handles_empty_guidelines() -> None:
    """Unrecognized extraction → empty guidelines → chat still works.

    Sara might ingest something with no abnormal lab findings; the flow
    must degrade gracefully — empty guidelines (200, not 500) and chat
    still answers from the extraction alone.
    """
    import main

    ingest_resp = _realistic_ingest_response()
    extraction = ingest_resp["extraction"]
    # Strip abnormal flags so query builder produces nothing useful
    for v in extraction["values"]:
        v["abnormal_flag"] = "normal"
    doc_ref_id = ingest_resp["document_reference_id"]

    rag_mock = AsyncMock(return_value=[])

    fake_message = SimpleNamespace(content=[SimpleNamespace(text=(
        "Lab values are within normal range. No specific guideline triggered. [D:lactate]"
    ), type="text")])
    fake_anthropic_client = MagicMock()
    fake_anthropic_client.messages = MagicMock()
    fake_anthropic_client.messages.create = AsyncMock(return_value=fake_message)

    with patch("rag.retrieve.search", rag_mock), \
         patch("anthropic.AsyncAnthropic", return_value=fake_anthropic_client):
        client = TestClient(main.app)

        ctx_resp = client.post(
            "/document/post-ingest-context",
            json={
                "extraction": extraction,
                "patient_id": extraction["patient_id"],
                "document_reference_id": doc_ref_id,
            },
        )
        assert ctx_resp.status_code == 200
        ctx = ctx_resp.json()
        assert ctx["guidelines"] == []
        # Summary should still synthesize from the extraction
        assert ctx["summary"]

        chat_resp = client.post(
            f"/document/{doc_ref_id}/chat",
            json={
                "patient_id": extraction["patient_id"],
                "question": "Anything to worry about?",
                "extraction": extraction,
                "guidelines": [],
                "history": [],
            },
        )
        assert chat_resp.status_code == 200
        assert chat_resp.json()["answer"]
