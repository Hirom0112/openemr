"""Tests for evals.rubrics_mechanical."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dataclasses import dataclass

from evals.rubrics_mechanical import (
    citation_present,
    correct_critic_decision,
    condition_writeback_succeeded,
    keyword_match_in_citation,
    no_phi_in_logs,
    no_unconfirmed_writes,
    quarantine_audit_emitted,
    schema_valid,
    stage_failure_audit_emitted,
    synthetic_marker_not_extracted,
)
from evals.runner import RunOutcome

pytestmark = pytest.mark.hard_failure


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _clean_lab_extraction() -> dict:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "doc-1",
        "values": [
            {
                "test_name": "Lactate",
                "normalized_test_name": "lactate",
                "value": "4.2",
                "unit": "mmol/L",
                "normalized_unit": "mmol/L",
                "reference_range": "0.5-2.0",
                "collection_date": "2024-04-01",
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "doc-1",
                        "page_or_section": "1",
                        "field_or_chunk_id": "p1-b001",
                        "quote_or_value": "4.2 mmol/L",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.92,
        "ocr_confidence_range": [0.85, 0.99],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def _outcome(extraction=None, **overrides) -> RunOutcome:
    return RunOutcome(
        case_id=overrides.get("case_id", "c1"),
        extraction=extraction,
        critic_decision=overrides.get("critic_decision", "pass"),
        critic_violations=overrides.get("critic_violations", []),
        soft_warns=overrides.get("soft_warns", []),
        captured_logs=overrides.get("captured_logs", []),
        error=overrides.get("error"),
    )


# --------------------------------------------------------------------------- #
# schema_valid
# --------------------------------------------------------------------------- #


def test_schema_valid_passes_for_clean_lab_report() -> None:
    assert schema_valid(_outcome(_clean_lab_extraction())) is True


def test_schema_valid_fails_when_required_field_missing() -> None:
    bad = _clean_lab_extraction()
    bad.pop("patient_id")  # required
    assert schema_valid(_outcome(bad)) is False


def test_schema_valid_fails_for_missing_extraction() -> None:
    assert schema_valid(_outcome(None)) is False


def test_schema_valid_fails_for_unknown_kind() -> None:
    bad = _clean_lab_extraction()
    bad["kind"] = "not-a-real-kind"
    assert schema_valid(_outcome(bad)) is False


# --------------------------------------------------------------------------- #
# citation_present
# --------------------------------------------------------------------------- #


def test_citation_present_passes_when_every_value_cited() -> None:
    assert citation_present(_outcome(_clean_lab_extraction())) is True


def test_citation_present_fails_when_value_has_empty_citations() -> None:
    bad = _clean_lab_extraction()
    bad["values"][0]["citations"] = []
    assert citation_present(_outcome(bad)) is False


def test_citation_present_fails_when_extraction_missing() -> None:
    assert citation_present(_outcome(None)) is False


def _intake_extraction_with_pertinent_labs(
    *, lab_citations: list[dict] | None = None,
) -> dict:
    """IntakeForm shape carrying one pertinent_labs entry — the new field
    added to capture lab values mentioned in non-LabReport documents
    (referral letters, admission notes). citation_present must walk
    pertinent_labs[].citations like every other intake field.
    """
    cit = (
        lab_citations
        if lab_citations is not None
        else [
            {
                "source_type": "document",
                "source_id": "doc-1",
                "page_or_section": "Pertinent Labs",
                "field_or_chunk_id": "para=26",
                "quote_or_value": "LDL-C: 142 mg/dL [HIGH]",
            }
        ]
    )
    return {
        "kind": "intake_form",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "doc-1",
        "pertinent_labs": [
            {
                "test_name": "LDL-C",
                "normalized_test_name": "LDL cholesterol",
                "value": "142",
                "unit": "mg/dL",
                "abnormal_flag": "high",
                "citations": cit,
            }
        ],
        "classifier_confidence": 0.9,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def test_citation_present_passes_for_intake_form_with_cited_pertinent_labs() -> None:
    assert citation_present(_outcome(_intake_extraction_with_pertinent_labs())) is True


def test_citation_present_fails_when_pertinent_lab_has_empty_citations() -> None:
    bad = _intake_extraction_with_pertinent_labs(lab_citations=[])
    assert citation_present(_outcome(bad)) is False


# --------------------------------------------------------------------------- #
# correct_critic_decision
# --------------------------------------------------------------------------- #


def test_correct_critic_decision_passes_on_match() -> None:
    out = _outcome(critic_decision="pass")
    assert correct_critic_decision(out, expected="pass") is True


def test_correct_critic_decision_fails_on_mismatch() -> None:
    out = _outcome(critic_decision="soft_warn")
    assert correct_critic_decision(out, expected="hard_block") is False


# --------------------------------------------------------------------------- #
# no_phi_in_logs
# --------------------------------------------------------------------------- #


def test_no_phi_in_logs_fails_when_message_contains_name() -> None:
    out = _outcome(
        captured_logs=[
            {"level": "INFO", "name": "graph", "message": "Patient Marcus Webb processed", "extra": {}},
        ]
    )
    assert no_phi_in_logs(out, synthetic_phi_values={"Marcus Webb"}) is False


def test_no_phi_in_logs_fails_when_extra_contains_phi() -> None:
    out = _outcome(
        captured_logs=[
            {"level": "INFO", "name": "graph", "message": "ok", "extra": {"name": "Marcus Webb"}},
        ]
    )
    assert no_phi_in_logs(out, synthetic_phi_values={"Marcus Webb"}) is False


def test_no_phi_in_logs_passes_when_only_ids_and_durations() -> None:
    out = _outcome(
        captured_logs=[
            {
                "level": "INFO",
                "name": "graph",
                "message": "graph_critic_decision",
                "extra": {"request_id": "req-123", "duration_ms": 42, "decision": "pass"},
            },
        ]
    )
    assert (
        no_phi_in_logs(out, synthetic_phi_values={"Marcus Webb", "100847", "1962-03-14"})
        is True
    )


# --------------------------------------------------------------------------- #
# keyword_match_in_citation — exercised against real retrieval output
# --------------------------------------------------------------------------- #


@dataclass
class _StubEvidenceCase:
    case_id: str = "evidence_test"
    bucket: str = "evidence_retrieval"
    evidence_query: str | None = "What is the AKI threshold per KDIGO?"
    expected_must_cite_source_id: tuple[str, ...] = ("kdigo-aki-2012",)
    expected_keywords_in_quote: tuple[str, ...] = ("creatinine", "0.3")


def _retrieval(snippets: list[dict]) -> dict:
    return {"snippets": snippets, "fallback_used": False}


def test_keyword_match_vacuously_true_when_no_evidence_query() -> None:
    case = _StubEvidenceCase(evidence_query=None)
    assert keyword_match_in_citation(_outcome(None), case=case) is True


def test_keyword_match_passes_when_source_and_all_keywords_present() -> None:
    case = _StubEvidenceCase()
    snippets = [
        {
            "chunk_id": "c-1",
            "source_id": "kdigo-aki-2012",
            "content": "AKI is defined by a serum creatinine rise of 0.3 mg/dL.",
        },
        {
            "chunk_id": "c-2",
            "source_id": "ssc-2021",
            "content": "Sepsis bundle.",
        },
    ]
    out = _outcome(None)
    out.retrieval = _retrieval(snippets)
    assert keyword_match_in_citation(out, case=case) is True


def test_keyword_match_fails_when_source_id_missing() -> None:
    case = _StubEvidenceCase()
    snippets = [
        {
            "chunk_id": "c-1",
            "source_id": "ssc-2021",
            "content": "Creatinine 0.3 mentioned but in the wrong source.",
        },
    ]
    out = _outcome(None)
    out.retrieval = _retrieval(snippets)
    assert keyword_match_in_citation(out, case=case) is False


def test_keyword_match_fails_when_keyword_missing_from_content() -> None:
    case = _StubEvidenceCase()
    snippets = [
        {
            "chunk_id": "c-1",
            "source_id": "kdigo-aki-2012",
            "content": "AKI is defined by a serum creatinine rise.",
        },
    ]
    out = _outcome(None)
    out.retrieval = _retrieval(snippets)
    # Missing the "0.3" keyword — should fail.
    assert keyword_match_in_citation(out, case=case) is False


def test_keyword_match_fails_when_no_snippets() -> None:
    case = _StubEvidenceCase()
    out = _outcome(None)
    out.retrieval = _retrieval([])
    assert keyword_match_in_citation(out, case=case) is False


def test_keyword_match_passes_with_only_source_match_when_no_keywords() -> None:
    case = _StubEvidenceCase(expected_keywords_in_quote=())
    snippets = [
        {"chunk_id": "c-1", "source_id": "kdigo-aki-2012", "content": "anything"},
    ]
    out = _outcome(None)
    out.retrieval = _retrieval(snippets)
    assert keyword_match_in_citation(out, case=case) is True


def test_keyword_match_fails_when_expected_sources_empty() -> None:
    case = _StubEvidenceCase(expected_must_cite_source_id=())
    out = _outcome(None)
    out.retrieval = _retrieval(
        [{"chunk_id": "c-1", "source_id": "kdigo-aki-2012", "content": "creatinine 0.3"}]
    )
    assert keyword_match_in_citation(out, case=case) is False


def test_keyword_match_skipped_run_treated_as_pass() -> None:
    case = _StubEvidenceCase()
    out = _outcome(None)
    out.skipped_reason = "missing_AUDIT_DB_URL_and_VOYAGE_API_KEY"
    # No retrieval populated — but skipped should be vacuously True.
    assert keyword_match_in_citation(out, case=case) is True


def test_keyword_match_keyword_match_is_case_insensitive() -> None:
    case = _StubEvidenceCase(expected_keywords_in_quote=("CREATININE", "0.3"))
    snippets = [
        {
            "chunk_id": "c-1",
            "source_id": "kdigo-aki-2012",
            "content": "creatinine rise of 0.3",
        },
    ]
    out = _outcome(None)
    out.retrieval = _retrieval(snippets)
    assert keyword_match_in_citation(out, case=case) is True


# --------------------------------------------------------------------------- #
# Default set sanity check
# --------------------------------------------------------------------------- #


def test_no_phi_in_logs_uses_default_set_when_none_provided() -> None:
    out = _outcome(
        captured_logs=[
            {"level": "INFO", "name": "graph", "message": "leaked Marcus Webb", "extra": {}},
        ]
    )
    # Default set includes "Marcus Webb" — should fail.
    assert no_phi_in_logs(out) is False


# --------------------------------------------------------------------------- #
# Phase 1.2 — multimodal-instrumentation rubrics fire substantively when the
# runner populates the backing fields.
#
# These tests exist so a future regression that drops the runner-side wiring
# (audit_rows / staged_observations / pending_extractions /
# written_observation_ids / written_condition_ids) is caught immediately —
# the rubrics already have vacuous-True branches when the field is None, so a
# silent wiring loss would not otherwise surface in the suite.
# --------------------------------------------------------------------------- #


@dataclass
class _StubMultimodalCase:
    case_id: str = "mm-1"
    expected_quarantine: bool = False
    expected_staging: bool = False
    document_modality: str | None = None


def test_quarantine_audit_emitted_passes_when_case_did_not_quarantine() -> None:
    case = _StubMultimodalCase(expected_quarantine=False)
    out = _outcome(None)
    out.audit_rows = []
    assert quarantine_audit_emitted(out, case=case) is True


def test_quarantine_audit_emitted_fails_when_expected_but_no_row() -> None:
    case = _StubMultimodalCase(expected_quarantine=True)
    out = _outcome(None)
    out.audit_rows = [
        {"event": "node_handoff", "detail_json": {"from_node": "supervisor"}},
    ]
    assert quarantine_audit_emitted(out, case=case) is False


def test_quarantine_audit_emitted_passes_when_quarantine_row_present() -> None:
    case = _StubMultimodalCase(expected_quarantine=True)
    out = _outcome(None)
    out.audit_rows = [
        {
            "event": "document_quarantined",
            "detail_json": {"reason": "identity_mismatch", "doc_id": "abc"},
        },
    ]
    assert quarantine_audit_emitted(out, case=case) is True


def test_quarantine_audit_vacuous_true_when_runner_did_not_wire() -> None:
    """Field-None ⇒ rubric does not fail (preserves backward-compat semantics)."""
    case = _StubMultimodalCase(expected_quarantine=True)
    out = _outcome(None)  # audit_rows defaults to None
    assert quarantine_audit_emitted(out, case=case) is True


def test_no_unconfirmed_writes_passes_when_pending_state_matches_writes() -> None:
    out = _outcome(None)
    out.pending_extractions = [
        {"observation_id": "obs-1", "state": "written"},
        {"observation_id": "obs-2", "state": "written"},
    ]
    out.written_observation_ids = ["obs-1", "obs-2"]
    assert no_unconfirmed_writes(out) is True


def test_no_unconfirmed_writes_fails_on_orphan_observation() -> None:
    out = _outcome(None)
    out.pending_extractions = [
        {"observation_id": "obs-1", "state": "approved"},  # not 'written'
    ]
    out.written_observation_ids = ["obs-1"]
    assert no_unconfirmed_writes(out) is False


def test_stage_failure_audit_emitted_fails_when_failure_row_lacks_reason() -> None:
    out = _outcome(None)
    out.audit_rows = [
        {"event": "stage_failure", "detail_json": {}, "reason": ""},
    ]
    assert stage_failure_audit_emitted(out) is False


def test_stage_failure_audit_emitted_passes_when_failure_row_carries_reason() -> None:
    out = _outcome(None)
    out.audit_rows = [
        {
            "event": "staging_failed",
            "detail_json": {"reason": "fhir_4xx"},
            "reason": "fhir_4xx",
        },
    ]
    assert stage_failure_audit_emitted(out) is True


def test_synthetic_marker_not_extracted_fails_on_marker_in_value() -> None:
    out = _outcome(None)
    out.staged_observations = [
        {"valueString": "Synthetic data — do not lift"},
    ]
    assert synthetic_marker_not_extracted(out) is False


def test_synthetic_marker_not_extracted_passes_on_clean_observation() -> None:
    out = _outcome(None)
    out.staged_observations = [
        {"valueString": "4.2", "note": [{"text": "see lab report"}]},
    ]
    assert synthetic_marker_not_extracted(out) is True


def test_condition_writeback_succeeded_fails_when_grounded_code_missing() -> None:
    extraction = {
        "kind": "intake_form",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "doc-1",
        "problem_list": [
            {"name": "essential hypertension", "icd10_code": "I10"},
        ],
    }
    out = _outcome(extraction)
    out.written_condition_ids = ["copilot-doc-1-E11.9"]  # different code
    assert condition_writeback_succeeded(out) is False


def test_condition_writeback_succeeded_passes_when_code_lands() -> None:
    extraction = {
        "kind": "intake_form",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "doc-1",
        "problem_list": [
            {"name": "essential hypertension", "icd10_code": "I10"},
        ],
    }
    out = _outcome(extraction)
    out.written_condition_ids = ["copilot-doc-1-I10"]
    assert condition_writeback_succeeded(out) is True


# --------------------------------------------------------------------------- #
# Phase 3 Part B' — per-modality citation locator rubrics + tiff page count
# --------------------------------------------------------------------------- #


@dataclass
class _StubCase:
    document_modality: str = "unknown"
    expected_quarantine: bool = False
    expected_staging: bool = False
    bucket: str | None = None


def _hl7_lab_extraction(field_id: str = "OBX-5|seg=3") -> dict:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "doc-1",
        "values": [
            {
                "test_name": "Lactate",
                "normalized_test_name": "lactate",
                "value": "4.2",
                "unit": "mmol/L",
                "normalized_unit": "mmol/L",
                "reference_range": "0.5-2.0",
                "collection_date": "2024-04-01",
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "doc-1",
                        "page_or_section": None,
                        "field_or_chunk_id": field_id,
                        "quote_or_value": "4.2",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def test_hl7_locator_well_formed_passes_for_obx_seg() -> None:
    from evals.rubrics_mechanical import hl7_citation_locator_well_formed
    out = _outcome(_hl7_lab_extraction("OBX-5|seg=3"))
    assert hl7_citation_locator_well_formed(out, case=_StubCase("hl7_v2")) is True


def test_hl7_locator_well_formed_passes_for_pid_subfield() -> None:
    from evals.rubrics_mechanical import hl7_citation_locator_well_formed
    out = _outcome(_hl7_lab_extraction("PID-3.1"))
    assert hl7_citation_locator_well_formed(out, case=_StubCase("hl7_v2")) is True


def test_hl7_locator_well_formed_fails_on_garbage_locator() -> None:
    from evals.rubrics_mechanical import hl7_citation_locator_well_formed
    out = _outcome(_hl7_lab_extraction("OBX_5_seg3"))
    assert hl7_citation_locator_well_formed(out, case=_StubCase("hl7_v2")) is False


def test_hl7_locator_well_formed_fails_on_unknown_segment() -> None:
    from evals.rubrics_mechanical import hl7_citation_locator_well_formed
    out = _outcome(_hl7_lab_extraction("XYZ-1.1"))
    assert hl7_citation_locator_well_formed(out, case=_StubCase("hl7_v2")) is False


def test_hl7_locator_well_formed_vacuous_for_other_modality() -> None:
    from evals.rubrics_mechanical import hl7_citation_locator_well_formed
    out = _outcome(_hl7_lab_extraction("garbage"))
    # Non-hl7 modality short-circuits to True regardless of locator shape.
    assert hl7_citation_locator_well_formed(out, case=_StubCase("typed_pdf")) is True


def _xlsx_extraction(field_id: str) -> dict:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "doc-1",
        "values": [
            {
                "test_name": "LDL-C",
                "normalized_test_name": "ldl",
                "value": "142",
                "unit": "mg/dL",
                "normalized_unit": "mg/dL",
                "reference_range": "<100",
                "collection_date": "2024-04-01",
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "doc-1",
                        "page_or_section": None,
                        "field_or_chunk_id": field_id,
                        "quote_or_value": "142",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def test_xlsx_locator_well_formed_passes_for_patient_value_locator() -> None:
    from evals.rubrics_mechanical import xlsx_citation_locator_well_formed
    out = _outcome(_xlsx_extraction("sheet=Patient|row=4|col=Value"))
    assert xlsx_citation_locator_well_formed(out, case=_StubCase("xlsx_workbook")) is True


def test_xlsx_locator_well_formed_passes_for_labs_trend_date_col() -> None:
    from evals.rubrics_mechanical import xlsx_citation_locator_well_formed
    out = _outcome(_xlsx_extraction("sheet=Labs_Trend|row=12|col=2026-04-15"))
    assert xlsx_citation_locator_well_formed(out, case=_StubCase("xlsx_workbook")) is True


def test_xlsx_locator_well_formed_fails_on_unknown_sheet() -> None:
    from evals.rubrics_mechanical import xlsx_citation_locator_well_formed
    out = _outcome(_xlsx_extraction("sheet=Bogus|row=1|col=Value"))
    assert xlsx_citation_locator_well_formed(out, case=_StubCase("xlsx_workbook")) is False


def test_xlsx_locator_well_formed_fails_on_garbage_locator() -> None:
    from evals.rubrics_mechanical import xlsx_citation_locator_well_formed
    out = _outcome(_xlsx_extraction("not-a-locator"))
    assert xlsx_citation_locator_well_formed(out, case=_StubCase("xlsx_workbook")) is False


def test_xlsx_locator_well_formed_vacuous_for_pdf_modality() -> None:
    from evals.rubrics_mechanical import xlsx_citation_locator_well_formed
    out = _outcome(_xlsx_extraction("anything"))
    assert xlsx_citation_locator_well_formed(out, case=_StubCase("typed_pdf")) is True


def test_demographic_update_schema_registered() -> None:
    """HL7 ADT^A08 emits a DemographicUpdateEvent with kind='demographic_update' —
    the Phase 3 Part B' kind registration must accept it."""
    from datetime import date as _date

    extraction = {
        "kind": "demographic_update",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "doc-1",
        "event_type": "ADT^A08",
        "control_id": "MSG00001",
        "mrn": {
            "value": "100847",
            "citations": [
                {
                    "source_type": "document",
                    "source_id": "doc-1",
                    "page_or_section": None,
                    "field_or_chunk_id": "PID-3.1",
                    "quote_or_value": "100847",
                }
            ],
        },
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    out = _outcome(extraction)
    assert schema_valid(out) is True


def test_tiff_n_pages_and_ocr_page_citations_default_none() -> None:
    """RunOutcome's new TIFF instrumentation defaults to None (vacuous-True)."""
    out = _outcome(None)
    assert out.tiff_n_pages is None
    assert out.ocr_page_citations is None


# --------------------------------------------------------------------------- #
# Phase 4c — citation_row_match / citation_token_match bbox-only short-circuit.
#
# The bbox_gt bucket fixtures (synthetic_v2 typed_pdf / table_heavy /
# photo_capture cases with a {fixture}.gt.json sidecar) were built for the
# geometry-only ``citation_iou`` rubric. Their sidecar GT carries bbox
# coordinates + value strings but no row-token contract. Scoring them on
# ``citation_row_match`` / ``citation_token_match`` charged the system for
# OCR-text vs LLM-value-text divergence the rubrics were never designed
# to gate (especially on warped photo_capture inputs where OCR noise
# dominates). Both rubrics now short-circuit to vacuous-True when the
# case fixture shape matches ``bucket == "bbox_gt"`` — mirrors the
# per-modality vacuous-True pattern used by ``tiff_all_pages_ocrd`` /
# ``hl7_citation_locator_well_formed``.
# --------------------------------------------------------------------------- #


def _bbox_only_extraction_with_misaligned_text() -> dict:
    """Extraction whose value text contains tokens NOT present in the cited
    block's text — exactly the shape that fails citation_row_match by
    construction on bbox_gt cases (warped photo OCR drops/garbles tokens
    the LLM still correctly emitted)."""
    return {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "pt-bbox",
        "document_reference_id": "doc-bbox",
        "key_facts": [
            {
                "text": "Patient: Iris Tanaka",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "doc-bbox",
                        "page_or_section": "1",
                        "field_or_chunk_id": "p1-b000",
                        # OCR captured something noisy/partial; the value
                        # tokens "patient", "iris", "tanaka" are NOT all
                        # present here.
                        "quote_or_value": "Pat1ent: lns",
                    }
                ],
                "needs_review": False,
            }
        ],
        "classifier_confidence": 0.9,
        "ocr_confidence_range": [0.6, 0.95],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def test_citation_row_match_bbox_only_returns_vacuous_true() -> None:
    """bbox_gt bucket short-circuits BOTH row_match and token_match to True
    even when the value tokens don't appear in the cited block."""
    from evals.rubrics_mechanical import citation_row_match, citation_token_match

    out = _outcome(_bbox_only_extraction_with_misaligned_text())
    bbox_case = _StubCase(document_modality="photo_capture", bucket="bbox_gt")
    # Fixture shape (bucket=bbox_gt) → vacuous-True, regardless of OCR noise.
    assert citation_row_match(out, case=bbox_case) is True
    assert citation_token_match(out, case=bbox_case) is True


def test_citation_row_match_non_bbox_case_still_scored_normally() -> None:
    """A photo_capture case NOT in the bbox_gt bucket is still scored —
    misalignment between value tokens and cited block still fails the
    rubric. Skip is fixture-shape-driven (bucket), not modality-driven."""
    from evals.rubrics_mechanical import citation_row_match, citation_token_match

    out = _outcome(_bbox_only_extraction_with_misaligned_text())
    # Same modality but a different bucket — must NOT short-circuit.
    other_case = _StubCase(document_modality="photo_capture", bucket="intake_nominal")
    assert citation_row_match(out, case=other_case) is False
    assert citation_token_match(out, case=other_case) is False
    # Calling without case (legacy path) also still gates as before.
    assert citation_row_match(out) is False
    assert citation_token_match(out) is False


def test_citation_row_match_bbox_only_with_aligned_text_still_passes() -> None:
    """Sanity: short-circuit doesn't change the True path. A bbox_gt case
    whose citation DOES contain the value tokens still returns True."""
    from evals.rubrics_mechanical import citation_row_match, citation_token_match

    aligned = {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "pt-bbox",
        "document_reference_id": "doc-bbox",
        "key_facts": [
            {
                "text": "ADMISSION",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "doc-bbox",
                        "page_or_section": "1",
                        "field_or_chunk_id": "p1-b000",
                        "quote_or_value": "ADMISSION",
                    }
                ],
                "needs_review": False,
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    out = _outcome(aligned)
    bbox_case = _StubCase(document_modality="photo_capture", bucket="bbox_gt")
    assert citation_row_match(out, case=bbox_case) is True
    assert citation_token_match(out, case=bbox_case) is True
