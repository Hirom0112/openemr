"""Tests for evals.rubrics_llm — judge logic patched, no live API calls."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple
from unittest.mock import AsyncMock, patch

import pytest

import evals.rubrics_llm as rubrics_llm
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
    # Reset the module-level "we already warned" flag so each test is independent.
    rubrics_llm._warned_no_key = False


# --------------------------------------------------------------------------- #
# factually_consistent
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_factually_consistent_passes_clean_run() -> None:
    case = _StubCase()
    out = _outcome("pass")
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="yes")) as ask:
        result = await rubrics_llm.factually_consistent(out, case)
    assert result is True
    assert ask.call_count == 1


@pytest.mark.asyncio
async def test_factually_consistent_auto_reruns_on_disagreement() -> None:
    """Per §11.6: first run "no" + second run "yes" → PASS (judge noise filter)."""
    case = _StubCase()
    out = _outcome("pass")
    ask = AsyncMock(side_effect=["no", "yes"])
    with patch.object(rubrics_llm, "_ask_yes_no", new=ask):
        result = await rubrics_llm.factually_consistent(out, case)
    assert result is True
    assert ask.call_count == 2


@pytest.mark.asyncio
async def test_factually_consistent_fails_when_both_runs_disagree() -> None:
    case = _StubCase()
    out = _outcome("pass")
    ask = AsyncMock(side_effect=["no", "no"])
    with patch.object(rubrics_llm, "_ask_yes_no", new=ask):
        result = await rubrics_llm.factually_consistent(out, case)
    assert result is False
    assert ask.call_count == 2


@pytest.mark.asyncio
async def test_factually_consistent_no_key_returns_true(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    case = _StubCase()
    out = _outcome("pass")
    result = await rubrics_llm.factually_consistent(out, case)
    assert result is True


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
    case = _StubCase(expected_critic_decision="hard_block")
    out = _outcome("hard_block")
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="yes")) as ask:
        result = await rubrics_llm.safe_refusal(out, case)
    assert result is True
    assert ask.call_count == 1


@pytest.mark.asyncio
async def test_safe_refusal_haiku_returns_false_on_judge_no() -> None:
    case = _StubCase(expected_critic_decision="hard_block")
    out = _outcome("pass")
    with patch.object(rubrics_llm, "_ask_yes_no", new=AsyncMock(return_value="no")):
        result = await rubrics_llm.safe_refusal(out, case)
    assert result is False


@pytest.mark.asyncio
async def test_safe_refusal_no_key_returns_true(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    case = _StubCase(expected_critic_decision="hard_block")
    out = _outcome("pass")
    result = await rubrics_llm.safe_refusal(out, case)
    assert result is True
