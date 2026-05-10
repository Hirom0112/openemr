"""Bucket-count contract for the W2 eval set."""

from __future__ import annotations

import pytest

from tests.fixtures.w2_eval_cases import BUCKET_COUNTS, CASES, BUCKET_CONTRACT_TOTAL


@pytest.mark.hard_failure
def test_bucket_counts_match() -> None:
    """Bucket-distribution contract.

    BUCKET_COUNTS is split into two accounting groups:

      1. Pre-multimodal entries — distribution must match the actual
         ``case.bucket`` tally over CASES, and sum to BUCKET_CONTRACT_TOTAL.
      2. ``slice_9_9_multimodal`` — a separate accounting entry for the
         32 Phase 9 Slice 9.9 multimodal cases that physically reuse the
         existing scenario buckets (lab_nominal, wrong_patient, etc.) but
         are tracked separately so the original W2 baseline contract stays
         intact.

    Total runtime suite size = BUCKET_CONTRACT_TOTAL + slice_9_9_multimodal
    = len(CASES).
    """
    counts: dict[str, int] = {}
    for c in CASES:
        counts[c.bucket] = counts.get(c.bucket, 0) + 1

    multimodal_n = BUCKET_COUNTS.get("slice_9_9_multimodal", 0)
    pre_multimodal = {
        k: v for k, v in BUCKET_COUNTS.items() if k != "slice_9_9_multimodal"
    }
    # Subtract the multimodal cases from the runtime tally so what's left
    # mirrors the frozen pre-multimodal contract. The multimodal cases'
    # physical bucket distribution is checked structurally by
    # _validate_multimodal_unique() at fixture import time.
    expected_runtime = dict(pre_multimodal)
    # Multimodal cases physically increment the same bucket keys; subtract
    # them by reconstructing what the pre-multimodal tally must look like.
    runtime_pre_multimodal = dict(counts)
    # The 32 multimodal cases live in CASES but are accounted under
    # "slice_9_9_multimodal" — back them out to compare to the frozen
    # pre-multimodal contract.
    from tests.fixtures.w2_eval_cases import _MULTIMODAL_CASES  # type: ignore
    for mc in _MULTIMODAL_CASES:
        runtime_pre_multimodal[mc.bucket] = runtime_pre_multimodal.get(mc.bucket, 0) - 1
        if runtime_pre_multimodal[mc.bucket] == 0:
            del runtime_pre_multimodal[mc.bucket]
    assert runtime_pre_multimodal == expected_runtime, runtime_pre_multimodal

    assert sum(pre_multimodal.values()) == BUCKET_CONTRACT_TOTAL
    assert multimodal_n == len(_MULTIMODAL_CASES)
    assert len(CASES) == BUCKET_CONTRACT_TOTAL + multimodal_n


@pytest.mark.hard_failure
def test_no_duplicate_case_ids() -> None:
    ids = [c.case_id for c in CASES]
    assert len(set(ids)) == len(ids), "duplicate case_ids: " + str(
        sorted(i for i in ids if ids.count(i) > 1)
    )


@pytest.mark.hard_failure
def test_every_fixture_key_resolves() -> None:
    """Every fixture_key referenced by a case must exist in the corpus."""
    from tests.fixtures.eval._generate_eval_corpus import generate_all

    paths = generate_all()
    referenced = {c.fixture_key for c in CASES}
    missing = referenced - paths.keys()
    assert not missing, f"unresolved fixture keys: {sorted(missing)}"
    for key in referenced:
        assert paths[key].exists(), f"{key} missing on disk: {paths[key]}"
