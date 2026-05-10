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
    # Wave 2C — citation-correctness rubrics. ``citation_present`` stays for
    # backward-compat (and is now informational-only in diff_baseline.py);
    # the three below are the new mechanical gates. Defaulted so existing
    # positional constructors (test fixtures predating Wave 2C) still work.
    citation_resolvable: bool = True
    citation_row_match: bool = True
    citation_token_match: bool = True
    # Phase 3 — Observation.derivedFrom provenance chain. Tri-state:
    #   True   = chain verified end-to-end (Observations have derivedFrom + citations resolve)
    #   False  = chain expected but broken
    #   None   = case had no provenance assertion OR MySQL probe was unavailable
    #            (skipped — does not count toward pass-rate denominator)
    provenance_chain: Optional[bool] = None
    # Phase 2 Step 2 Stage 3 — post-approval RAG synthesis grounding.
    # ``synthesis`` mirrors ``SynthesisOutput.to_dict()`` (see agent/synthesis.py);
    # ``synthesis_input`` mirrors the serialized ``SynthesisInput`` allowlist.
    # Both default to None — the rubric vacuously PASSes when synthesis was not
    # invoked, so existing extraction-only cases keep their pass rate.
    synthesis: Optional[dict] = None
    synthesis_input: Optional[dict] = None
    synthesis_grounded: bool = True  # vacuous-PASS default (synthesis is None)
    # Phase 9 Slice 9.9 — multimodal expansion rubrics. All vacuous-True
    # when their backing runner instrumentation is absent (see each rubric's
    # docstring). Wired into score_case but defaulted here so test fixtures
    # that build CaseScore positionally keep working.
    quarantine_audit_emitted: bool = True
    no_unconfirmed_writes: bool = True
    stage_failure_audit_emitted: bool = True
    tiff_all_pages_ocrd: bool = True
    synthetic_marker_not_extracted: bool = True
    # Phase 3 Part B' — per-modality citation locator shape rubrics.
    # Vacuous-True when ``case.document_modality`` doesn't match
    # ``'hl7_v2'`` / ``'xlsx_workbook'`` respectively, so PDF / DOCX /
    # TIFF / PNG cases keep their pass-rate contribution intact.
    hl7_citation_locator_well_formed: bool = True
    xlsx_citation_locator_well_formed: bool = True
    # 2026-05-08 problem_list build — ICD-10 hallucination guardrail and
    # FHIR Condition write-through (vacuous-True for non-intake_form cases
    # and when the runner hasn't populated written_condition_ids).
    icd10_grounded: bool = True
    condition_writeback_succeeded: bool = True
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
    citation_resolvable_ok = rubrics_mechanical.citation_resolvable(outcome)
    citation_row_match_ok = rubrics_mechanical.citation_row_match(outcome, case=case)
    citation_token_match_ok = rubrics_mechanical.citation_token_match(outcome, case=case)
    critic_ok = rubrics_mechanical.correct_critic_decision(outcome, expected=expected)
    phi_ok = rubrics_mechanical.no_phi_in_logs(outcome)

    factually_ok = await rubrics_llm.factually_consistent(outcome, case)
    safe_ok = await rubrics_llm.safe_refusal(outcome, case)

    is_false_positive = expected == "pass" and outcome.critic_decision == "hard_block"
    provenance_ok = _score_provenance_chain(case, outcome)
    synthesis_grounded_ok = rubrics_mechanical.synthesis_grounded(outcome, case=case)

    # Phase 9 Slice 9.9 — multimodal expansion rubrics. Each is vacuous-True
    # when the runner hasn't populated its backing field (see rubric
    # docstrings) so cases without the relevant instrumentation are not
    # spuriously failed. Calling them all here is what surfaces them in the
    # aggregate pass-rate map (instead of defaulting to 0.0 in
    # run_full_suite.py's empty-aggregate path).
    quarantine_audit_emitted_ok = rubrics_mechanical.quarantine_audit_emitted(outcome, case=case)
    no_unconfirmed_writes_ok = rubrics_mechanical.no_unconfirmed_writes(outcome, case=case)
    stage_failure_audit_emitted_ok = rubrics_mechanical.stage_failure_audit_emitted(outcome, case=case)
    tiff_all_pages_ocrd_ok = rubrics_mechanical.tiff_all_pages_ocrd(outcome, case=case)
    synthetic_marker_not_extracted_ok = rubrics_mechanical.synthetic_marker_not_extracted(outcome, case=case)
    icd10_grounded_ok = rubrics_mechanical.icd10_grounded(outcome, case=case)
    condition_writeback_succeeded_ok = rubrics_mechanical.condition_writeback_succeeded(outcome, case=case)
    hl7_locator_ok = rubrics_mechanical.hl7_citation_locator_well_formed(outcome, case=case)
    xlsx_locator_ok = rubrics_mechanical.xlsx_citation_locator_well_formed(outcome, case=case)

    return CaseScore(
        case_id=outcome.case_id,
        schema_valid=schema_ok,
        citation_present=citation_ok,
        citation_resolvable=citation_resolvable_ok,
        citation_row_match=citation_row_match_ok,
        citation_token_match=citation_token_match_ok,
        correct_critic_decision=critic_ok,
        factually_consistent=factually_ok,
        safe_refusal=safe_ok,
        no_phi_in_logs=phi_ok,
        is_critic_false_positive=is_false_positive,
        provenance_chain=provenance_ok,
        synthesis=outcome.synthesis,
        synthesis_input=outcome.synthesis_input,
        synthesis_grounded=synthesis_grounded_ok,
        quarantine_audit_emitted=quarantine_audit_emitted_ok,
        no_unconfirmed_writes=no_unconfirmed_writes_ok,
        stage_failure_audit_emitted=stage_failure_audit_emitted_ok,
        tiff_all_pages_ocrd=tiff_all_pages_ocrd_ok,
        synthetic_marker_not_extracted=synthetic_marker_not_extracted_ok,
        icd10_grounded=icd10_grounded_ok,
        condition_writeback_succeeded=condition_writeback_succeeded_ok,
        hl7_citation_locator_well_formed=hl7_locator_ok,
        xlsx_citation_locator_well_formed=xlsx_locator_ok,
        error=outcome.error,
    )


# --------------------------------------------------------------------------- #
# Aggregator
# --------------------------------------------------------------------------- #


_RUBRIC_FIELDS = (
    "schema_valid",
    "citation_present",
    "citation_resolvable",
    "citation_row_match",
    "citation_token_match",
    "correct_critic_decision",
    "factually_consistent",
    "safe_refusal",
    "no_phi_in_logs",
    # Phase 2 Step 2 Stage 3 — post-approval RAG synthesis grounding.
    "synthesis_grounded",
    # Phase 9 Slice 9.9 — multimodal expansion rubrics (vacuous-True when
    # the runner hasn't wired their backing fields; see rubric docstrings).
    "quarantine_audit_emitted",
    "no_unconfirmed_writes",
    "stage_failure_audit_emitted",
    "tiff_all_pages_ocrd",
    "synthetic_marker_not_extracted",
    # 2026-05-08 problem_list build — ICD-10 hallucination guardrail +
    # FHIR Condition write-through (vacuous-True for non-intake_form cases).
    "icd10_grounded",
    "condition_writeback_succeeded",
    # Phase 3 Part B' — per-modality citation locator shape (vacuous-True
    # for cases whose document_modality doesn't match).
    "hl7_citation_locator_well_formed",
    "xlsx_citation_locator_well_formed",
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

    out: Dict[str, float] = {}
    n = len(scores)
    # Tri-state-aware aggregation:
    #   bool True/False → counted in numerator (True) and denominator
    #   None            → "rubric doesn't apply to this case" — excluded
    #                     from BOTH numerator and denominator (legitimate skip,
    #                     mirrors the provenance_chain pattern below).
    # NOTE: this is distinct from EvalConfigError raised in rubrics_llm —
    # that's "infrastructure misconfigured, abort entire eval", not a
    # per-case skip. None here means the rubric author returned None for a
    # legitimate "not applicable" reason; the raise means we can't even run.
    for name in _RUBRIC_FIELDS:
        applicable = [getattr(s, name) for s in scores if getattr(s, name) is not None]
        if not applicable:
            out[name] = 1.0 if n else 0.0
            continue
        passed = sum(1 for v in applicable if v)
        out[name] = passed / len(applicable)

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
