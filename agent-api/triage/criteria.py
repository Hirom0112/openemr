"""Extract boolean/numeric triage criteria from a FHIR patient bundle.

All thresholds are defined as module-level constants so they can be
referenced in tests without parsing the YAML.

Input:  the dict returned by FHIRClient.get_bundle_for_patient()
Output: TriageCriteria dataclass — one field per criteria key in the YAML
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ── Thresholds ────────────────────────────────────────────────────────────────
QSOFA_RR_HIGH = 22          # breaths/min
QSOFA_SBP_LOW = 100         # mmHg
CRITICAL_SPO2_LOW = 92.0    # %
CRITICAL_HR_HIGH = 120      # bpm
CRITICAL_RR_HIGH = 24       # breaths/min
CRITICAL_MAP_LOW = 65.0     # mmHg
PAIN_HIGH_THRESHOLD = 8     # /10

# LOINC codes
LOINC_RR = "9279-1"
LOINC_HR = "8867-4"
LOINC_SPO2 = "2708-6"
LOINC_SBP = "8480-6"
LOINC_GCS_TOTAL = "9269-2"
LOINC_PAIN = "72514-3"
LOINC_MAP = "8478-0"

CRITICAL_LAB_LOINCS = {
    "2823-3",   # Potassium
    "2075-0",   # Chloride
    "2951-2",   # Sodium
    "17861-6",  # Calcium
    "1751-7",   # Albumin (for critical low)
    "33914-3",  # Creatinine
    "14646-4",  # LDH
    "1558-6",   # Glucose
    "4548-4",   # HbA1c
    "718-7",    # Hemoglobin
    "777-3",    # Platelets
    "6690-2",   # WBC
    "5902-2",   # PT
    "5895-8",   # PTT
}

# Tuples are (low_critical_threshold, high_critical_threshold).
# None means that direction is not checked.
# Examples:
#   (None, 6.0)   → value > 6.0 is critical high
#   (3.0, None)   → value < 3.0 is critical low
#   (50.0, 500.0) → value < 50 OR value > 500 is critical
CRITICAL_LAB_VALUE_RANGES: dict[str, tuple[float | None, float | None]] = {
    "2823-3":     (3.0, 6.0),     # K+:      < 3.0 or > 6.0 critical
    "1558-6":     (50.0, 500.0),  # Glucose: < 50  or > 500 critical
    "718-7":      (7.0, None),    # Hgb:     < 7.0 critical low
    "777-3":      (50_000, None), # Plt:     < 50k critical low
    "6690-2":     (None, 30_000), # WBC:     > 30k critical high
}


def _numeric(observation: dict[str, Any]) -> float | None:
    """Extract a numeric value from any FHIR R4 value[x] variant."""
    # valueQuantity — most common for vitals and most labs
    qty = observation.get("valueQuantity")
    if qty and isinstance(qty.get("value"), (int, float)):
        return float(qty["value"])

    # valueInteger — GCS and other discrete integer observations
    vi = observation.get("valueInteger")
    if isinstance(vi, int):
        return float(vi)

    # valueDecimal — FHIR R4 decimal type (distinct from valueQuantity)
    vd = observation.get("valueDecimal")
    if isinstance(vd, (int, float)):
        return float(vd)

    # valueString — some vendor implementations return parseable numeric strings
    # e.g., "6.4" for potassium from certain lab systems. Non-numeric strings
    # (e.g., "positive", "trace") return None.
    vs = observation.get("valueString")
    if isinstance(vs, str):
        try:
            return float(vs)
        except ValueError:
            return None

    # valueRatio — PT/INR and similar coagulation studies
    # Returns numerator/denominator; a denominator of 0 is invalid.
    vr = observation.get("valueRatio")
    if vr:
        num = vr.get("numerator", {}).get("value")
        den = vr.get("denominator", {}).get("value")
        if isinstance(num, (int, float)) and isinstance(den, (int, float)) and den != 0:
            return float(num) / float(den)

    return None


def _interp_codes(observation: dict[str, Any]) -> list[str]:
    """Return interpretation codes for an observation, safe against empty coding lists."""
    codes: list[str] = []
    for interp in observation.get("interpretation", []):
        coding_list = interp.get("coding", [])
        if coding_list:
            code = coding_list[0].get("code", "")
            if code:
                codes.append(code)
    return codes


def _loinc(observation: dict[str, Any]) -> str | None:
    for coding in observation.get("code", {}).get("coding", []):
        if coding.get("system") == "http://loinc.org":
            return coding.get("code")
    return None


def _effective_datetime(observation: dict[str, Any]) -> datetime | None:
    """Parse the observation timestamp for recency comparison."""
    raw = observation.get("effectiveDateTime") or observation.get("effectivePeriod", {}).get("start")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _is_lab_observation(obs: dict[str, Any]) -> bool:
    """Return True only for observations categorised as laboratory.

    FHIR R4 uses system http://terminology.hl7.org/CodeSystem/observation-category
    with code "laboratory". Some implementations omit the system field; the code
    alone is accepted for those cases.

    Vital-sign and imaging observations are excluded by their category code.

    When no category is present at all, the LOINC code is used as a fallback:
    if it is a known lab LOINC it is treated as a lab observation. This handles
    implementations that omit category on lab results.
    """
    categories = obs.get("category", [])
    if categories:
        for cat in categories:
            for coding in cat.get("coding", []):
                code = coding.get("code", "")
                system = coding.get("system", "")
                # Accept if system is the standard observation-category system,
                # or if system is absent (lenient for incomplete implementations).
                if code == "laboratory" and ("observation-category" in system or not system):
                    return True
        return False  # Has categories but none matched "laboratory" — not a lab obs

    # No category at all: fall back to LOINC-based inference.
    loinc = _loinc(obs)
    return loinc in CRITICAL_LAB_LOINCS if loinc else False


def _is_critical_lab(obs: dict[str, Any]) -> bool:
    """Return True if the observation carries a critical interpretation flag
    OR its value exceeds a hardcoded critical threshold for known lab LOINCs."""
    if any(c in ("AA", "LL", "HH", "A") for c in _interp_codes(obs)):
        return True

    loinc = _loinc(obs)
    if loinc and loinc in CRITICAL_LAB_VALUE_RANGES:
        val = _numeric(obs)
        if val is not None:
            low, high = CRITICAL_LAB_VALUE_RANGES[loinc]
            if low is not None and val < low:
                return True
            if high is not None and val > high:
                return True

    return False


@dataclass
class TriageCriteria:
    qsofa_score: int = 0
    critical_lab: bool = False
    rapid_response: bool = False
    abnormal_lab: bool = False
    critical_vital: bool = False
    mental_status_alert: bool = False
    pain_score_high: bool = False
    active_condition: bool = False
    blank_code_status: bool = False
    isolation_precaution: str = "Unknown"
    blank_isolation: bool = False
    latest_vitals: dict[str, float] = field(default_factory=dict)


def extract(bundle: dict[str, Any]) -> TriageCriteria:
    """Derive TriageCriteria from a FHIRClient.get_bundle_for_patient() bundle."""
    criteria = TriageCriteria()
    resources = bundle.get("resources", {})

    observations: list[dict] = [
        entry.get("resource", {})
        for entry in resources.get("Observation", [])
    ]

    # Build vitals dict keeping the most recent observation per LOINC code.
    # Array order in a FHIR bundle is not guaranteed to be chronological.
    vitals: dict[str, float] = {}
    vitals_timestamps: dict[str, datetime] = {}
    _epoch = datetime.min.replace(tzinfo=timezone.utc)

    for obs in observations:
        code = _loinc(obs)
        val = _numeric(obs)
        if code and val is not None:
            ts = _effective_datetime(obs)
            existing_ts = vitals_timestamps.get(code, _epoch)
            # Replace if no existing value, or if this observation is more recent.
            if code not in vitals or (ts is not None and ts > existing_ts):
                vitals[code] = val
                if ts is not None:
                    vitals_timestamps[code] = ts

    criteria.latest_vitals = vitals

    # ── qSOFA ─────────────────────────────────────────────────────────────────
    score = 0
    if vitals.get(LOINC_RR, 0) >= QSOFA_RR_HIGH:
        score += 1
    if vitals.get(LOINC_SBP, 999) <= QSOFA_SBP_LOW:
        score += 1
    # Mental-status change would add +1; approximated via GCS < 15
    gcs = vitals.get(LOINC_GCS_TOTAL)
    if gcs is not None and gcs < 15:
        score += 1
        criteria.mental_status_alert = True
    criteria.qsofa_score = score

    # ── Critical vitals ────────────────────────────────────────────────────────
    spo2 = vitals.get(LOINC_SPO2)
    hr = vitals.get(LOINC_HR)
    rr = vitals.get(LOINC_RR)
    map_val = vitals.get(LOINC_MAP)
    if (
        (spo2 is not None and spo2 < CRITICAL_SPO2_LOW)
        or (hr is not None and hr > CRITICAL_HR_HIGH)
        or (rr is not None and rr > CRITICAL_RR_HIGH)
        or (map_val is not None and map_val < CRITICAL_MAP_LOW)
    ):
        criteria.critical_vital = True

    # ── Labs — restrict interpretation checks to laboratory-category observations
    for obs in observations:
        if not _is_lab_observation(obs):
            continue
        if _is_critical_lab(obs):
            criteria.critical_lab = True
        if any(c in ("H", "L", "HH", "LL", "A", "AA") for c in _interp_codes(obs)):
            criteria.abnormal_lab = True

    # ── Pain ──────────────────────────────────────────────────────────────────
    for obs in observations:
        if _loinc(obs) == LOINC_PAIN:
            val = _numeric(obs)
            if val is not None and val >= PAIN_HIGH_THRESHOLD:
                criteria.pain_score_high = True

    # ── Active conditions ─────────────────────────────────────────────────────
    conditions = [
        entry.get("resource", {})
        for entry in resources.get("Condition", [])
    ]
    if conditions:
        criteria.active_condition = True

    # ── Blank code status ─────────────────────────────────────────────────────
    # Look for an Observation with LOINC 81638-3.  Absence = blank code status.
    code_status_present = any(
        _loinc(entry.get("resource", entry)) == "81638-3"
        for entry in resources.get("Observation", [])
    )
    if not code_status_present:
        criteria.blank_code_status = True

    # ── Isolation precaution ──────────────────────────────────────────────────
    # Look for a Flag resource.  Absence = Unknown (never silently treat as
    # "No Isolation Required" — unknown isolation is a safety gap, not clean).
    flags = [
        entry.get("resource", entry)
        for entry in resources.get("Flag", [])
    ]
    if flags:
        flag = flags[0]
        status = flag.get("status", "")
        code_text = flag.get("code", {}).get("text", "")
        if status == "active" and code_text:
            criteria.isolation_precaution = code_text
            criteria.blank_isolation = False
        elif status == "inactive" and code_text:
            criteria.isolation_precaution = code_text
            criteria.blank_isolation = False
        else:
            criteria.isolation_precaution = "Unknown"
            criteria.blank_isolation = True
    else:
        criteria.isolation_precaution = "Unknown"
        criteria.blank_isolation = True

    return criteria
