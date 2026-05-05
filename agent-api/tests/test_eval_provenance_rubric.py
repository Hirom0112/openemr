"""Tests for the Phase-3 provenance_chain rubric in evals.scoring."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple
from unittest.mock import patch

import pytest

import evals.rubrics_llm as rubrics_llm
from evals.runner import RunOutcome
from evals.scoring import _score_provenance_chain, aggregate, CaseScore, score_case

pytestmark = pytest.mark.hard_failure


@dataclass
class _StubCase:
    case_id: str = "stub-prov"
    bucket: str = "lab_nominal"
    fixture_key: str = "fk"
    doc_type_hint: str | None = None
    chart_patient: dict | None = None
    expected_kind: str = "lab_report"
    expected_critic_decision: str = "pass"
    expected_violation_codes: Tuple[str, ...] = ()
    expected_softwarn_codes: Tuple[str, ...] = ()
    expected_field_assertions: Tuple = ()
    notes: str = ""
    expected_provenance: dict | None = None


def _ok_extraction() -> dict:
    from datetime import datetime, timezone

    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "pt-1",
        "document_reference_id": "copilot:117",
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
                        "source_id": "copilot:117",
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


def _good_observation() -> dict:
    return {
        "id": "copilot-117-32693-4",
        "fhir_resource": {
            "resourceType": "Observation",
            "derivedFrom": [{"reference": "DocumentReference/copilot-117"}],
        },
        "_copilot_citations": [{"bbox_id": "p1-b001", "quote_or_value": "4.2 mmol/L"}],
    }


def _ocr_layout() -> list[dict]:
    return [{"bbox_id": "p1-b001", "page": 1, "text": "Lactate"}]


def test_provenance_skipped_when_no_expectation() -> None:
    case = _StubCase(expected_provenance=None)
    outcome = RunOutcome(
        case_id="c", extraction=_ok_extraction(), observations=[_good_observation()],
        ocr_layout=_ocr_layout(),
    )
    assert _score_provenance_chain(case, outcome) is None


def test_provenance_skipped_when_observations_unprobed() -> None:
    case = _StubCase(expected_provenance={"observations_min": 1, "all_have_derivedFrom": True})
    outcome = RunOutcome(case_id="c", extraction=_ok_extraction(), observations=None)
    # No MySQL probe -> skipped
    assert _score_provenance_chain(case, outcome) is None


def test_provenance_passes_with_complete_chain() -> None:
    case = _StubCase(
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        }
    )
    outcome = RunOutcome(
        case_id="c",
        extraction=_ok_extraction(),
        observations=[_good_observation()],
        ocr_layout=_ocr_layout(),
    )
    assert _score_provenance_chain(case, outcome) is True


def test_provenance_fails_when_observations_below_min() -> None:
    case = _StubCase(expected_provenance={"observations_min": 1, "all_have_derivedFrom": True})
    outcome = RunOutcome(case_id="c", extraction=_ok_extraction(), observations=[])
    assert _score_provenance_chain(case, outcome) is False


def test_provenance_fails_when_derivedFrom_missing() -> None:
    bad = _good_observation()
    bad["fhir_resource"]["derivedFrom"] = []
    case = _StubCase(expected_provenance={"observations_min": 1, "all_have_derivedFrom": True})
    outcome = RunOutcome(case_id="c", extraction=_ok_extraction(), observations=[bad])
    assert _score_provenance_chain(case, outcome) is False


def test_provenance_fails_when_derivedFrom_wrong_shape() -> None:
    bad = _good_observation()
    bad["fhir_resource"]["derivedFrom"] = [{"reference": "Patient/123"}]
    case = _StubCase(expected_provenance={"observations_min": 1, "all_have_derivedFrom": True})
    outcome = RunOutcome(case_id="c", extraction=_ok_extraction(), observations=[bad])
    assert _score_provenance_chain(case, outcome) is False


def test_provenance_fails_when_citation_does_not_resolve() -> None:
    bad = _good_observation()
    bad["_copilot_citations"] = [{"bbox_id": "ghost-b999", "quote_or_value": "x"}]
    case = _StubCase(
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        }
    )
    outcome = RunOutcome(
        case_id="c", extraction=_ok_extraction(), observations=[bad], ocr_layout=_ocr_layout()
    )
    assert _score_provenance_chain(case, outcome) is False


def test_aggregate_excludes_skipped_provenance_from_denominator() -> None:
    scores = [
        CaseScore("c1", True, True, True, True, True, True, False, provenance_chain=True),
        CaseScore("c2", True, True, True, True, True, True, False, provenance_chain=None),
        CaseScore("c3", True, True, True, True, True, True, False, provenance_chain=None),
    ]
    out = aggregate(scores)
    # 1/1 of probed cases pass; the two None cases don't dilute the rate.
    assert out["provenance_chain"] == pytest.approx(1.0)


def test_aggregate_all_skipped_provenance_reports_one() -> None:
    scores = [
        CaseScore("c1", True, True, True, True, True, True, False, provenance_chain=None),
        CaseScore("c2", True, True, True, True, True, True, False, provenance_chain=None),
    ]
    out = aggregate(scores)
    assert out["provenance_chain"] == pytest.approx(1.0)


def test_aggregate_provenance_failures_reduce_rate() -> None:
    scores = [
        CaseScore("c1", True, True, True, True, True, True, False, provenance_chain=True),
        CaseScore("c2", True, True, True, True, True, True, False, provenance_chain=False),
        CaseScore("c3", True, True, True, True, True, True, False, provenance_chain=True),
    ]
    out = aggregate(scores)
    assert out["provenance_chain"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_score_case_surfaces_provenance_field(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rubrics_llm._warned_no_key = False
    case = _StubCase(
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        }
    )
    outcome = RunOutcome(
        case_id="stub-prov",
        extraction=_ok_extraction(),
        critic_decision="pass",
        critic_violations=[],
        soft_warns=[],
        captured_logs=[],
        error=None,
        observations=[_good_observation()],
        ocr_layout=_ocr_layout(),
    )
    score = await score_case(case, outcome)
    assert score.provenance_chain is True
