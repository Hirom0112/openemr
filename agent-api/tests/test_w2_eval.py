"""W2 eval-set collection smoke test (Slice 5.1 + 5.4 acceptance).

Proves the 50-case set is real, parametrizable, and structurally valid.
The rubric runners (Slices 5.2/5.3) consume the same ``CASES`` list.
"""

from __future__ import annotations

import pytest

from tests.fixtures.w2_eval_cases import CASES, W2EvalCase

_VALID_BUCKETS = {
    "lab_nominal", "intake_nominal", "unknown_nominal",
    "wrong_type_hint", "wrong_patient", "blank_noise",
    "mixed_content", "low_quality_scan", "intra_doc_conflict",
    "evidence_retrieval", "missing_data",
}
_VALID_KINDS = {"lab_report", "intake_form", "unknown"}
_VALID_DECISIONS = {"pass", "soft_warn", "hard_block"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
@pytest.mark.hard_failure
def test_case_collected(case: W2EvalCase) -> None:
    """Smoke: every case is collected and structurally valid."""
    assert case.case_id, "case_id must be non-empty"
    assert case.bucket in _VALID_BUCKETS, case.bucket
    assert case.fixture_key, "fixture_key must be set"
    assert case.expected_kind in _VALID_KINDS, case.expected_kind
    assert case.expected_critic_decision in _VALID_DECISIONS, case.expected_critic_decision
    # chart_patient must be FHIR-Patient-shaped
    assert isinstance(case.chart_patient, dict)
    assert case.chart_patient.get("resourceType") == "Patient"
    assert case.chart_patient.get("id"), "chart_patient must carry an id"
    # tuple invariants for slot-style fields
    assert isinstance(case.expected_violation_codes, tuple)
    assert isinstance(case.expected_softwarn_codes, tuple)
    assert isinstance(case.expected_field_assertions, tuple)
    for entry in case.expected_field_assertions:
        assert isinstance(entry, tuple) and len(entry) == 2, entry
