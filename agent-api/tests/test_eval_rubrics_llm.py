"""Tests for evals.rubrics_llm — judge logic patched, no live API calls."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple
from unittest.mock import AsyncMock, patch

import pytest

import evals.rubrics_llm as rubrics_llm
from evals import EvalConfigError
from evals.runner import RunOutcome

pytestmark = pytest.mark.hard_failure


# --------------------------------------------------------------------------- #
# Stand-in case dataclass — keeps these tests independent of the parallel
# agent's W2EvalCase fixture import.
# --------------------------------------------------------------------------- #


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


def _outcome(decision: str = "pass") -> RunOutcome:
    return RunOutcome(
        case_id="stub-1",
        extraction={"kind": "lab_report"},
        critic_decision=decision,  # type: ignore[arg-type]
        critic_violations=[],
        soft_warns=[],
        captured_logs=[],
        error=None,
    )


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch):
    """Most tests want an API key present so the judge actually runs.

    Tests that exercise the no-key branch override this with monkeypatch.delenv.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    # Phase 4.8 — bypass the on-disk judge cache so each test exercises a
    # fresh vote. Without this the first test populates the cache and
    # subsequent tests with the same payload hit the cached result.
    monkeypatch.setattr(rubrics_llm, "_judge_cache_read", lambda key: None)
    monkeypatch.setattr(
        rubrics_llm, "_judge_cache_write", lambda key, **kw: None
    )


# --------------------------------------------------------------------------- #
# factually_consistent
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_factually_consistent_passes_clean_run() -> None:
    """Phase 4.8 — median-of-3: 3 yes votes → pass, 3 calls issued."""
    case = _StubCase()
    out = _outcome("pass")
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="yes")) as ask:
        result = await rubrics_llm.factually_consistent(out, case)
    assert result is True
    assert ask.call_count == 3


@pytest.mark.asyncio
async def test_factually_consistent_majority_yes_wins() -> None:
    """Phase 4.8 — 2 of 3 yes → pass (filters single judge-noise flip)."""
    case = _StubCase()
    out = _outcome("pass")
    ask = AsyncMock(side_effect=["no", "yes", "yes"])
    with patch.object(rubrics_llm, "_ask_yes_no", new=ask):
        result = await rubrics_llm.factually_consistent(out, case)
    assert result is True
    assert ask.call_count == 3


@pytest.mark.asyncio
async def test_factually_consistent_fails_when_majority_no() -> None:
    """Phase 4.8 — 2 of 3 no → fail (sustained disagreement, not noise)."""
    case = _StubCase()
    out = _outcome("pass")
    ask = AsyncMock(side_effect=["no", "no", "yes"])
    with patch.object(rubrics_llm, "_ask_yes_no", new=ask):
        result = await rubrics_llm.factually_consistent(out, case)
    assert result is False
    assert ask.call_count == 3


@pytest.mark.asyncio
async def test_factually_consistent_split_outcome_treated_as_ambiguous() -> None:
    """Phase 4.8 — yes / no / None → fails (ambiguous = not confidently grounded)."""
    case = _StubCase()
    out = _outcome("pass")
    ask = AsyncMock(side_effect=["yes", "no", None])
    with patch.object(rubrics_llm, "_ask_yes_no", new=ask):
        result = await rubrics_llm.factually_consistent(out, case)
    assert result is False
    assert ask.call_count == 3


@pytest.mark.asyncio
async def test_factually_consistent_raises_when_no_api_key(monkeypatch) -> None:
    """Phase 5A''' — silent-True short-circuit replaced by hard-raise.

    The prior behavior returned True when ANTHROPIC_API_KEY was unset,
    producing fake 1.0 pass-rates. We now fail fast.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    case = _StubCase()
    out = _outcome("pass")
    with pytest.raises(EvalConfigError, match="factually_consistent"):
        await rubrics_llm.factually_consistent(out, case)


# --------------------------------------------------------------------------- #
# safe_refusal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_safe_refusal_returns_true_for_pass_case_without_call() -> None:
    case = _StubCase(expected_critic_decision="pass")
    out = _outcome("pass")
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="no")) as ask:
        result = await rubrics_llm.safe_refusal(out, case)
    assert result is True
    # Pass-cases never invoke the judge (vacuously True).
    assert ask.call_count == 0


@pytest.mark.asyncio
async def test_safe_refusal_haiku_returns_yes_on_correct_refusal() -> None:
    """Phase 4.8 — 3 yes votes → pass (3 calls under median-of-3)."""
    case = _StubCase(expected_critic_decision="hard_block")
    out = _outcome("hard_block")
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="yes")) as ask:
        result = await rubrics_llm.safe_refusal(out, case)
    assert result is True
    assert ask.call_count == 3


@pytest.mark.asyncio
async def test_safe_refusal_haiku_returns_false_on_judge_no() -> None:
    case = _StubCase(expected_critic_decision="hard_block")
    out = _outcome("pass")
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="no")):
        result = await rubrics_llm.safe_refusal(out, case)
    assert result is False


@pytest.mark.asyncio
async def test_safe_refusal_raises_when_no_api_key(monkeypatch) -> None:
    """Phase 5A''' — silent-True short-circuit replaced by hard-raise."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    case = _StubCase(expected_critic_decision="hard_block")
    out = _outcome("pass")
    with pytest.raises(EvalConfigError, match="safe_refusal"):
        await rubrics_llm.safe_refusal(out, case)


@pytest.mark.asyncio
async def test_safe_refusal_pass_case_skips_key_check(monkeypatch) -> None:
    """Pass-cases short-circuit BEFORE the API-key check (vacuous True)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    case = _StubCase(expected_critic_decision="pass")
    out = _outcome("pass")
    # No raise — the expected=='pass' branch returns True before _require_client.
    result = await rubrics_llm.safe_refusal(out, case)
    assert result is True


# --------------------------------------------------------------------------- #
# Aggregator None-skip handling (Phase 5A''')
# --------------------------------------------------------------------------- #


def test_aggregator_excludes_none_from_denominator() -> None:
    """None values mean 'rubric does not apply' — exclude from numerator AND denominator."""
    from evals.scoring import CaseScore, aggregate

    # 3 cases: 1 passes synthesis_grounded, 1 fails, 1 sets None ("not applicable").
    # Pass-rate should be 1/2 = 0.5 (not 1/3 ≈ 0.333) because the None case
    # is excluded from the denominator.
    common = dict(
        schema_valid=True, citation_present=True, correct_critic_decision=True,
        factually_consistent=True, safe_refusal=True, no_phi_in_logs=True,
        is_critic_false_positive=False,
    )
    scores = [
        CaseScore(case_id="a", synthesis_grounded=True, **common),
        CaseScore(case_id="b", synthesis_grounded=False, **common),
        CaseScore(case_id="c", synthesis_grounded=None, **common),  # type: ignore[arg-type]
    ]
    out = aggregate(scores)
    assert out["synthesis_grounded"] == 0.5


# --------------------------------------------------------------------------- #
# Startup check in _run_async (Phase 5A''')
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_full_suite_aborts_at_startup_when_no_api_key(monkeypatch) -> None:
    """_run_async must raise EvalConfigError BEFORE any case is processed."""
    import argparse
    from pathlib import Path

    from evals.run_full_suite import _run_async

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    args = argparse.Namespace(
        cache=None,
        smoke=False,
        failing_only=None,
        max_cases=None,
        batch_size=8,
        fixtures_root=Path("/tmp/does-not-matter"),
        skip_llm_judges=False,
    )
    with pytest.raises(EvalConfigError, match="ANTHROPIC_API_KEY"):
        await _run_async(args)


@pytest.mark.asyncio
async def test_run_full_suite_skip_llm_judges_bypasses_key_check(monkeypatch) -> None:
    """--skip-llm-judges allows the startup check to pass without a key."""
    import argparse
    from pathlib import Path

    from evals.run_full_suite import _run_async

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    args = argparse.Namespace(
        cache=None,
        smoke=True,  # smoke to keep the case-set tiny if it gets past startup
        failing_only=None,
        max_cases=0,  # zero cases — the startup check is what we're exercising
        batch_size=8,
        fixtures_root=Path("/tmp/does-not-matter"),
        skip_llm_judges=True,
    )
    # Past the startup check, the function will try to import CASES and run
    # them; we only care that it does NOT raise EvalConfigError.
    try:
        await _run_async(args)
    except EvalConfigError:  # pragma: no cover — would fail the test
        raise
    except Exception:
        # Any other exception (missing fixture, etc.) is fine — startup passed.
        pass
