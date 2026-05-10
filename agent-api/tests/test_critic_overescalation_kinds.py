"""Phase 3 Type-D — over-escalation regression on multimodal kinds.

Background: the HL7-v2 / XLSX / DOCX multimodal lanes don't carry
rasterised OCR layouts; their citations point at structured locators
(segment paths, sheet/row/col, paragraph indexes). Several fixture buckets
("hl7_oru_nominal_*", "xlsx_full_*", "docx_referral_*") expect the critic
to ``pass`` cleanly when the extraction is structurally well-formed.

Pre-fix risk: the kind allowlist in ``_validate_schema`` returns
``unsupported_kind`` for any kind outside {lab_report, unknown,
intake_form, workbook}, which the document path routes to ``hard_block``
SCHEMA_INVALID. Today the HL7 ORU parser emits ``kind="lab_report"``
(parsers/hl7/oru.py:245) and the XLSX wrapper emits ``kind="workbook"``
so they actually do pass the allowlist — these tests pin that contract
and guard against future regressions that would re-introduce
hard_block over-escalation on the multimodal lanes.

Each case below is a stripped-down analog of a fixture bucket that
expects ``pass``.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph.nodes.critic import critic_node
from graph.state import make_initial_state

pytestmark = pytest.mark.hard_failure


def _state(**overrides: Any) -> dict[str, Any]:
    state = make_initial_state(
        request_id="req-over", session_id="sess-over", provider_id="prov-over"
    )
    state.update(overrides)  # type: ignore[arg-type]
    return state  # type: ignore[return-value]


def _hl7_lab_report() -> dict[str, Any]:
    """Stripped-down HL7-ORU analog — kind=lab_report, no ocr_layout."""
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "PT-CHEN",
        "document_reference_id": "DocumentReference/hl7-001",
        "collection_facility": "OSH Lab",
        "values": [
            {
                "test_name": "LDL-C",
                "normalized_test_name": "ldl-c",
                "value": "142",
                "unit": "mg/dL",
                "loinc_code": "13457-7",
                "normalized_unit": "mg/dl",
                "reference_range": "<100",
                "collection_date": "2024-03-01",
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/hl7-001",
                        "page_or_section": "OBX-1",
                        "field_or_chunk_id": "obx-1-5",
                        "quote_or_value": "142",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": "2024-03-01T10:00:00+00:00",
    }


def _workbook_extraction() -> dict[str, Any]:
    """Stripped-down XLSX analog — kind=workbook, no ocr_layout."""
    return {
        "kind": "workbook",
        "schema_version": "1.0",
        "patient_id": "PT-CHEN",
        "document_reference_id": "DocumentReference/xlsx-001",
        "intake_form": None,
        "lab_reports": [
            {
                "kind": "lab_report",
                "schema_version": "1.0",
                "patient_id": "PT-CHEN",
                "document_reference_id": "DocumentReference/xlsx-001",
                "collection_facility": None,
                "values": [
                    {
                        "test_name": "HbA1c",
                        "normalized_test_name": "hba1c",
                        "value": "6.8",
                        "unit": "%",
                        "normalized_unit": "%",
                        "reference_range": "<5.7",
                        "collection_date": "2024-03-01",
                        "abnormal_flag": "high",
                        "citations": [
                            {
                                "source_type": "document",
                                "source_id": "DocumentReference/xlsx-001",
                                "page_or_section": "Labs_Trend",
                                "field_or_chunk_id": "Labs_Trend!B2",
                                "quote_or_value": "6.8",
                            }
                        ],
                    }
                ],
                "classifier_confidence": 0.99,
                "ocr_confidence_range": [1.0, 1.0],
                "extracted_at": "2024-03-01T10:00:00+00:00",
            }
        ],
        "pending_tasks": [],
        "classifier_confidence": 0.99,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": "2024-03-01T10:00:00+00:00",
    }


def _docx_intake_form() -> dict[str, Any]:
    """Stripped-down DOCX referral analog — kind=intake_form, no ocr_layout."""
    return {
        "kind": "intake_form",
        "schema_version": "1.0",
        "patient_id": "PT-CHEN",
        "document_reference_id": "DocumentReference/docx-001",
        "demographics": None,
        "chief_concern": {
            "value": "Hyperlipidemia management",
            "citations": [
                {
                    "source_type": "document",
                    "source_id": "DocumentReference/docx-001",
                    "page_or_section": "prose",
                    "field_or_chunk_id": "para-3",
                    "quote_or_value": "Hyperlipidemia management",
                }
            ],
        },
        "current_medications": [],
        "allergies": [],
        "family_history": [],
        "code_status": None,
        "pertinent_labs": [],
        "problem_list": [],
        "classifier_confidence": 0.92,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": "2024-03-01T10:00:00+00:00",
    }


# ── Positive: nominal multimodal extractions must pass ──────────────────────


@pytest.mark.asyncio
async def test_hl7_oru_nominal_passes() -> None:
    """HL7 ORU emits kind=lab_report with no ocr_layout -> pass."""
    out = await critic_node(
        _state(extraction=_hl7_lab_report(), doc_type_hint="lab_report")
    )
    assert out["critic_decision"] == "pass", (
        f"HL7 ORU should pass, got {out['critic_decision']} "
        f"violations={out['critic_violations']}"
    )


@pytest.mark.asyncio
async def test_xlsx_workbook_full_passes() -> None:
    """XLSX wrapper emits kind=workbook with no ocr_layout -> pass."""
    out = await critic_node(_state(extraction=_workbook_extraction()))
    assert out["critic_decision"] == "pass", (
        f"XLSX workbook should pass, got {out['critic_decision']} "
        f"violations={out['critic_violations']}"
    )


@pytest.mark.asyncio
async def test_docx_referral_intake_passes() -> None:
    """DOCX referral emits kind=intake_form with no ocr_layout -> pass."""
    out = await critic_node(
        _state(extraction=_docx_intake_form(), doc_type_hint="intake_form")
    )
    assert out["critic_decision"] == "pass", (
        f"DOCX intake should pass, got {out['critic_decision']} "
        f"violations={out['critic_violations']}"
    )


# ── Negative: schema-broken extractions still hard-block ────────────────────


@pytest.mark.asyncio
async def test_unknown_kind_still_hard_blocks() -> None:
    """Unsupported kind (e.g. typo'd discriminator) must still hard-block.

    Defensive guard: the kind allowlist in ``_validate_schema`` is the
    last line of defence against an extractor emitting a discriminator the
    critic has no semantics for.
    """
    extraction = {
        "kind": "not_a_real_kind",
        "schema_version": "1.0",
    }
    out = await critic_node(_state(extraction=extraction))
    assert out["critic_decision"] == "hard_block"
    assert "SCHEMA_INVALID" in out["critic_violations"]
