"""Advisory 10-case smoke subset — one case per bucket.

Run by the advisory pre-push hook. NEVER blocks a push; CI is the canonical gate.
Selects the first case of each distinct bucket from CASES (deterministic).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.hard_failure]


def _smoke_subset():
    try:
        from tests.fixtures.w2_eval_cases import CASES  # type: ignore
    except Exception:
        return []
    seen: set[str] = set()
    subset = []
    for case in CASES:
        bucket = getattr(case, "bucket", None)
        if bucket is None or bucket in seen:
            continue
        seen.add(bucket)
        subset.append(case)
    return subset


SMOKE_CASES = _smoke_subset()


@pytest.mark.skipif(not SMOKE_CASES, reason="W2 eval cases not yet available")
@pytest.mark.parametrize(
    "case",
    SMOKE_CASES,
    ids=[getattr(c, "case_id", f"case-{i}") for i, c in enumerate(SMOKE_CASES)],
)
def test_smoke_case_collected(case):
    """Mere collection — confirms the fixture surface is well-formed."""
    assert getattr(case, "case_id", None), "case missing case_id"
    assert getattr(case, "bucket", None), "case missing bucket"
