"""Minimal verification layer — domain constraints.

Runs after the LLM explanation is generated and before the response is
returned to the caller.  Each check is a hard rule the LLM cannot override.

Constraints applied across all LLM surfaces:
  1. Never say "no known allergies" if the allergy section is blank.
  2. Always flag blank code status.
  3. Mark critical values older than 30 minutes as potentially stale.
  4. Strip any text that includes clinical recommendations
     ("should", "consider", "recommend", "order", "administer", …).

UC-3 (conversation answers): strip recommendation language.
UC-4 (safety summary): strip recommendations; block unsupported "safe"
  assertions when safety flags exist — safety status is determined by
  the deterministic rules engine, not the LLM.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_RECOMMENDATION_WORDS = re.compile(
    r"\b(should|consider|recommend|order|administer|prescribe|initiate|start|increase|decrease|titrate)\b",
    re.IGNORECASE,
)

# Phrases that assert a drug or the medication list is safe — blocked when
# safety flags are present because the LLM does not determine safety status.
_SAFE_ASSERTION = re.compile(
    r"\b(is safe|are safe|no safety concerns?|no concerns?|well.tolerated|"
    r"no drug interactions? (?:detected|found|identified|present)|"
    r"no medication safety (?:issues?|concerns?|flags?))\b",
    re.IGNORECASE,
)

_STALE_THRESHOLD = timedelta(minutes=30)


def _strip_recommendations(text: str, patient_id: str) -> str:
    if _RECOMMENDATION_WORDS.search(text):
        logger.warning(
            "Explanation contains recommendation language — stripped",
            extra={"patient_id": patient_id, "original": text},
        )
        # Replace the offending sentence(s) with a neutral continuation marker.
        return re.sub(r"[^.!?]*\b(should|consider|recommend|order|administer|prescribe|initiate|start|increase|decrease|titrate)\b[^.!?]*[.!?]?", "", text, flags=re.IGNORECASE).strip()
    return text


def _check_allergy_blank(entry: dict[str, Any], bundle: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    allergies = bundle.get("resources", {}).get("AllergyIntolerance", [])
    if not allergies:
        explanation = entry.get("explanation", "")
        if "no known allerg" in explanation.lower():
            warnings.append("CONSTRAINT_VIOLATION: cannot assert 'no known allergies' — allergy section is blank")
    return warnings


def _check_stale_critical(bundle: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    for obs_entry in bundle.get("resources", {}).get("Observation", []):
        obs = obs_entry.get("resource", obs_entry)
        interps = [
            i.get("coding", [{}])[0].get("code", "")
            for i in obs.get("interpretation", [])
        ]
        if any(c in ("HH", "LL", "AA") for c in interps):
            effective = obs.get("effectiveDateTime") or obs.get("effectivePeriod", {}).get("end")
            if effective:
                try:
                    obs_time = datetime.fromisoformat(effective.replace("Z", "+00:00"))
                    if now - obs_time > _STALE_THRESHOLD:
                        code = obs.get("code", {}).get("coding", [{}])[0].get("code", "?")
                        warnings.append(f"STALE_CRITICAL: {code} critical value is >{_STALE_THRESHOLD.seconds // 60} min old")
                except ValueError:
                    pass
    return warnings


def verify_conversation_answer(answer: str, patient_id: str) -> str:
    """Strip recommendation language from a UC-3 conversation answer.

    The system prompt forbids recommendations, but this is the hard enforcement
    layer — the LLM cannot override it.
    """
    return _strip_recommendations(answer, patient_id)


def verify_safety_summary(summary: str, patient_id: str, *, has_flags: bool) -> str:
    """Verify a UC-4 medication safety LLM summary.

    Strips recommendation language and, when safety flags exist, removes any
    sentence that asserts a medication or the list is 'safe'.  Safety status
    is determined by the deterministic rules engine; the LLM summary may only
    describe flags, not override them.
    """
    summary = _strip_recommendations(summary, patient_id)
    if has_flags and _SAFE_ASSERTION.search(summary):
        logger.warning(
            "Safety summary contains unsupported 'safe' assertion — stripped",
            extra={"patient_id": patient_id, "original": summary},
        )
        summary = re.sub(
            r"[^.!?]*" + _SAFE_ASSERTION.pattern + r"[^.!?]*[.!?]?",
            "",
            summary,
            flags=re.IGNORECASE,
        ).strip()
    return summary


def verify_triage_entry(entry: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """Apply domain constraints to a single annotated census entry.

    Returns the entry with:
      - explanation sanitized
      - warnings list appended
    """
    patient_id = entry.get("patient_id", "unknown")
    explanation = entry.get("explanation", "")

    explanation = _strip_recommendations(explanation, patient_id)

    warnings: list[str] = []
    warnings.extend(_check_allergy_blank(entry, bundle))
    warnings.extend(_check_stale_critical(bundle))

    if entry.get("blank_code_status"):
        warnings.append("BLANK_CODE_STATUS: code-status field is empty — clarification required")

    if entry.get("blank_isolation"):
        warnings.append("BLANK_ISOLATION: isolation precaution field is unknown — verify before entering room")

    verified = {**entry, "explanation": explanation}
    if warnings:
        verified["verification_warnings"] = warnings
        logger.info(
            "Verification warnings attached",
            extra={"patient_id": patient_id, "warnings": warnings},
        )

    return verified
