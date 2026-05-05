"""Wiring test for ``nearest_label_grounded`` in ``run_full_suite``.

Confirms the contract documented in the Wave 2B/2C handoff:

  - Cases that emit a ``nearest_label`` whose normalized substring appears
    in a layout block within the 200pt window of the citation bbox PASS.
  - Cases that emit a ``nearest_label`` with no nearby match FAIL.
  - Cases that emit no ``nearest_label`` are SKIPPED (excluded from both
    numerator and denominator).

Constructs minimal fakes for cases / outcomes / layout and drives
``_score_nearest_label_grounded`` directly — does not require fixtures or
the runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from evals.run_full_suite import _score_nearest_label_grounded

pytestmark = pytest.mark.hard_failure


@dataclass
class _FakeCase:
    case_id: str
    bucket: str = "schema"
    document_modality: str = "intake"


@dataclass
class _FakeOutcome:
    extraction: dict[str, Any]
    ocr_layout: Optional[list[dict[str, Any]]] = field(default=None)


def _intake_extraction_with_nearest_label(label: Optional[str]) -> dict[str, Any]:
    """Minimal intake_form extraction with one current_medication carrying
    a citation that may carry a ``nearest_label``."""
    citation: dict[str, Any] = {
        "bbox": [100.0, 100.0, 50.0, 12.0],
        "page": 1,
    }
    if label is not None:
        citation["nearest_label"] = label
    return {
        "kind": "intake_form",
        "current_medications": [
            {"name": "Lisinopril 10mg", "citations": [citation]},
        ],
    }


def test_nearest_label_grounded_wiring_global_pass_rate():
    # Case A: emits "Patient Name" — layout has a "Patient Name" block at
    # (110, 105) which is within the 200pt window of the citation centroid
    # (125, 106). PASS.
    case_a = _FakeCase(case_id="case-a")
    outcome_a = _FakeOutcome(
        extraction=_intake_extraction_with_nearest_label("Patient Name"),
        ocr_layout=[
            {
                "bbox_id": "p1-b001",
                "page": 1,
                "bbox": (110.0, 105.0, 80.0, 12.0),
                "text": "Patient Name",
            },
        ],
    )

    # Case B: emits "Bogus" — layout has no matching block. FAIL.
    case_b = _FakeCase(case_id="case-b")
    outcome_b = _FakeOutcome(
        extraction=_intake_extraction_with_nearest_label("Bogus"),
        ocr_layout=[
            {
                "bbox_id": "p1-b002",
                "page": 1,
                "bbox": (110.0, 105.0, 80.0, 12.0),
                "text": "Patient Name",
            },
        ],
    )

    # Case C: emits no nearest_label at all. SKIPPED.
    case_c = _FakeCase(case_id="case-c")
    outcome_c = _FakeOutcome(
        extraction=_intake_extraction_with_nearest_label(None),
        ocr_layout=[
            {
                "bbox_id": "p1-b003",
                "page": 1,
                "bbox": (110.0, 105.0, 80.0, 12.0),
                "text": "Patient Name",
            },
        ],
    )

    cases = [case_a, case_b, case_c]
    outcomes = {
        "case-a": outcome_a,
        "case-b": outcome_b,
        "case-c": outcome_c,
    }

    global_block, per_mod = _score_nearest_label_grounded(cases, outcomes)

    # 1 passed, 2 evaluated (case_c excluded), pass-rate = 0.5.
    assert global_block["n_evaluated"] == 2
    assert global_block["n_passed"] == 1
    assert global_block["pass_rate"] == 0.5
    assert global_block["info_only"] is True

    # Per-modality (all three are 'intake') — same 1/2 numbers.
    assert "intake" in per_mod
    assert per_mod["intake"]["nearest_label_grounded"] == 0.5
    assert per_mod["intake"]["nearest_label_grounded_detail"]["n_evaluated"] == 2


def test_nearest_label_grounded_all_skipped_returns_none():
    """When every case skips (no labels emitted), global pass_rate is None."""
    case = _FakeCase(case_id="case-z")
    outcome = _FakeOutcome(
        extraction=_intake_extraction_with_nearest_label(None),
        ocr_layout=[],
    )
    global_block, per_mod = _score_nearest_label_grounded([case], {"case-z": outcome})
    assert global_block["pass_rate"] is None
    assert global_block["n_evaluated"] == 0
    assert per_mod == {}


def test_nearest_label_grounded_handles_rubric_exception(monkeypatch, caplog):
    """If the rubric raises, the case is skipped — the run does NOT crash."""
    import evals.run_full_suite as mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic rubric failure")

    monkeypatch.setattr(
        "evals.rubrics_llm.nearest_label_grounded", _boom,
    )

    case = _FakeCase(case_id="case-x")
    outcome = _FakeOutcome(
        extraction=_intake_extraction_with_nearest_label("Patient Name"),
        ocr_layout=[],
    )
    global_block, _ = mod._score_nearest_label_grounded([case], {"case-x": outcome})
    # Skipped — does not crash, does not count.
    assert global_block["pass_rate"] is None
    assert global_block["n_evaluated"] == 0
