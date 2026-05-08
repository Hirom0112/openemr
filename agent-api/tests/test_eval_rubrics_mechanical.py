"""Tests for evals.rubrics_mechanical."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dataclasses import dataclass

from evals.rubrics_mechanical import (
    citation_present,
    correct_critic_decision,
    keyword_match_in_citation,
    no_phi_in_logs,
    schema_valid,
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
