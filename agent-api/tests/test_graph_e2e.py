"""Slice 3.9 — End-to-end graph integration tests.

Compiles the W2 graph with real (non-stub) demographics + critic nodes and
exercises the full pipeline through ``ainvoke``. Each test patches the
external boundaries (FHIR fetch, dispatcher, lab.extract) but otherwise
runs the actual graph topology.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from graph import compile_graph, make_initial_state

pytestmark = pytest.mark.hard_failure


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_lab_extraction(
    *,
    document_reference_id: str = "doc-e2e-1",
    demographics: dict | None = None,
) -> dict:
    """Return a serialized LabReport extraction (matches lab.extract output)."""
    extraction = {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": document_reference_id,
        "collection_facility": "OSH",
        "values": [
            {
                "test_name": "Lactate",
                "normalized_test_name": "lactate",
                "value": "4.2 mmol/L",
                "unit": "mmol/L",
                "normalized_unit": "mmol/L",
                "reference_range": "0.5-2.2",
                "collection_date": None,
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": document_reference_id,
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-b001",
                        "quote_or_value": "4.2 mmol/L",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.85, 0.99],
        "extracted_at": _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc).isoformat(),
    }
    if demographics is not None:
        extraction["demographics"] = demographics
    return extraction


class _StubExtraction:
    """Object with .model_dump() — mirrors LabReport surface for the extractor node."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "json") -> dict:
        return self._payload


def _ocr_layout_for_extraction() -> list[dict]:
    """OCR layout that resolves the citation in ``_make_lab_extraction``."""
    return [
        {
            "bbox_id": "p1-b001",
            "text": "Lactate 4.2 mmol/L",
            "ocr_confidence": 0.92,
            "page": 1,
        }
    ]


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_graph_document_path_e2e() -> None:
    """File upload → extractor → demographics (pass) → critic (pass) → finalize."""
    document_demographics = {
        "mrn": "MRN-1",
        "name": "Jane Doe",
        "dob": "1980-01-01",
    }
    extraction_payload = _make_lab_extraction(demographics=document_demographics)

    chart_patient = {
        "id": "pt-1",
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-1"},
        ],
        "name": [{"given": ["Jane"], "family": "Doe"}],
        "birthDate": "1980-01-01",
    }

    async def _file_bytes_provider(_ref: str) -> bytes:
        return b"fake-pdf-bytes"

    async def _fhir_patient_provider(_pid: str) -> dict:
        return chart_patient

    with patch(
        "graph.nodes.extractor.extract",
        new=AsyncMock(return_value=_StubExtraction(extraction_payload)),
    ), patch("graph.nodes.extractor.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.demographics.audit_writer.emit", new=AsyncMock()
    ), patch("graph.nodes.critic.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()
    ):
        compiled = compile_graph(
            checkpointer=MemorySaver(),
            file_bytes_provider=_file_bytes_provider,
            fhir_patient_provider=_fhir_patient_provider,
        )
        initial = make_initial_state(
            request_id="req-e2e-doc",
            session_id="sess-e2e-doc",
            provider_id="prov-1",
            patient_id="pt-1",
            file_bytes_ref="ref-1",
        )
        # Inject ocr_layout so the critic's citation-fidelity check passes.
        initial["ocr_layout"] = _ocr_layout_for_extraction()
        config = {"configurable": {"thread_id": "sess-e2e-doc"}}
        final = await compiled.ainvoke(initial, config=config)

    assert final.get("extraction") is not None
    assert final["extraction"]["kind"] == "lab_report"
    assert final["demographic_check"]["decision"] == "pass"
    assert final["critic_decision"] == "pass"
    assert "finalized" in final


@pytest.mark.asyncio
async def test_full_graph_demographics_hardblock_short_circuits() -> None:
    """Mismatched MRN → demographics hard_block → critic hard_block."""
    document_demographics = {
        "mrn": "MRN-2",  # different from chart
        "name": "Jane Doe",
        "dob": "1980-01-01",
    }
    extraction_payload = _make_lab_extraction(demographics=document_demographics)

    chart_patient = {
        "id": "pt-1",
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-1"},
        ],
        "name": [{"given": ["Jane"], "family": "Doe"}],
        "birthDate": "1980-01-01",
    }

    async def _file_bytes_provider(_ref: str) -> bytes:
        return b"fake-pdf-bytes"

    async def _fhir_patient_provider(_pid: str) -> dict:
        return chart_patient

    with patch(
        "graph.nodes.extractor.extract",
        new=AsyncMock(return_value=_StubExtraction(extraction_payload)),
    ), patch("graph.nodes.extractor.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.demographics.audit_writer.emit", new=AsyncMock()
    ), patch("graph.nodes.critic.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()
    ):
        compiled = compile_graph(
            checkpointer=MemorySaver(),
            file_bytes_provider=_file_bytes_provider,
            fhir_patient_provider=_fhir_patient_provider,
        )
        initial = make_initial_state(
            request_id="req-e2e-mismatch",
            session_id="sess-e2e-mismatch",
            provider_id="prov-1",
            patient_id="pt-1",
            file_bytes_ref="ref-1",
        )
        initial["ocr_layout"] = _ocr_layout_for_extraction()
        config = {"configurable": {"thread_id": "sess-e2e-mismatch"}}
        final = await compiled.ainvoke(initial, config=config)

    assert final["demographic_check"]["decision"] == "hard_block"
    assert final["demographic_check"]["reason_code"] == "MRN_MISMATCH"
    assert final["critic_decision"] == "hard_block"
    assert "MRN_MISMATCH" in final.get("critic_violations", [])


@pytest.mark.asyncio
async def test_full_graph_structured_path_e2e() -> None:
    """message='show vitals' → structured → critic (W1 verification) → finalize."""
    stub_response = {
        "narrative": "Latest vitals on file.",
        "data": {"hr": 72},
        "citations": [],
    }
    with patch(
        "agent.dispatcher.dispatch", new=AsyncMock(return_value=stub_response)
    ), patch("graph.nodes.structured.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.critic.audit_writer.emit", new=AsyncMock()
    ), patch("graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()):
        compiled = compile_graph(checkpointer=MemorySaver())
        initial = make_initial_state(
            request_id="req-e2e-struct",
            session_id="sess-e2e-struct",
            provider_id="prov-1",
            message="show vitals",
        )
        config = {"configurable": {"thread_id": "sess-e2e-struct"}}
        final = await compiled.ainvoke(initial, config=config)

    # The W1 verifier may have stripped/modified the response; structured_response
    # in the finalized envelope is whatever the critic emitted (the modified one).
    assert final["critic_decision"] == "pass"
    assert "finalized" in final
    assert final["finalized"]["structured_response"] is not None
