"""Phase 5A' regression — soft_warn decision escalation.

Pre-fix bug: ``critic_node`` accumulated soft-warns from three branches
(OCR low confidence, demographic soft-warn fold-in, stale-guideline) but
never escalated the categorical ``decision`` from ``"pass"`` to
``"soft_warn"``. ``correct_critic_decision`` is a string-equality rubric
(``evals.rubrics_mechanical.correct_critic_decision`` line 713), so the
40 fixture cases that expect ``soft_warn`` were structurally
unreachable. Per W2_ARCHITECTURE §5.6/§5.8 a populated ``soft_warns``
list is by definition a soft-warn outcome.

These tests pin the escalation rule:

  - decision starts as "pass" + soft_warns non-empty  -> escalate to
    "soft_warn"
  - decision starts as "hard_block" + soft_warns non-empty -> stays
    "hard_block" (never downgraded)
  - decision starts as "pass" + soft_warns empty     -> stays "pass"

The first assertion is the one that fails without the fix.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph.nodes.critic import critic_node
from graph.state import make_initial_state

pytestmark = pytest.mark.hard_failure


def _state(**overrides: Any) -> dict[str, Any]:
    state = make_initial_state(
        request_id="req-esc", session_id="sess-esc", provider_id="prov-esc"
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


def _lab_report(*, bbox_id: str = "p1-b001", quote: str = "4.2") -> dict[str, Any]:
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
                        "quote_or_value": quote,
                    }
                ],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_pass_with_demographic_softwarn_escalates_to_soft_warn() -> None:
    """Demographic comparator returned soft_warn -> decision must escalate."""
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
        demographic_check={
            "decision": "soft_warn",
            "reason_code": "MRN_MATCH_DOB_MISMATCH",
            "message": "DOB on document differs from chart - verify",
        },
    )
    out = await critic_node(state)
    # Pre-fix this returned "pass" — fixture expectation was unreachable.
    assert out["critic_decision"] == "soft_warn"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "MRN_MATCH_DOB_MISMATCH" in codes


@pytest.mark.asyncio
async def test_pass_with_low_ocr_confidence_escalates_to_soft_warn() -> None:
    """OCR_LOW_CONFIDENCE soft-warn appended in _check_document_path."""
    # Quote does NOT match block text but OCR conf is below threshold, so
    # fidelity check is skipped and OCR_LOW_CONFIDENCE is appended.
    state = _state(
        extraction=_lab_report(quote="99.9"),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2", conf=0.4)],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "soft_warn"
    assert any(w.get("code") == "OCR_LOW_CONFIDENCE" for w in out["soft_warns"])


@pytest.mark.asyncio
async def test_hard_block_with_softwarns_stays_hard_block() -> None:
    """Negative case: never downgrade hard_block to soft_warn."""
    # Demographic comparator hard-blocks. Pre-existing soft_warn in state
    # (e.g. from upstream classifier confidence) should not downgrade the
    # decision.
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
        soft_warns=[{"code": "classifier_confidence_low", "message": "x"}],
        demographic_check={
            "decision": "hard_block",
            "reason_code": "MRN_MISMATCH",
            "message": "MRN on document does not match chart",
        },
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "hard_block"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "classifier_confidence_low" in codes


@pytest.mark.asyncio
async def test_pass_with_no_softwarns_stays_pass() -> None:
    """Negative case: empty soft_warns must not promote pass to soft_warn."""
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "pass"
    assert out["soft_warns"] == []


@pytest.mark.asyncio
async def test_preexisting_state_softwarn_alone_escalates() -> None:
    """Upstream node (classifier, extractor) appended soft_warn -> escalate.

    Many fixture cases expect ``classifier_confidence_low`` or
    ``mixed_content_detected`` from upstream nodes. The critic carries
    those forward via ``state.get("soft_warns")`` at line 302; the
    escalation rule must apply to them too.
    """
    state = _state(
        extraction=_lab_report(),
        ocr_layout=[_layout_block("p1-b001", "Lactate 4.2")],
        soft_warns=[
            {"code": "classifier_confidence_low", "message": "uncertain class"}
        ],
    )
    out = await critic_node(state)
    assert out["critic_decision"] == "soft_warn"
