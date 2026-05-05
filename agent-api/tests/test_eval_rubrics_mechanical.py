"""Tests for evals.rubrics_mechanical."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from evals.rubrics_mechanical import (
    citation_present,
    correct_critic_decision,
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


def test_no_phi_in_logs_uses_default_set_when_none_provided() -> None:
    out = _outcome(
        captured_logs=[
            {"level": "INFO", "name": "graph", "message": "leaked Marcus Webb", "extra": {}},
        ]
    )
    # Default set includes "Marcus Webb" — should fail.
    assert no_phi_in_logs(out) is False
