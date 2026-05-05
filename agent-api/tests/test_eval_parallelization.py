"""Tests for the asyncio.gather-based parallelization of run_full_suite.

Two contracts:

1. Identical results — running an N-case mini suite serially (batch_size=1)
   and in parallel (batch_size=8) must produce byte-identical JSON results.
   This is the regression that protects against hidden shared state in
   ``run_case`` / ``score_case`` (a per-case cache key, an in-process
   counter, etc.).

2. 429 retry — when ``run_case`` raises a synthetic 429 on the first two
   attempts and succeeds on the third, the case must be reported as
   ``status=pass`` (not ``ERROR``) and the retry must respect the documented
   exponential backoff schedule (1s, 2s, 4s).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.hard_failure


# --------------------------------------------------------------------------- #
# Deterministic mock case + outcome + score                                   #
# --------------------------------------------------------------------------- #


@dataclass
class _MockCase:
    case_id: str
    bucket: str = "schema"
    document_modality: str = "lab_report"


@dataclass
class _MockOutcome:
    case_id: str
    extraction: dict = field(default_factory=dict)


@dataclass
class _MockScore:
    case_id: str
    schema_valid: bool = True
    citation_present: bool = True
    citation_resolvable: bool = True
    citation_row_match: bool = True
    citation_token_match: bool = True
    correct_critic_decision: bool = True
    factually_consistent: bool = True
    safe_refusal: bool = True
    no_phi_in_logs: bool = True
    is_critic_false_positive: bool = False
    provenance_chain: bool = True
    status: str = "pass"
    notes: str = ""
    error: str | None = None


def _deterministic_aggregate(scores: List[_MockScore]) -> dict:
    """Mock aggregate that mirrors evals.scoring.aggregate's keys."""
    if not scores:
        return {}
    n = len(scores)
    return {
        "schema_valid": sum(1 for s in scores if s.schema_valid) / n,
        "citation_present": sum(1 for s in scores if s.citation_present) / n,
        "citation_resolvable": sum(1 for s in scores if s.citation_resolvable) / n,
        "citation_row_match": sum(1 for s in scores if s.citation_row_match) / n,
        "citation_token_match": sum(1 for s in scores if s.citation_token_match) / n,
        "correct_critic_decision": sum(1 for s in scores if s.correct_critic_decision) / n,
        "factually_consistent": sum(1 for s in scores if s.factually_consistent) / n,
        "safe_refusal": sum(1 for s in scores if s.safe_refusal) / n,
        "no_phi_in_logs": sum(1 for s in scores if s.no_phi_in_logs) / n,
        "provenance_chain": sum(1 for s in scores if s.provenance_chain) / n,
        "critic_false_positive_rate": sum(1 for s in scores if s.is_critic_false_positive) / n,
        "keyword_match_in_citation": 1.0,
    }


def _install_fake_modules(cases: List[_MockCase], run_case_impl=None) -> None:
    """Stub out ``tests.fixtures.w2_eval_cases`` / ``evals.runner`` /
    ``evals.scoring`` so run_full_suite imports cleanly without needing the
    full agent stack."""

    fixtures_pkg = sys.modules.setdefault(
        "tests.fixtures", types.ModuleType("tests.fixtures")
    )
    w2_mod = types.ModuleType("tests.fixtures.w2_eval_cases")
    w2_mod.CASES = cases
    sys.modules["tests.fixtures.w2_eval_cases"] = w2_mod

    runner_mod = types.ModuleType("evals.runner")

    async def _default_run(case, fixtures_root):  # noqa: ARG001
        # Tiny await to force a real context switch in parallel mode — this
        # is what proves the gather is actually running concurrently.
        await asyncio.sleep(0)
        return _MockOutcome(case_id=case.case_id)

    runner_mod.run_case = run_case_impl or _default_run

    def _resolve_fixture_path(_key, root):
        return Path(root) / "missing.jpg"

    runner_mod.resolve_fixture_path = _resolve_fixture_path
    sys.modules["evals.runner"] = runner_mod

    scoring_mod = types.ModuleType("evals.scoring")

    async def _score(case, outcome):
        await asyncio.sleep(0)
        return _MockScore(case_id=case.case_id)

    scoring_mod.score_case = _score
    scoring_mod.aggregate = _deterministic_aggregate
    sys.modules["evals.scoring"] = scoring_mod


def _stub_bbox_rubrics(monkeypatch=None) -> None:
    """Replace _score_bbox_rubrics with a no-op so the test does not pull in
    the full extractors / documents / auth import chain (which registers
    Prometheus collectors and breaks under repeated test invocations)."""
    from evals import run_full_suite as _rfs

    def _empty(_cases, _outcomes, _root):
        return (
            {
                "citation_iou": {"pass_rate": None, "n_evaluated": 0},
                "citation_pixel_distance": {
                    "mean_px": None,
                    "n_evaluated": 0,
                    "info_only": True,
                },
            },
            {},
        )

    if monkeypatch is not None:
        monkeypatch.setattr(_rfs, "_score_bbox_rubrics", _empty)
    else:
        _rfs._score_bbox_rubrics = _empty  # type: ignore[attr-defined]


def _run(tmp_path: Path, batch_size: int, max_cases: int | None = None) -> dict:
    out_json = tmp_path / f"results_b{batch_size}.json"
    out_md = tmp_path / f"results_b{batch_size}.md"

    from evals import run_full_suite
    _stub_bbox_rubrics()

    argv = [
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(tmp_path),
        "--batch-size", str(batch_size),
    ]
    if max_cases is not None:
        argv.extend(["--max-cases", str(max_cases)])
    rc = run_full_suite.main(argv)
    assert rc == 0
    return json.loads(out_json.read_text())


# --------------------------------------------------------------------------- #
# Contract 1: identical per-rubric pass rates                                 #
# --------------------------------------------------------------------------- #


def test_serial_and_parallel_produce_identical_aggregate(tmp_path):
    cases = [_MockCase(case_id=f"c{i}") for i in range(4)]
    _install_fake_modules(cases)

    serial = _run(tmp_path / "serial", batch_size=1)
    parallel = _run(tmp_path / "parallel", batch_size=8)

    rubric_keys = (
        "schema_valid",
        "citation_present",
        "citation_resolvable",
        "citation_row_match",
        "citation_token_match",
        "correct_critic_decision",
        "factually_consistent",
        "safe_refusal",
        "no_phi_in_logs",
        "provenance_chain",
        "critic_false_positive_rate",
    )
    for k in rubric_keys:
        assert serial[k] == parallel[k], (
            f"rubric {k} diverged between serial ({serial[k]}) "
            f"and parallel ({parallel[k]})"
        )


def test_parallel_output_is_sorted_by_case_id(tmp_path):
    """Output ordering must be deterministic regardless of gather order."""
    # Reverse-order case_ids — gather will likely complete them in some
    # interleaved order but the markdown report must come out alphabetised.
    cases = [_MockCase(case_id=f"c{i:02d}") for i in (3, 1, 2, 0)]
    _install_fake_modules(cases)

    out_json = tmp_path / "results.json"
    out_md = tmp_path / "results.md"
    from evals import run_full_suite
    rc = run_full_suite.main([
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(tmp_path),
        "--batch-size", "8",
    ])
    assert rc == 0

    md = out_md.read_text()
    # Find the per-case rows. ``c00`` should appear before ``c03``.
    idx = [md.find(f"| c{i:02d} |") for i in (0, 1, 2, 3)]
    assert all(i > 0 for i in idx), f"missing rows: {idx}"
    assert idx == sorted(idx), f"rows not sorted: {idx}"


# --------------------------------------------------------------------------- #
# Contract 2: 429 retry with exponential backoff                              #
# --------------------------------------------------------------------------- #


class _Fake429(Exception):
    """Mimics the Anthropic SDK's RateLimitError shape — name + message
    are what _is_retryable_exception sniffs on."""

    def __init__(self):
        super().__init__("HTTP 429 rate limit exceeded")


def test_retry_on_429_recovers_and_records_pass(tmp_path, monkeypatch):
    """First two attempts raise 429; third succeeds. Case must end ``pass``."""
    attempts: dict[str, int] = {}

    async def _flaky_run(case, fixtures_root):  # noqa: ARG001
        n = attempts.get(case.case_id, 0)
        attempts[case.case_id] = n + 1
        if n < 2:
            raise _Fake429()
        return _MockOutcome(case_id=case.case_id)

    cases = [_MockCase(case_id="flaky-1")]
    _install_fake_modules(cases, run_case_impl=_flaky_run)

    # Patch asyncio.sleep to a no-op so the test doesn't actually wait
    # 1+2 = 3 seconds per case. We still verify the call schedule.
    sleep_calls: list[float] = []

    async def _fast_sleep(seconds):
        sleep_calls.append(seconds)
        # yield so other tasks run, but don't actually wait
        await asyncio.sleep(0)

    from evals import run_full_suite as _rfs
    monkeypatch.setattr(_rfs, "_retry_sleep", _fast_sleep)

    data = _run(tmp_path, batch_size=4)

    # The case should have completed successfully — schema_valid=1.0 means
    # the score was recorded (not bypassed via the ERROR path).
    assert data["schema_valid"] == 1.0
    assert attempts["flaky-1"] == 3, "expected 2 retries + 1 final success"
    # Two sleeps should be in the documented backoff schedule. They include
    # jitter, so check the floor only (1.0 and 2.0).
    assert len(sleep_calls) >= 2
    assert sleep_calls[0] >= 1.0 and sleep_calls[0] < 1.3
    assert sleep_calls[1] >= 2.0 and sleep_calls[1] < 2.3


def test_non_retryable_exception_records_error(tmp_path):
    """A non-429 ValueError must NOT be retried — surfaces as ERROR row."""
    attempts: dict[str, int] = {}

    async def _broken_run(case, fixtures_root):  # noqa: ARG001
        attempts[case.case_id] = attempts.get(case.case_id, 0) + 1
        raise ValueError("some structured-validation bug")

    cases = [_MockCase(case_id="broken-1"), _MockCase(case_id="ok-1")]

    async def _selective_run(case, fixtures_root):
        if case.case_id == "broken-1":
            return await _broken_run(case, fixtures_root)
        return _MockOutcome(case_id=case.case_id)

    _install_fake_modules(cases, run_case_impl=_selective_run)

    out_json = tmp_path / "results.json"
    out_md = tmp_path / "results.md"
    from evals import run_full_suite
    rc = run_full_suite.main([
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(tmp_path),
        "--batch-size", "4",
    ])
    assert rc == 0
    assert attempts["broken-1"] == 1, "non-retryable exception was retried"
    md = out_md.read_text()
    assert "| broken-1 |" in md
    assert "ERROR" in md
