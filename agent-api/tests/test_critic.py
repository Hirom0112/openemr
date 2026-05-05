"""Tests for graph.nodes.critic (W2_ARCHITECTURE §5.8).

Covers the document path (schema, citation presence, resolvability,
fidelity, OCR-confidence skip), the structured-data path (W1 reuse),
demographic fold-in, and the audit emission contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from graph.nodes.critic import critic_node
from graph.state import make_initial_state
from verification.dispatcher_response import VerificationResult

pytestmark = pytest.mark.hard_failure


# ── Fixture builders ─────────────────────────────────────────────────────────


def _state(**overrides: Any) -> dict[str, Any]:
    state = make_initial_state(
        request_id="req-1", session_id="sess-1", provider_id="prov-1"
    )
    state.update(overrides)  # type: ignore[arg-type]
    return state  # type: ignore[return-value]


def _layout_block(bbox_id: str, text: str, conf: float = 1.0) -> dict[str, Any]:
    return {
        "bbox_id": bbox_id,
        "page": 1,
        "bbox": [0.0, 0.0, 100.0, 20.0],
        "text": text,
        "ocr_confidence": conf,
    }


def _lab_report(
    *,
    test_name: str = "Lactate",
    value: str = "4.2",
    bbox_id: str = "p1-b001",
    quote: str = "4.2",
    citations_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    citations = citations_override
    if citations is None:
        citations = [
            {
                "source_type": "document",
                "source_id": "DocumentReference/abc",
                "page_or_section": "p1",
                "field_or_chunk_id": bbox_id,
                "quote_or_value": quote,
            }
        ]
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/abc",
        "collection_facility": None,
        "values": [
            {
                "test_name": test_name,
                "normalized_test_name": test_name.lower(),
                "value": value,
                "unit": "mmol/L",
                "normalized_unit": "mmol/l",
                "reference_range": "0.5-2.0",
                "collection_date": "2024-01-15",
                "abnormal_flag": "high",
                "citations": citations,
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


def _unknown_doc(bbox_id: str = "p1-b001") -> dict[str, Any]:
    return {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/xyz",
        "document_kind_guess": "referral letter",
        "summary": "summary",
        "key_facts": [
            {
                "text": "fact one",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/xyz",
                        "page_or_section": "p1",
                        "field_or_chunk_id": bbox_id,
                        "quote_or_value": "fact one",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.55,
        "ocr_confidence_range": [0.9, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


# ── Document-path tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_critic_passes_clean_lab_report() -> None:
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2 mmol/L")],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "pass"
    assert out["critic_violations"] == []


@pytest.mark.asyncio
async def test_critic_hard_blocks_unresolvable_bbox() -> None:
    state = _state(
        extraction=_lab_report(bbox_id="p9-b999"),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "hard_block"
    assert "CITATION_UNRESOLVABLE" in out["critic_violations"]


@pytest.mark.asyncio
async def test_critic_hard_blocks_fabricated_quote() -> None:
    state = _state(
        extraction=_lab_report(quote="99.9"),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "hard_block"
    assert "CITATION_FIDELITY_FAILED" in out["critic_violations"]


@pytest.mark.asyncio
async def test_critic_skips_fidelity_when_ocr_low() -> None:
    state = _state(
        extraction=_lab_report(quote="99.9"),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2", conf=0.4)],
    )
    out = await critic_node(state)
    assert out["critic_decision"] != "hard_block"
    assert "CITATION_FIDELITY_FAILED" not in out["critic_violations"]
    assert any(w.get("code") == "OCR_LOW_CONFIDENCE" for w in out["soft_warns"])


@pytest.mark.asyncio
async def test_critic_passes_through_unknown_document() -> None:
    state = _state(
        extraction=_unknown_doc(),
        ocr_layout=[_layout_block("p1-b001", "fact one is mentioned here")],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "pass"


@pytest.mark.asyncio
async def test_critic_hard_blocks_missing_citations() -> None:
    # Schema requires min_length=1 — passing []  triggers SCHEMA_INVALID rather
    # than CITATION_MISSING. Both are hard_block; we accept either to honour
    # the spec.
    extraction = _lab_report(citations_override=[])
    state = _state(
        extraction=extraction,
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "hard_block"
    assert any(
        v in out["critic_violations"] for v in ("CITATION_MISSING", "SCHEMA_INVALID")
    )


# ── Demographic fold-in ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_critic_hard_blocks_on_demographic_hard_block() -> None:
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
        demographic_check={
            "decision": "hard_block",
            "reason_code": "MRN_MISMATCH",
            "message": "MRN on document does not match chart",
        },
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "hard_block"
    assert "MRN_MISMATCH" in out["critic_violations"]


@pytest.mark.asyncio
async def test_critic_appends_demographic_softwarn() -> None:
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
        demographic_check={
            "decision": "soft_warn",
            "reason_code": "MRN_MATCH_DOB_MISMATCH",
            "message": "DOB on document differs from chart — verify",
        },
    )
    out = await critic_node(state)
    assert out["critic_decision"] in ("pass", "soft_warn")
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "MRN_MATCH_DOB_MISMATCH" in codes


# ── Structured-data path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_critic_reuses_w1_verification_on_structured_path() -> None:
    fake = VerificationResult(
        passed=False,
        blocked=False,
        violations=["NKDA_BLOCK: ..."],
        modified_response={"narrative": "(stripped)"},
    )
    state = _state(
        structured_response={"narrative": "no known allergies", "data": {}},
        patient_id="PT-1",
    )
    with patch(
        "graph.nodes.critic.verify_dispatcher_response", return_value=fake
    ) as mock:
        out = await critic_node(state)
    assert mock.called
    assert out["critic_decision"] == "soft_warn"
    assert any("NKDA_BLOCK" in v for v in out["critic_violations"])
    assert out["structured_response"] == {"narrative": "(stripped)"}


@pytest.mark.asyncio
async def test_critic_blocks_on_w1_blocked_response() -> None:
    fake = VerificationResult(
        passed=False,
        blocked=True,
        violations=["SYSTEM_BOUNDARY_TOKEN detected"],
        modified_response={},
        physician_message="Response blocked by safety check.",
    )
    state = _state(
        structured_response={"narrative": "x", "data": {}},
        patient_id="PT-1",
    )
    with patch(
        "graph.nodes.critic.verify_dispatcher_response", return_value=fake
    ):
        out = await critic_node(state)
    assert out["critic_decision"] == "hard_block"


# ── Audit emission ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_critic_emits_audit_row() -> None:
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
    )
    with patch(
        "graph.nodes.critic.audit_writer.emit", new=AsyncMock()
    ) as mock_emit:
        await critic_node(state)
    assert mock_emit.await_count == 1
    event = mock_emit.await_args.args[0]
    assert event.event_type == "node_handoff"
    assert event.detail_json["from_node"] == "critic"
    assert event.detail_json["to_node"] == "finalize"
    assert event.detail_json["decision"] in ("pass", "soft_warn", "hard_block")
    assert "violation_codes" in event.detail_json
    # Sanity: no clinical text strings in the detail payload (block-list
    # words from audit.writer would also catch this in production).
    serialized = str(event.detail_json).lower()
    for forbidden in ("lactate", "patient", "narrative", "complaint"):
        assert forbidden not in serialized
