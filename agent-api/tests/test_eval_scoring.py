"""Tests for evals.scoring."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple
from unittest.mock import AsyncMock, patch

import pytest

import evals.rubrics_llm as rubrics_llm
from evals.runner import RunOutcome
from evals.scoring import CaseScore, aggregate, score_case

pytestmark = pytest.mark.hard_failure


@dataclass
class _StubCase:
    case_id: str = "stub-1"
    bucket: str = "nominal_lab"
    fixture_key: str = "fk"
    doc_type_hint: str | None = None
    chart_patient: dict | None = None
    expected_kind: str = "lab_report"
    expected_critic_decision: str = "pass"
    expected_violation_codes: Tuple[str, ...] = ()
    expected_softwarn_codes: Tuple[str, ...] = ()
    expected_field_assertions: Tuple = ()
    notes: str = ""


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


@pytest.mark.asyncio
async def test_score_case_combines_all_rubrics(monkeypatch) -> None:
    # Phase 5A''' — LLM-judge rubrics now hard-raise on missing key. Stub the
    # key + the underlying judge call so the test exercises the score_case
    # composition without any live API traffic.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    from unittest.mock import AsyncMock, patch
    monkeypatch.setattr(rubrics_llm, "_judge_cache_read", lambda key: None)
    monkeypatch.setattr(rubrics_llm, "_judge_cache_write", lambda key, **kw: None)

    case = _StubCase(expected_critic_decision="pass")
    outcome = RunOutcome(
        case_id="stub-1",
        extraction=_clean_lab_extraction(),
        critic_decision="pass",
        critic_violations=[],
        soft_warns=[],
        captured_logs=[
            {"level": "INFO", "name": "graph", "message": "ok", "extra": {"duration_ms": 5}},
        ],
        error=None,
    )
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="yes")):
        score = await score_case(case, outcome)
    assert score.schema_valid is True
    assert score.citation_present is True
    assert score.correct_critic_decision is True
    assert score.factually_consistent is True
    assert score.safe_refusal is True  # pass-case short-circuits — no judge call
    assert score.no_phi_in_logs is True
    assert score.is_critic_false_positive is False


@pytest.mark.asyncio
async def test_critic_false_positive_detected(monkeypatch) -> None:
    # Phase 5A''' — LLM-judge rubrics now hard-raise on missing key.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    from unittest.mock import AsyncMock, patch
    monkeypatch.setattr(rubrics_llm, "_judge_cache_read", lambda key: None)
    monkeypatch.setattr(rubrics_llm, "_judge_cache_write", lambda key, **kw: None)

    case = _StubCase(expected_critic_decision="pass")
    outcome = RunOutcome(
        case_id="stub-1",
        extraction=_clean_lab_extraction(),
        critic_decision="hard_block",
        critic_violations=["CITATION_FIDELITY_FAILED"],
        soft_warns=[],
        captured_logs=[],
        error=None,
    )
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="yes")):
        score = await score_case(case, outcome)
    assert score.is_critic_false_positive is True
    assert score.correct_critic_decision is False


def test_aggregate_pass_rates() -> None:
    scores = [
        CaseScore(
            case_id=f"c{i}",
            schema_valid=schema,
            citation_present=cit,
            correct_critic_decision=cd,
            factually_consistent=fc,
            safe_refusal=sr,
            no_phi_in_logs=phi,
            is_critic_false_positive=fp,
        )
        for i, (schema, cit, cd, fc, sr, phi, fp) in enumerate(
            [
                (True, True, True, True, True, True, False),
                (True, True, True, False, True, True, False),
                (True, False, True, True, True, True, False),
                (False, True, False, True, True, True, True),
                (True, True, True, True, False, True, False),
            ]
        )
    ]
    out = aggregate(scores)
    assert out["schema_valid"] == pytest.approx(4 / 5)
    assert out["citation_present"] == pytest.approx(4 / 5)
    assert out["correct_critic_decision"] == pytest.approx(4 / 5)
    assert out["factually_consistent"] == pytest.approx(4 / 5)
    assert out["safe_refusal"] == pytest.approx(4 / 5)
    assert out["no_phi_in_logs"] == pytest.approx(1.0)
    assert out["critic_false_positive_rate"] == pytest.approx(1 / 5)


def test_aggregate_empty_returns_zeroed_rates() -> None:
    out = aggregate([])
    for key in (
        "schema_valid",
        "citation_present",
        "correct_critic_decision",
        "factually_consistent",
        "safe_refusal",
        "no_phi_in_logs",
        "critic_false_positive_rate",
    ):
        assert out[key] == 0.0
