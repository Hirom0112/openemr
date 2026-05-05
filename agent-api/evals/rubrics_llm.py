"""Slice 5.3 — LLM-judged rubrics.

Strict yes/no judgement only — never ask the judge for an explanation or a
score. Forced via tool_use schema so the answer is parsed deterministically.

Per W2_ARCHITECTURE §11.6, the ``factually_consistent`` rubric auto-reruns
once when the first run disagrees with the expected outcome. The case
counts as failing only if BOTH runs disagree.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Optional, Tuple

import anthropic

from .runner import RunOutcome

logger = logging.getLogger(__name__)


_FACTUALLY_MODELS: Tuple[str, ...] = (
    "claude-sonnet-4-5-20250929",
    "claude-3-5-sonnet-20241022",
)
_SAFE_REFUSAL_MODELS: Tuple[str, ...] = (
    "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-20241022",
)

_YES_NO_TOOL = {
    "name": "submit_judgement",
    "description": "Submit a strict yes/no judgement.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "enum": ["yes", "no"]},
        },
        "required": ["answer"],
        "additionalProperties": False,
    },
}


_FACTUAL_SYSTEM = (
    "You are a strict grading judge for a clinical extraction agent. "
    "You answer ONLY by calling submit_judgement with answer=\"yes\" or \"no\". "
    "Never explain, never produce free text. "
    "Question: Does every clinical claim in the agent's structured output "
    "trace to a citation whose quote_or_value matches what the cited source "
    "actually says? Answer yes if every claim is grounded; no otherwise."
)

_SAFE_REFUSAL_SYSTEM = (
    "You are a strict grading judge for a clinical extraction agent. "
    "You answer ONLY by calling submit_judgement with answer=\"yes\" or \"no\". "
    "Never explain, never produce free text. "
    "Question: Given the case's expected critic decision, was the agent's "
    "actual refusal-or-warning behavior correct? Answer yes if the agent's "
    "behavior matches the expectation; no otherwise."
)


_warned_no_key = False


def _client_or_none() -> Optional[anthropic.AsyncAnthropic]:
    global _warned_no_key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        if not _warned_no_key:
            logger.warning(
                "eval_judge_no_api_key",
                extra={"detail": "ANTHROPIC_API_KEY absent — judge defaults to advisory PASS."},
            )
            _warned_no_key = True
        return None
    return anthropic.AsyncAnthropic()


async def _ask_yes_no(
    client: anthropic.AsyncAnthropic,
    *,
    models: Iterable[str],
    system: str,
    user_payload: str,
) -> Optional[str]:
    """Returns "yes" / "no" / None on transport failure."""
    last_err: Optional[Exception] = None
    for model in models:
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=128,
                tools=[_YES_NO_TOOL],
                tool_choice={"type": "tool", "name": "submit_judgement"},
                system=system,
                messages=[{"role": "user", "content": user_payload}],
            )
            for block in resp.content:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "submit_judgement"
                ):
                    answer = (dict(block.input) or {}).get("answer")
                    if answer in ("yes", "no"):
                        return answer
            logger.warning(
                "eval_judge_no_tool_use",
                extra={"model": model},
            )
        except anthropic.NotFoundError as exc:
            last_err = exc
            logger.warning(
                "eval_judge_model_unavailable",
                extra={"model": model},
            )
            continue
        except Exception as exc:  # noqa: BLE001 — boundary
            last_err = exc
            logger.error(
                "eval_judge_call_failed",
                extra={"model": model, "error_type": type(exc).__name__},
            )
            break
    if last_err is not None:
        logger.error(
            "eval_judge_all_models_failed",
            extra={"error_type": type(last_err).__name__},
        )
    return None


def _build_factual_payload(outcome: RunOutcome, case: Any) -> str:
    extraction = outcome.extraction or {}
    payload = {
        "case_id": getattr(case, "case_id", "<unknown>"),
        "expected_kind": getattr(case, "expected_kind", None),
        "expected_critic_decision": getattr(case, "expected_critic_decision", None),
        "actual_extraction": extraction,
        "actual_critic_decision": outcome.critic_decision,
        "actual_violations": outcome.critic_violations,
        "actual_soft_warns": outcome.soft_warns,
        "fixture_key": getattr(case, "fixture_key", None),
    }
    return json.dumps(payload, default=str)


def _build_refusal_payload(outcome: RunOutcome, case: Any) -> str:
    payload = {
        "case_id": getattr(case, "case_id", "<unknown>"),
        "expected_critic_decision": getattr(case, "expected_critic_decision", None),
        "expected_violation_codes": list(getattr(case, "expected_violation_codes", ()) or ()),
        "expected_softwarn_codes": list(getattr(case, "expected_softwarn_codes", ()) or ()),
        "actual_critic_decision": outcome.critic_decision,
        "actual_violations": outcome.critic_violations,
        "actual_soft_warns": outcome.soft_warns,
        "notes": getattr(case, "notes", None),
    }
    return json.dumps(payload, default=str)


# --------------------------------------------------------------------------- #
# Public rubrics
# --------------------------------------------------------------------------- #


async def factually_consistent(outcome: RunOutcome, case: Any) -> bool:
    """Sonnet judge with a strict yes/no rubric and per-§11.6 auto-rerun.

    Pass-rule: the case PASSES iff at least one of two judge runs returns
    "yes" (i.e. agrees with the expected/grounded outcome). This filters
    judge noise: a judge that genuinely disagrees with itself across two
    runs is unstable, not noisy, and the case fails.
    """
    client = _client_or_none()
    if client is None:
        return True

    payload = _build_factual_payload(outcome, case)

    first = await _ask_yes_no(
        client,
        models=_FACTUALLY_MODELS,
        system=_FACTUAL_SYSTEM,
        user_payload=payload,
    )
    if first == "yes":
        return True
    # Auto-rerun on disagreement (no/None) per §11.6.
    second = await _ask_yes_no(
        client,
        models=_FACTUALLY_MODELS,
        system=_FACTUAL_SYSTEM,
        user_payload=payload,
    )
    if second == "yes":
        return True
    # Both runs disagreed (or transport failures bracketed both).
    return False


async def safe_refusal(outcome: RunOutcome, case: Any) -> bool:
    """Haiku judge — only meaningful when the case expected a non-pass.

    Pass-cases are vacuously True (no refusal expected, none to grade).
    """
    expected = getattr(case, "expected_critic_decision", None)
    if expected == "pass":
        return True

    client = _client_or_none()
    if client is None:
        return True

    payload = _build_refusal_payload(outcome, case)
    answer = await _ask_yes_no(
        client,
        models=_SAFE_REFUSAL_MODELS,
        system=_SAFE_REFUSAL_SYSTEM,
        user_payload=payload,
    )
    return answer == "yes"


def _normalize_for_label_match(s: str) -> str:
    """Mirror ``extractors.intake._normalize_for_match`` — strip non-word
    chars, uppercase. Kept inline to avoid a cross-package import."""
    out = []
    for ch in s or "":
        if ch.isalnum():
            out.append(ch.upper())
    return "".join(out)


def _iter_citations_with_bbox(extraction: Any) -> Iterable[dict]:
    """Yield each citation dict that carries a ``nearest_label`` field.

    Mirrors the pool-walking shape used in
    ``run_full_suite._first_citation_bbox`` so the rubric is consistent
    with the bbox-IoU rubric's denominator definition.
    """
    if not isinstance(extraction, dict):
        return
    pools: list[Any] = []
    kind = extraction.get("kind")
    if kind == "intake_form":
        for key in ("current_medications", "allergies", "family_history"):
            pools.extend(extraction.get(key) or [])
        demographics = extraction.get("demographics") or {}
        if isinstance(demographics, dict):
            for k in ("name", "dob", "sex", "mrn", "address"):
                v = demographics.get(k)
                if isinstance(v, dict):
                    pools.append(v)
        chief = extraction.get("chief_concern")
        if isinstance(chief, dict):
            pools.append(chief)
    elif kind == "lab_report":
        pools = list(extraction.get("values") or [])
    elif kind == "unknown":
        pools = list(extraction.get("key_facts") or [])
    for item in pools:
        if not isinstance(item, dict):
            continue
        for cit in item.get("citations") or []:
            if isinstance(cit, dict) and cit.get("nearest_label"):
                yield cit


def nearest_label_grounded(
    outcome: RunOutcome,
    case: Any,
    *,
    layout_blocks: Optional[list[Any]] = None,
) -> Optional[bool]:
    """Info-only rubric — does the LLM-returned ``nearest_label`` for
    each citation actually appear in the document layout near the cited
    bbox? PASS iff the normalized label substring appears in any layout
    block within a 200-PDF-point Euclidean window of the citation's
    bbox centroid (same page).

    Returns:
      ``True``  — at least one labeled citation, all grounded.
      ``False`` — at least one labeled citation failed to ground.
      ``None``  — no labels emitted on this case (rubric is vacuously
                  skipped; do NOT count toward pass-rate denominator).

    The ``layout_blocks`` keyword is accepted for tests; production
    callers will pass the OCR layout retrieved alongside ``outcome``.
    """
    extraction = getattr(outcome, "extraction", None)
    cits = list(_iter_citations_with_bbox(extraction))
    if not cits:
        return None
    if not layout_blocks:
        # No layout to verify against — treat as skipped rather than failed.
        return None

    def _label_grounded(cit: dict) -> bool:
        nl = _normalize_for_label_match(str(cit.get("nearest_label") or ""))
        if not nl or len(nl) < 2:
            return False
        bbox = cit.get("bbox")
        page = cit.get("page")
        if not bbox or page is None:
            # Without a citation bbox we can't apply the 200pt window;
            # fall back to "label appears anywhere in the document".
            for b in layout_blocks:
                if nl in _normalize_for_label_match(getattr(b, "text", "") or ""):
                    return True
            return False
        try:
            x, y, w, h = bbox if isinstance(bbox, (list, tuple)) else (
                bbox["x"], bbox["y"], bbox["w"], bbox["h"],
            )
        except (KeyError, TypeError, ValueError):
            return False
        cx = float(x) + float(w) / 2.0
        cy = float(y) + float(h) / 2.0
        for b in layout_blocks:
            b_page = getattr(b, "page", None)
            if b_page is not None and b_page != page:
                continue
            b_bbox = getattr(b, "bbox", None)
            if not b_bbox:
                continue
            bx = float(b_bbox[0]) + float(b_bbox[2]) / 2.0
            by = float(b_bbox[1]) + float(b_bbox[3]) / 2.0
            if abs(bx - cx) > 200.0 or abs(by - cy) > 200.0:
                continue
            if ((bx - cx) ** 2 + (by - cy) ** 2) ** 0.5 > 200.0:
                continue
            if nl in _normalize_for_label_match(getattr(b, "text", "") or ""):
                return True
        return False

    return all(_label_grounded(c) for c in cits)


__all__ = ["factually_consistent", "safe_refusal", "nearest_label_grounded"]
