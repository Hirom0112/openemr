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
    # Phase 3 — Observation.derivedFrom provenance chain. Tri-state:
    #   True   = chain verified end-to-end (Observations have derivedFrom + citations resolve)
    #   False  = chain expected but broken
    #   None   = case had no provenance assertion OR MySQL probe was unavailable
    #            (skipped — does not count toward pass-rate denominator)
    provenance_chain: Optional[bool] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Per-case scorer
# --------------------------------------------------------------------------- #


def _score_provenance_chain(case: Any, outcome: RunOutcome) -> Optional[bool]:
    """Return True/False/None for the provenance rubric.

    None = skipped (case has no expected_provenance, OR observations weren't
    probed). Pass-rate aggregation excludes None from the denominator.
    """
    expected = getattr(case, "expected_provenance", None)
    if not expected:
        return None
    if outcome.observations is None:
        # MySQL probe unavailable — skip rather than fail.
        return None

    obs_list = list(outcome.observations or [])
    min_count = int(expected.get("observations_min", 1))
    if len(obs_list) < min_count:
        return False

    if expected.get("all_have_derivedFrom"):
        for obs in obs_list:
            fhir = obs.get("fhir_resource") or {}
            derived = fhir.get("derivedFrom") or []
            if not isinstance(derived, list) or len(derived) == 0:
                return False
            ref0 = (derived[0] or {}).get("reference", "") if isinstance(derived[0], dict) else ""
            if not str(ref0).startswith("DocumentReference/copilot-"):
                return False

    if expected.get("all_citations_resolve"):
        layout_ids: set[str] = set()
        for blk in (outcome.ocr_layout or []):
            bid = blk.get("bbox_id") if isinstance(blk, dict) else None
            if bid:
                layout_ids.add(str(bid))
        # If we have no layout to compare against, treat as skipped-ok.
        if layout_ids:
            for obs in obs_list:
                citations = obs.get("_copilot_citations") or []
                if not citations:
                    return False
                for cit in citations:
                    bid = (cit or {}).get("bbox_id") if isinstance(cit, dict) else None
                    if bid and str(bid) not in layout_ids:
                        return False
    return True


async def score_case(case: Any, outcome: RunOutcome) -> CaseScore:
    expected = getattr(case, "expected_critic_decision", "pass")

    schema_ok = rubrics_mechanical.schema_valid(outcome)
    citation_ok = rubrics_mechanical.citation_present(outcome)
    critic_ok = rubrics_mechanical.correct_critic_decision(outcome, expected=expected)
    phi_ok = rubrics_mechanical.no_phi_in_logs(outcome)

    factually_ok = await rubrics_llm.factually_consistent(outcome, case)
    safe_ok = await rubrics_llm.safe_refusal(outcome, case)

    is_false_positive = expected == "pass" and outcome.critic_decision == "hard_block"
    provenance_ok = _score_provenance_chain(case, outcome)

    return CaseScore(
        case_id=outcome.case_id,
        schema_valid=schema_ok,
        citation_present=citation_ok,
        correct_critic_decision=critic_ok,
        factually_consistent=factually_ok,
        safe_refusal=safe_ok,
        no_phi_in_logs=phi_ok,
        is_critic_false_positive=is_false_positive,
        provenance_chain=provenance_ok,
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

    The ``provenance_chain`` rubric uses tri-state scoring: None values are
    excluded from the denominator (skipped cases — either no expectation or
    MySQL probe unavailable). When ALL cases skip the rubric, the rate is
    reported as 1.0 (the gate doesn't bite a fully-skipped run).
    """
    if not scores:
        empty: Dict[str, float] = {name: 0.0 for name in _RUBRIC_FIELDS}
        empty["critic_false_positive_rate"] = 0.0
        empty["provenance_chain"] = 0.0
        return empty

    n = len(scores)
    out: Dict[str, float] = {}
    for name in _RUBRIC_FIELDS:
        passed = sum(1 for s in scores if getattr(s, name))
        out[name] = passed / n

    fp = sum(1 for s in scores if s.is_critic_false_positive)
    out["critic_false_positive_rate"] = fp / n

    # Tri-state provenance: skip None.
    prov = [s.provenance_chain for s in scores if s.provenance_chain is not None]
    if not prov:
        out["provenance_chain"] = 1.0
    else:
        out["provenance_chain"] = sum(1 for p in prov if p) / len(prov)
    return out


__all__ = ["CaseScore", "score_case", "aggregate"]
