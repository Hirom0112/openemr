"""Bucket-count contract for the W2 eval set."""

from __future__ import annotations

import pytest

from tests.fixtures.w2_eval_cases import BUCKET_COUNTS, CASES, BUCKET_CONTRACT_TOTAL


@pytest.mark.hard_failure
def test_bucket_counts_match() -> None:
    counts: dict[str, int] = {}
    for c in CASES:
        counts[c.bucket] = counts.get(c.bucket, 0) + 1
    assert counts == BUCKET_COUNTS, counts
    assert sum(BUCKET_COUNTS.values()) == BUCKET_CONTRACT_TOTAL
    assert len(CASES) == BUCKET_CONTRACT_TOTAL


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
