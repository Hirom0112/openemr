"""Slice 5.3 — Combined per-case scoring + aggregate pass-rates.

Wraps the mechanical and LLM rubrics into a single :class:`CaseScore` and
exposes :func:`aggregate` for gate-evaluation. Critic-false-positive is
tracked separately per W2_ARCHITECTURE §11.4.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from . import rubrics_llm, rubrics_mechanical
from .runner import RunOutcome


@dataclass
class CaseScore:
    case_id: str
    schema_valid: bool
    citation_present: bool
    correct_critic_decision: bool
    factually_consistent: bool
    safe_refusal: bool
    no_phi_in_logs: bool
    is_critic_false_positive: bool
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Per-case scorer
# --------------------------------------------------------------------------- #


async def score_case(case: Any, outcome: RunOutcome) -> CaseScore:
    expected = getattr(case, "expected_critic_decision", "pass")

    schema_ok = rubrics_mechanical.schema_valid(outcome)
    citation_ok = rubrics_mechanical.citation_present(outcome)
    critic_ok = rubrics_mechanical.correct_critic_decision(outcome, expected=expected)
    phi_ok = rubrics_mechanical.no_phi_in_logs(outcome)

    factually_ok = await rubrics_llm.factually_consistent(outcome, case)
    safe_ok = await rubrics_llm.safe_refusal(outcome, case)

    is_false_positive = expected == "pass" and outcome.critic_decision == "hard_block"

    return CaseScore(
        case_id=outcome.case_id,
        schema_valid=schema_ok,
        citation_present=citation_ok,
        correct_critic_decision=critic_ok,
        factually_consistent=factually_ok,
        safe_refusal=safe_ok,
        no_phi_in_logs=phi_ok,
        is_critic_false_positive=is_false_positive,
        error=outcome.error,
    )


# --------------------------------------------------------------------------- #
# Aggregator
# --------------------------------------------------------------------------- #


_RUBRIC_FIELDS = (
    "schema_valid",
    "citation_present",
    "correct_critic_decision",
    "factually_consistent",
    "safe_refusal",
    "no_phi_in_logs",
)


def aggregate(scores: List[CaseScore]) -> Dict[str, float]:
    """Return ``{rubric_name: pass_rate}`` plus ``critic_false_positive_rate``.

    Pass-rates are 0.0 when the input list is empty (avoids ZeroDivision and
    surfaces a clearly-broken gate run).
    """
    if not scores:
        empty: Dict[str, float] = {name: 0.0 for name in _RUBRIC_FIELDS}
        empty["critic_false_positive_rate"] = 0.0
        return empty

    n = len(scores)
    out: Dict[str, float] = {}
    for name in _RUBRIC_FIELDS:
        passed = sum(1 for s in scores if getattr(s, name))
        out[name] = passed / n

    fp = sum(1 for s in scores if s.is_critic_false_positive)
    out["critic_false_positive_rate"] = fp / n
    return out


__all__ = ["CaseScore", "score_case", "aggregate"]
