"""Final-response verification gate for the dispatcher (Phase 5).

Applied after every dispatcher end_turn before the response is returned to
the caller.  All seven checks are hard rules — none may be softened or made
optional.

Checks:
  1. NKDA block              — strips "no known allergies" when allergy data is incomplete
  2. Blank code-status flag  — prepends warning when code status is Unknown/blank
  3. Blank isolation flag    — prepends warning when isolation is Unknown
  4. Stale critical value    — prepends staleness flag when critical value >30 min old
  5. SYSTEM_BOUNDARY_TOKEN   — blocks entire response on canary detection
  6. Claim-without-citation  — strips specific values absent from FHIR context
  7. Recommendation language — replaces prescriptive phrases with a removal notice
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

SYSTEM_BOUNDARY_TOKEN = "SYSTEM_BOUNDARY_TOKEN=cc-pilot-v1"
_STALE_THRESHOLD = timedelta(minutes=30)

# Recommendation language patterns (case-insensitive)
_RECOMMENDATION_PATTERNS: list[str] = [
    r"I recommend\b[^.!?]*[.!?]?",
    r"you should\b[^.!?]*[.!?]?",
    r"consider ordering\b[^.!?]*[.!?]?",
    r"consider prescribing\b[^.!?]*[.!?]?",
    r"I suggest prescribing\b[^.!?]*[.!?]?",
    r"I suggest\b[^.!?]*[.!?]?",
    r"transfer to (?:the )?ICU\b[^.!?]*[.!?]?",
    r"initiate \w+[^.!?]*[.!?]?",
]
_RECOMMENDATION_RE = re.compile(
    "|".join(f"(?:{p})" for p in _RECOMMENDATION_PATTERNS),
    re.IGNORECASE,
)

_NKDA_RE = re.compile(r"no known allerg(?:y|ies)\b[^.!?]*[.!?]?", re.IGNORECASE)


@dataclasses.dataclass
class VerificationResult:
    passed: bool
    blocked: bool
    violations: list[str]
    modified_response: dict[str, Any]
    physician_message: str = ""


def verify_dispatcher_response(
    response: dict[str, Any],
    fhir_context: dict[str, Any],
    patient_id: str | None,
) -> VerificationResult:
    """Apply all hard verification rules to a final dispatcher response.

    Parameters
    ----------
    response:     The typed response envelope {type, data, narrative, citations, metadata}.
    fhir_context: The FHIR bundle/resources dict that was in context for this response.
    patient_id:   The active patient ID, or None for multi-patient responses.

    Returns
    -------
    VerificationResult with modified_response containing the cleaned output.
    """
    violations: list[str] = []
    narrative: str = response.get("narrative") or ""
    data: Any = response.get("data")

    # ── Check 5: SYSTEM_BOUNDARY_TOKEN (check first — if triggered, block everything) ──
    narrative_str = narrative if isinstance(narrative, str) else json.dumps(narrative)
    data_str = json.dumps(data) if data is not None else ""
    if SYSTEM_BOUNDARY_TOKEN in narrative_str or SYSTEM_BOUNDARY_TOKEN in data_str:
        logger.error(
            "SYSTEM_BOUNDARY_TOKEN detected in dispatcher response — blocking",
            extra={"patient_id": patient_id},
        )
        return VerificationResult(
            passed=False,
            blocked=True,
            violations=["SYSTEM_BOUNDARY_TOKEN detected"],
            modified_response={},
            physician_message="Response blocked by safety check. Please view the chart directly.",
        )

    resources: dict[str, list[Any]] = fhir_context.get("resources", {})

    # ── Check 1: NKDA block ───────────────────────────────────────────────────
    if _NKDA_RE.search(narrative):
        allergy_entries = resources.get("AllergyIntolerance", [])
        has_incomplete = _has_incomplete_allergy(allergy_entries)
        if has_incomplete or not allergy_entries:
            violations.append("NKDA_BLOCK: 'no known allergies' stripped — allergy data incomplete")
            narrative = _NKDA_RE.sub("Allergy section has incomplete entries — verify in chart.", narrative)
            logger.warning("NKDA claim stripped", extra={"patient_id": patient_id})

    # ── Check 6: Claim-without-citation strip ─────────────────────────────────
    # Strip numeric lab values that have no matching Observation in fhir_context.
    # Pattern: a value like "K+ 4.2" or "4.2 mEq/L" or similar numeric clinical values.
    narrative, stripped_claims = _strip_uncited_values(narrative, resources)
    if stripped_claims:
        violations.append(f"UNCITED_CLAIMS_STRIPPED: {stripped_claims}")

    # ── Check 7: Recommendation language strip ────────────────────────────────
    if _RECOMMENDATION_RE.search(narrative):
        violations.append("RECOMMENDATION_LANGUAGE_STRIPPED")
        narrative = _RECOMMENDATION_RE.sub(
            "[Clinical decision language removed — verify with chart]", narrative
        )
        logger.warning("Recommendation language stripped", extra={"patient_id": patient_id})

    # ── Check 4: Stale critical value flag ────────────────────────────────────
    stale_flags = _check_stale_critical_values(resources)
    if stale_flags:
        for flag in stale_flags:
            violations.append(f"STALE_CRITICAL: {flag}")
        staleness_prefix = "[STALE VALUE — >30 min old — verify in chart] "
        narrative = staleness_prefix + narrative

    # ── Check 5 (canary already handled above) ────────────────────────────────

    # ── Check 2: Blank code status ────────────────────────────────────────────
    if _response_mentions_code_status(narrative):
        code_status = _extract_code_status(resources)
        if code_status in ("Unknown", "", None):
            violations.append("BLANK_CODE_STATUS")
            narrative = "[CODE STATUS UNKNOWN — verify in chart] " + narrative

    # ── Check 3: Blank isolation flag ────────────────────────────────────────
    if _response_mentions_isolation(narrative):
        isolation = _extract_isolation(resources)
        if isolation in ("Unknown", "", None):
            violations.append("BLANK_ISOLATION")
            narrative = "[ISOLATION STATUS UNKNOWN — verify in chart] " + narrative

    modified_response = {
        **response,
        "narrative": narrative,
    }

    passed = len(violations) == 0
    return VerificationResult(
        passed=passed,
        blocked=False,
        violations=violations,
        modified_response=modified_response,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _has_incomplete_allergy(allergy_entries: list[Any]) -> bool:
    for entry in allergy_entries:
        resource = entry.get("resource", entry) if isinstance(entry, dict) else {}
        # Blank reaction or missing code → incomplete
        reactions = resource.get("reaction", [])
        if not reactions:
            return True
        for reaction in reactions:
            manifestations = reaction.get("manifestation", [])
            if not manifestations:
                return True
            for m in manifestations:
                codings = m.get("coding", [])
                if not codings or not codings[0].get("code"):
                    return True
        # Check type/category
        category = resource.get("category", [])
        if not category:
            return True
    return False


def _check_stale_critical_values(resources: dict[str, list[Any]]) -> list[str]:
    stale: list[str] = []
    now = datetime.now(timezone.utc)
    for obs_entry in resources.get("Observation", []):
        obs = obs_entry.get("resource", obs_entry) if isinstance(obs_entry, dict) else {}
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
                        stale.append(f"{code} critical value >{_STALE_THRESHOLD.seconds // 60} min old")
                except ValueError:
                    pass
    return stale


def _strip_uncited_values(narrative: str, resources: dict[str, list[Any]]) -> tuple[str, list[str]]:
    """Strip numeric clinical values from narrative if no supporting Observation exists.

    Strategy: find patterns like "K+ 4.2", "Na 138", "Cr 1.4 mg/dL" etc.
    If there are NO Observations at all in the FHIR context, strip all such values.
    If there ARE Observations, allow values through (attribution is handled by source_attribution.py).
    This function is the hard backstop for responses where the LLM hallucinated a value
    not present in any FHIR resource.
    """
    observations = resources.get("Observation", [])
    if observations:
        # Context has observations — trust source_attribution for detailed matching
        return narrative, []

    # No observations in context at all — strip numeric lab/vital patterns
    _numeric_clinical = re.compile(
        r"\b(?:K\+?|Na\+?|Cr|BUN|Hgb|WBC|Plt|Lac|pH|pCO2|pO2|HCO3|Mg|Phos|Ca|Glu|INR|PTT|Trop|BNP|PCO2|CO2)\s*[=:]?\s*\d+\.?\d*\s*(?:mEq/L|mg/dL|g/dL|mmol/L|K/uL|ng/mL|pg/mL|IU/L|U/L|%)?\b",
        re.IGNORECASE,
    )
    stripped: list[str] = []
    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        stripped.append(m.group(0))
        return "[value removed — no source in context]"

    cleaned = _numeric_clinical.sub(_replace, narrative)
    return cleaned, stripped


def _response_mentions_code_status(narrative: str) -> bool:
    return bool(re.search(r"\b(?:code status|DNR|DNI|full code|comfort care|POLST)\b", narrative, re.IGNORECASE))


def _extract_code_status(resources: dict[str, list[Any]]) -> str | None:
    for obs_entry in resources.get("Observation", []):
        obs = obs_entry.get("resource", obs_entry) if isinstance(obs_entry, dict) else {}
        for coding in obs.get("code", {}).get("coding", []):
            if coding.get("code") == "81638-3":
                val = obs.get("valueCodeableConcept", {}).get("text") or obs.get("valueString")
                return val or "Unknown"
    return None


def _extract_isolation(resources: dict[str, list[Any]]) -> str | None:
    for flag_entry in resources.get("Flag", []):
        flag = flag_entry.get("resource", flag_entry) if isinstance(flag_entry, dict) else {}
        if flag.get("resourceType") == "Flag":
            return flag.get("code", {}).get("text") or "Unknown"
    return None


def _response_mentions_isolation(narrative: str) -> bool:
    return bool(re.search(
        r"\b(?:isolation|contact precaution|droplet|airborne|PPE|gown|mask required)\b",
        narrative, re.IGNORECASE,
    ))
