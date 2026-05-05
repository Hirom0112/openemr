"""Phase 6.2 — for each W2 ``event_type`` value (W2_ARCHITECTURE §9.4),
assert one is emitted at the right boundary of a representative pipeline
run. Captures ``audit_writer.emit`` with AsyncMock at each module's local
import binding.

Coverage:

* ``document_ingested`` + ``document_extracted`` — happy /document/ingest.
* ``node_handoff`` (supervisor, extractor, retriever, critic, finalize) —
  one row per node when the node fires.
* ``critic_decision`` — alongside the critic's ``node_handoff``.
* ``demographic_check`` — emitted by the demographics node when
  document-side demographics are present.
* ``retrieval_completed`` — emitted by the retriever node alongside its
  ``node_handoff``.

The ingest watchdog event types (``document_processing_timeout``,
``document_extraction_abandoned``) and conflict types
(``intra_doc_conflict_detected``, ``record_evidence_contradiction``,
``classifier_verdict``) land in later phases and are not yet emitted.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _layout(bbox_id: str, text: str, conf: float = 1.0) -> dict[str, Any]:
    return {
        "bbox_id": bbox_id,
        "page": 1,
        "bbox": [0.0, 0.0, 100.0, 20.0],
        "text": text,
        "ocr_confidence": conf,
    }


def _lab_extraction(bbox_id: str = "p1-b001") -> dict[str, Any]:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/abc",
        "collection_facility": None,
        "values": [
            {
                "test_name": "Lactate",
                "normalized_test_name": "lactate",
                "value": "4.2",
                "unit": "mmol/L",
                "normalized_unit": "mmol/l",
                "reference_range": "0.5-2.0",
                "collection_date": "2024-01-15",
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/abc",
                        "page_or_section": "p1",
                        "field_or_chunk_id": bbox_id,
                        "quote_or_value": "4.2",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


# ── document_ingested + document_extracted ──────────────────────────────────


async def test_document_ingest_emits_ingested_and_extracted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import test_document_ingest as tdi

    pdf_path = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"
    pdf_bytes = pdf_path.read_bytes()
    mocks = tdi._patch_pipeline(monkeypatch)
    resp = await tdi._post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text

    types = [c.args[0].event_type for c in mocks["audit_emit"].await_args_list]
    assert "document_ingested" in types
    assert "document_extracted" in types


# ── node_handoff for each graph node ─────────────────────────────────────────


async def test_supervisor_emits_node_handoff() -> None:
    from graph.nodes.supervisor import supervisor
    from graph.state import make_initial_state

    state = make_initial_state(
        request_id="rid", session_id="sess", provider_id="prov"
    )
    state["message"] = "ingest"
    state["file_bytes_ref"] = "ref-1"

    with patch(
        "graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()
    ) as m:
        await supervisor(state)
    assert m.await_count >= 1
    assert any(c.args[0].event_type == "node_handoff" for c in m.await_args_list)


async def test_extractor_emits_node_handoff() -> None:
    from graph.nodes.extractor import extractor_node
    from graph.state import make_initial_state

    state = make_initial_state(
        request_id="rid", session_id="sess", provider_id="prov"
    )
    state["file_bytes_ref"] = "ref-1"

    with patch(
        "graph.nodes.extractor.audit_writer.emit", new=AsyncMock()
    ) as m:
        await extractor_node(state)
    assert any(c.args[0].event_type == "node_handoff" for c in m.await_args_list)


async def test_retriever_emits_node_handoff_and_retrieval_completed() -> None:
    from graph.nodes import retriever as _retr_mod
    from rag.retrieve import Snippet

    fake_snippets = [
        Snippet(
            chunk_id="c1",
            source_id="src",
            document_title="t",
            section=None,
            page_number=1,
            indexed_version_date=_dt.date(2012, 3, 1),
            content="x",
            relevance_score=0.5,
        )
    ]
    fake_stats = {
        "sparse_hits": 3,
        "dense_hits": 2,
        "after_rerank": 1,
        "rerank_used": False,
        "sparse_seconds": 0.0,
        "dense_seconds": 0.0,
        "merge_seconds": 0.0,
        "rerank_seconds": 0.0,
        "query_prefix": "any",
    }
    state = {
        "request_id": "rid",
        "session_id": "sess",
        "provider_id": "prov",
        "patient_id": "pt",
        "message": "any",
        "errors": [],
    }
    audit_mock = AsyncMock()
    with patch.object(
        _retr_mod, "audit_writer", new=type("X", (), {"emit": audit_mock})()
    ):
        with patch("rag.retrieve.search", new=AsyncMock(return_value=fake_snippets)):
            with patch(
                "rag.retrieve.get_last_retrieval_stats", return_value=fake_stats
            ):
                await _retr_mod.retriever_node(state)  # type: ignore[arg-type]
    types = {c.args[0].event_type for c in audit_mock.await_args_list}
    assert "node_handoff" in types
    assert "retrieval_completed" in types


async def test_finalize_emits_node_handoff() -> None:
    from graph.nodes.finalize import finalize_node
    from graph.state import make_initial_state

    state = make_initial_state(
        request_id="rid", session_id="sess", provider_id="prov"
    )
    state["critic_decision"] = "pass"

    with patch(
        "graph.nodes.finalize.audit_writer.emit", new=AsyncMock()
    ) as m:
        await finalize_node(state)
    assert any(c.args[0].event_type == "node_handoff" for c in m.await_args_list)


# ── critic_decision (alongside the critic's node_handoff) ────────────────────


async def test_critic_emits_critic_decision() -> None:
    from graph.nodes.critic import critic_node
    from graph.state import make_initial_state

    state = make_initial_state(
        request_id="rid", session_id="sess", provider_id="prov"
    )
    state["extraction"] = _lab_extraction()
    state["ocr_layout"] = [_layout("p1-b001", "Lactate 4.2 mmol/L")]

    with patch(
        "graph.nodes.critic.audit_writer.emit", new=AsyncMock()
    ) as m:
        await critic_node(state)
    types = {c.args[0].event_type for c in m.await_args_list}
    assert "critic_decision" in types
    assert "node_handoff" in types


# ── demographic_check ────────────────────────────────────────────────────────


async def test_demographics_emits_demographic_check() -> None:
    from graph.nodes.demographics import demographics_node

    chart_patient = {
        "id": "pt-1",
        "birthDate": "1962-03-14",
        "name": [{"given": ["Jane"], "family": "Doe"}],
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": "12345"}
        ],
    }

    async def _provider(_pid: str) -> dict[str, Any]:
        return chart_patient

    extraction = _lab_extraction()
    extraction["demographics"] = {
        "mrn": "12345",
        "name": "Jane Doe",
        "dob": "1962-03-14",
    }
    state = {
        "request_id": "rid-d",
        "session_id": "sess-d",
        "provider_id": "prov-d",
        "patient_id": "pt-1",
        "extraction": extraction,
        "errors": [],
    }
    with patch(
        "graph.nodes.demographics.audit_writer.emit", new=AsyncMock()
    ) as m:
        await demographics_node(state, fhir_patient_provider=_provider)  # type: ignore[arg-type]
    types = {c.args[0].event_type for c in m.await_args_list}
    assert "demographic_check" in types
