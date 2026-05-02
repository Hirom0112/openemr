"""Unit tests for triage/criteria.py — extractor code paths.

Covers newly audited paths: valueDecimal, valueString, valueRatio,
safe interpretation coding, all CRITICAL_LAB_VALUE_RANGES thresholds,
uncategorized lab observation fallback, and most-recent-vital precedence.

All tests are isolated — no FHIR network calls, no fixtures from disk.
"""

from __future__ import annotations

import pytest

from triage.criteria import (
    CRITICAL_LAB_VALUE_RANGES,
    LOINC_HR,
    LOINC_RR,
    LOINC_SBP,
    LOINC_SPO2,
    _interp_codes,
    _is_critical_lab,
    _is_lab_observation,
    _numeric,
    extract,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _obs(loinc: str, value_field: dict, *, category: str | None = "laboratory",
         system: str = "http://terminology.hl7.org/CodeSystem/observation-category",
         interpretation: list[dict] | None = None,
         effective: str | None = None) -> dict:
    """Build a minimal FHIR Observation dict for testing."""
    obs: dict = {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
    }
    obs.update(value_field)
    if category is not None:
        obs["category"] = [{"coding": [{"system": system, "code": category}]}]
    if interpretation:
        obs["interpretation"] = interpretation
    if effective:
        obs["effectiveDateTime"] = effective
    return obs


def _bundle(*obs_list: dict) -> dict:
    """Wrap observations in a minimal bundle structure."""
    return {
        "resources": {
            "Observation": [{"resource": o} for o in obs_list],
            "Condition": [],
        }
    }


def _interp_entry(code: str) -> dict:
    return {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": code}]}


# ── _numeric() new code paths ─────────────────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestNumericExtraction:
    def test_value_quantity_integer_value(self):
        obs = {"valueQuantity": {"value": 97, "unit": "%"}}
        assert _numeric(obs) == 97.0

    def test_value_quantity_float_value(self):
        obs = {"valueQuantity": {"value": 6.4, "unit": "mEq/L"}}
        assert _numeric(obs) == 6.4

    def test_value_integer(self):
        obs = {"valueInteger": 14}
        assert _numeric(obs) == 14.0

    def test_value_decimal(self):
        # FHIR R4 valueDecimal — distinct from valueQuantity
        obs = {"valueDecimal": 3.14}
        assert _numeric(obs) == 3.14

    def test_value_decimal_integer_typed(self):
        obs = {"valueDecimal": 5}
        assert _numeric(obs) == 5.0

    def test_value_string_parseable(self):
        # Some vendor implementations return numeric strings
        obs = {"valueString": "6.4"}
        assert _numeric(obs) == pytest.approx(6.4)

    def test_value_string_not_numeric_returns_none(self):
        # "positive", "trace", etc. should return None, not raise
        obs = {"valueString": "positive"}
        assert _numeric(obs) is None

    def test_value_string_empty_returns_none(self):
        obs = {"valueString": ""}
        assert _numeric(obs) is None

    def test_value_ratio_normal(self):
        # PT/INR: numerator=12.5s, denominator=11.0s (normal)
        obs = {
            "valueRatio": {
                "numerator": {"value": 12.5, "unit": "s"},
                "denominator": {"value": 11.0, "unit": "s"},
            }
        }
        result = _numeric(obs)
        assert result is not None
        assert result == pytest.approx(12.5 / 11.0)

    def test_value_ratio_zero_denominator_returns_none(self):
        obs = {
            "valueRatio": {
                "numerator": {"value": 5.0},
                "denominator": {"value": 0},
            }
        }
        assert _numeric(obs) is None

    def test_no_value_field_returns_none(self):
        obs = {"code": {"coding": []}}
        assert _numeric(obs) is None


# ── _interp_codes() — safe against empty coding lists ────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestInterpCodes:
    def test_normal_interpretation(self):
        obs = {"interpretation": [_interp_entry("HH")]}
        assert "HH" in _interp_codes(obs)

    def test_empty_coding_list_does_not_raise(self):
        # Previously would IndexError on coding[0] when coding=[]
        obs = {"interpretation": [{"coding": []}]}
        result = _interp_codes(obs)
        assert result == []

    def test_missing_interpretation_key(self):
        obs: dict = {}
        assert _interp_codes(obs) == []

    def test_multiple_interpretations(self):
        obs = {
            "interpretation": [
                _interp_entry("HH"),
                _interp_entry("A"),
            ]
        }
        codes = _interp_codes(obs)
        assert "HH" in codes
        assert "A" in codes

    def test_interpretation_missing_code_key(self):
        obs = {"interpretation": [{"coding": [{"system": "x"}]}]}
        result = _interp_codes(obs)
        assert result == []


# ── _is_lab_observation() — category and fallback paths ──────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestIsLabObservation:
    def test_laboratory_category_standard_system(self):
        obs = _obs("2823-3", {"valueQuantity": {"value": 4.0}})
        assert _is_lab_observation(obs) is True

    def test_vital_signs_category_excluded(self):
        obs = _obs(LOINC_RR, {"valueQuantity": {"value": 18}}, category="vital-signs")
        assert _is_lab_observation(obs) is False

    def test_imaging_category_excluded(self):
        obs = _obs("24627-2", {"valueString": "clear"}, category="imaging")
        assert _is_lab_observation(obs) is False

    def test_laboratory_category_no_system(self):
        # Some implementations omit the system — still accept "laboratory"
        obs = _obs("2823-3", {"valueQuantity": {"value": 4.0}}, system="")
        assert _is_lab_observation(obs) is True

    def test_uncategorized_known_lab_loinc_treated_as_lab(self):
        # No category field at all, but LOINC is in CRITICAL_LAB_LOINCS
        obs = _obs("2823-3", {"valueQuantity": {"value": 6.4}}, category=None)
        assert _is_lab_observation(obs) is True

    def test_uncategorized_vital_loinc_not_treated_as_lab(self):
        # No category, LOINC is a vital sign — should NOT be treated as lab
        obs = _obs(LOINC_HR, {"valueQuantity": {"value": 80}}, category=None)
        assert _is_lab_observation(obs) is False

    def test_uncategorized_unknown_loinc_not_treated_as_lab(self):
        obs = _obs("99999-9", {"valueQuantity": {"value": 1.0}}, category=None)
        assert _is_lab_observation(obs) is False


# ── _is_critical_lab() — all CRITICAL_LAB_VALUE_RANGES thresholds ────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestCriticalLabThresholds:
    def test_potassium_critical_high(self):
        obs = _obs("2823-3", {"valueQuantity": {"value": 6.1}},
                   interpretation=[_interp_entry("HH")])
        assert _is_critical_lab(obs) is True

    def test_potassium_critical_high_by_value_no_flag(self):
        # Value alone exceeds threshold — no interpretation flag present
        obs = _obs("2823-3", {"valueQuantity": {"value": 6.1}})
        assert _is_critical_lab(obs) is True

    def test_potassium_critical_low(self):
        obs = _obs("2823-3", {"valueQuantity": {"value": 2.8}})
        assert _is_critical_lab(obs) is True

    def test_potassium_normal_not_critical(self):
        obs = _obs("2823-3", {"valueQuantity": {"value": 4.2}})
        assert _is_critical_lab(obs) is False

    def test_glucose_critical_low(self):
        obs = _obs("1558-6", {"valueQuantity": {"value": 45.0}})
        assert _is_critical_lab(obs) is True

    def test_glucose_critical_high(self):
        obs = _obs("1558-6", {"valueQuantity": {"value": 520.0}})
        assert _is_critical_lab(obs) is True

    def test_glucose_normal_not_critical(self):
        obs = _obs("1558-6", {"valueQuantity": {"value": 110.0}})
        assert _is_critical_lab(obs) is False

    def test_hemoglobin_critical_low(self):
        obs = _obs("718-7", {"valueQuantity": {"value": 6.5}})
        assert _is_critical_lab(obs) is True

    def test_hemoglobin_normal_not_critical(self):
        obs = _obs("718-7", {"valueQuantity": {"value": 11.0}})
        assert _is_critical_lab(obs) is False

    def test_platelets_critical_low(self):
        obs = _obs("777-3", {"valueQuantity": {"value": 40_000}})
        assert _is_critical_lab(obs) is True

    def test_platelets_normal_not_critical(self):
        obs = _obs("777-3", {"valueQuantity": {"value": 150_000}})
        assert _is_critical_lab(obs) is False

    def test_wbc_critical_high(self):
        obs = _obs("6690-2", {"valueQuantity": {"value": 32_000}})
        assert _is_critical_lab(obs) is True

    def test_wbc_normal_not_critical(self):
        obs = _obs("6690-2", {"valueQuantity": {"value": 8_000}})
        assert _is_critical_lab(obs) is False

    def test_interpretation_flag_alone_triggers_critical(self):
        # LL flag on sodium — interpretation alone is sufficient
        obs = _obs("2951-2", {"valueQuantity": {"value": 131.0}},
                   interpretation=[_interp_entry("LL")])
        assert _is_critical_lab(obs) is True

    def test_critical_lab_via_valuestring(self):
        # K+ returned as a string "6.5" from a vendor system
        obs = _obs("2823-3", {"valueString": "6.5"})
        assert _is_critical_lab(obs) is True


# ── extract() — most-recent vital wins ───────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestMostRecentVitalPrecedence:
    def test_later_timestamp_wins(self):
        # Two HR observations: older=55 (normal), newer=130 (critical).
        # Anchor on now() so vitals are within the 24h freshness window.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        older = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        newer = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        older_hr = _obs(LOINC_HR, {"valueQuantity": {"value": 55}},
                        category="vital-signs", effective=older)
        newer_hr = _obs(LOINC_HR, {"valueQuantity": {"value": 130}},
                        category="vital-signs", effective=newer)
        bundle = _bundle(older_hr, newer_hr)
        criteria = extract(bundle)
        assert criteria.latest_vitals.get(LOINC_HR) == 130.0
        assert criteria.critical_vital is True

    def test_earlier_timestamp_does_not_overwrite(self):
        # Array order has older first — newer (high value) should win.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        newer = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        older = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        newer_rr = _obs(LOINC_RR, {"valueQuantity": {"value": 26}},
                        category="vital-signs", effective=newer)
        older_rr = _obs(LOINC_RR, {"valueQuantity": {"value": 14}},
                        category="vital-signs", effective=older)
        # Bundle has newer first in array — older should NOT overwrite
        bundle = _bundle(newer_rr, older_rr)
        criteria = extract(bundle)
        assert criteria.latest_vitals.get(LOINC_RR) == 26.0
        assert criteria.qsofa_score >= 1

    def test_no_timestamp_first_value_kept(self):
        # When neither observation has a timestamp, the first one is kept
        first_spo2 = _obs(LOINC_SPO2, {"valueQuantity": {"value": 88}},
                          category="vital-signs")
        second_spo2 = _obs(LOINC_SPO2, {"valueQuantity": {"value": 97}},
                           category="vital-signs")
        bundle = _bundle(first_spo2, second_spo2)
        criteria = extract(bundle)
        # Without timestamps, insertion order wins — first value (88) is kept
        assert criteria.latest_vitals.get(LOINC_SPO2) == 88.0


# ── extract() — pain freshness gate ──────────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestPainFreshness:
    """Pain reading must be recent (≤ 4h) to fire pain_score_high.

    Regression: previously the pain handler iterated raw observations and
    would fire on a multi-day-old severe-pain reading.
    """

    def test_recent_severe_pain_fires(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pain = _obs("72514-3", {"valueQuantity": {"value": 9}},
                    category="vital-signs", effective=recent)
        criteria = extract(_bundle(pain))
        assert criteria.pain_score_high is True

    def test_stale_severe_pain_does_not_fire(self):
        # Pain reading older than the freshness window must not fire.
        from datetime import datetime, timedelta, timezone
        from triage.criteria import PAIN_FRESHNESS_WINDOW
        now = datetime.now(timezone.utc)
        stale = (now - PAIN_FRESHNESS_WINDOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pain = _obs("72514-3", {"valueQuantity": {"value": 9}},
                    category="vital-signs", effective=stale)
        criteria = extract(_bundle(pain))
        assert criteria.pain_score_high is False

    def test_pain_just_outside_window_does_not_fire(self):
        # Boundary: just outside the configured pain freshness window does not fire.
        from datetime import datetime, timedelta, timezone
        from triage.criteria import PAIN_FRESHNESS_WINDOW
        now = datetime.now(timezone.utc)
        ts = (now - PAIN_FRESHNESS_WINDOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pain = _obs("72514-3", {"valueQuantity": {"value": 9}},
                    category="vital-signs", effective=ts)
        criteria = extract(_bundle(pain))
        assert criteria.pain_score_high is False


# ── extract() — uncategorized critical lab integration ───────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestUncategorizedLabIntegration:
    def test_uncategorized_critical_k_flagged(self):
        # K+ 6.5 with no category — should still be detected as critical
        critical_k = _obs("2823-3", {"valueQuantity": {"value": 6.5}}, category=None)
        bundle = _bundle(critical_k)
        criteria = extract(bundle)
        assert criteria.critical_lab is True

    def test_uncategorized_vital_not_treated_as_lab(self):
        # HR 130 with no category — should affect critical_vital, NOT critical_lab
        high_hr = _obs(LOINC_HR, {"valueQuantity": {"value": 130}}, category=None)
        bundle = _bundle(high_hr)
        criteria = extract(bundle)
        assert criteria.critical_vital is True
        assert criteria.critical_lab is False


# ── extract() — isolation precaution ─────────────────────────────────────────

def _flag(status: str, code_text: str, snomed_code: str | None = None) -> dict:
    """Build a minimal FHIR Flag dict for testing."""
    resource: dict = {
        "resourceType": "Flag",
        "status": status,
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/flag-category", "code": "infection"}]}],
        "code": {"text": code_text},
        "subject": {"reference": "Patient/test"},
    }
    if snomed_code:
        resource["code"]["coding"] = [{"system": "http://snomed.info/sct", "code": snomed_code}]
    return resource


def _bundle_with_flag(flag_resource: dict | None) -> dict:
    bundle: dict = {
        "resources": {
            "Observation": [],
            "Condition": [],
            "Flag": [{"resource": flag_resource}] if flag_resource else [],
        }
    }
    return bundle


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestIsolationExtraction:
    def test_active_contact_precautions(self):
        bundle = _bundle_with_flag(
            _flag("active", "Contact Precautions", "409528009")
        )
        criteria = extract(bundle)
        assert criteria.isolation_precaution == "Contact Precautions"
        assert criteria.blank_isolation is False

    def test_active_droplet_precautions(self):
        bundle = _bundle_with_flag(
            _flag("active", "Droplet Precautions", "409527004")
        )
        criteria = extract(bundle)
        assert criteria.isolation_precaution == "Droplet Precautions"
        assert criteria.blank_isolation is False

    def test_active_airborne_precautions(self):
        bundle = _bundle_with_flag(
            _flag("active", "Airborne Precautions", "409526008")
        )
        criteria = extract(bundle)
        assert criteria.isolation_precaution == "Airborne Precautions"
        assert criteria.blank_isolation is False

    def test_inactive_no_isolation_required(self):
        bundle = _bundle_with_flag(
            _flag("inactive", "No Isolation Required")
        )
        criteria = extract(bundle)
        assert criteria.isolation_precaution == "No Isolation Required"
        assert criteria.blank_isolation is False

    def test_missing_flag_returns_unknown_not_clean(self):
        # Absence of Flag must return "Unknown", never "No Isolation Required"
        bundle = _bundle_with_flag(None)
        criteria = extract(bundle)
        assert criteria.isolation_precaution == "Unknown"
        assert criteria.blank_isolation is True

    def test_missing_flag_key_in_resources(self):
        # Bundle without a Flag key at all — same as empty Flag list
        bundle: dict = {"resources": {"Observation": [], "Condition": []}}
        criteria = extract(bundle)
        assert criteria.isolation_precaution == "Unknown"
        assert criteria.blank_isolation is True


# ── BP component shape (OpenEMR 85354-9 with components) ─────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestBloodPressureComponentShape:
    """OpenEMR emits a single BP Observation with code 85354-9 and SBP/DBP
    nested under component[].  Both the flat shape (separate top-level
    SBP/DBP Observations, used by the synthetic bundle JSONs) and the
    component shape must extract correctly."""

    def test_bp_component_shape_extracts_sbp(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bp = {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
            "effectiveDateTime": ts,
            "component": [
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                 "valueQuantity": {"value": 88, "unit": "mmHg"}},
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                 "valueQuantity": {"value": 54, "unit": "mmHg"}},
            ],
        }
        criteria = extract(_bundle(bp))
        assert criteria.latest_vitals.get("8480-6") == 88.0
        assert criteria.latest_vitals.get("8462-4") == 54.0
        # SBP=88 ≤ 100 → qSOFA +1 and circulatory critical vital
        assert criteria.qsofa_score >= 1
        assert criteria.critical_vital_circulatory is True

    def test_flat_bp_shape_still_extracts_sbp(self):
        # Synthetic-bundle shape: SBP as a top-level Observation.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sbp = _obs(LOINC_SBP, {"valueQuantity": {"value": 88, "unit": "mmHg"}},
                   category="vital-signs", effective=ts)
        criteria = extract(_bundle(sbp))
        assert criteria.latest_vitals.get(LOINC_SBP) == 88.0
        assert criteria.qsofa_score >= 1


# ── Chronic-condition matching (ICD-10 variants + text fallback) ─────────────

def _bundle_with_condition(condition: dict) -> dict:
    return {"resources": {"Observation": [], "Condition": [{"resource": condition}]}}


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestChronicConditionMatching:
    def _active_clinical_status(self) -> dict:
        return {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active",
            }],
        }

    def test_icd10_variant_system_uri_matches(self):
        # YAML stores http://hl7.org/fhir/sid/icd-10; OpenEMR may emit -cm.
        cond = {
            "resourceType": "Condition",
            "clinicalStatus": self._active_clinical_status(),
            "code": {"coding": [{
                "system": "http://hl7.org/fhir/sid/icd-10-cm",
                "code": "I10",
            }]},
        }
        criteria = extract(_bundle_with_condition(cond))
        assert criteria.active_condition is True

    def test_canonical_icd10_system_still_matches(self):
        cond = {
            "resourceType": "Condition",
            "clinicalStatus": self._active_clinical_status(),
            "code": {"coding": [{
                "system": "http://hl7.org/fhir/sid/icd-10",
                "code": "I10",
            }]},
        }
        criteria = extract(_bundle_with_condition(cond))
        assert criteria.active_condition is True

    def test_text_only_chronic_label_matches(self):
        # OpenEMR emits Conditions with only code.text and no coding[]; we
        # match against curated label substrings (case-insensitive).
        cond = {
            "resourceType": "Condition",
            "clinicalStatus": self._active_clinical_status(),
            "code": {"text": "Essential hypertension — chronic management"},
        }
        criteria = extract(_bundle_with_condition(cond))
        assert criteria.active_condition is True

    def test_text_only_acute_does_not_match(self):
        cond = {
            "resourceType": "Condition",
            "clinicalStatus": self._active_clinical_status(),
            "code": {"text": "Acute pneumonia"},
        }
        criteria = extract(_bundle_with_condition(cond))
        assert criteria.active_condition is False

    def test_inactive_chronic_does_not_match(self):
        cond = {
            "resourceType": "Condition",
            "clinicalStatus": {"coding": [{"code": "resolved"}]},
            "code": {"text": "Essential hypertension"},
        }
        criteria = extract(_bundle_with_condition(cond))
        assert criteria.active_condition is False
