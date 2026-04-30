"""Eval suite — Medication safety checks (deterministic).

Covers 7 of the 47 required tests.
"""

import pytest
from medication.safety import run_safety_checks, MedicationSafetyReport


def _med(name: str) -> dict:
    return {"medicationCodeableConcept": {"coding": [{"display": name}], "text": name}}


def _allergy(substance: str) -> dict:
    return {"code": {"coding": [{"display": substance}], "text": substance}}


def _creatinine_obs(value: float) -> dict:
    return {
        "code": {"coding": [{"system": "http://loinc.org", "code": "33914-3"}]},
        "valueQuantity": {"value": value, "unit": "mg/dL"},
    }


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestMedicationSafety:
    def test_allergy_conflict_detected(self):
        report = run_safety_checks(
            "pt-1",
            medications=[_med("Penicillin G")],
            allergies=[_allergy("Penicillin")],
            observations=[],
        )
        assert any(f.code == "ALLERGY_CONFLICT" for f in report.flags)

    def test_no_false_allergy_conflict(self):
        report = run_safety_checks(
            "pt-1",
            medications=[_med("Metoprolol")],
            allergies=[_allergy("Penicillin")],
            observations=[],
        )
        assert not any(f.code == "ALLERGY_CONFLICT" for f in report.flags)

    def test_renal_interaction_at_high_creatinine(self):
        report = run_safety_checks(
            "pt-1",
            medications=[_med("Metformin")],
            allergies=[],
            observations=[_creatinine_obs(2.1)],
        )
        assert any(f.code == "RENAL_INTERACTION" for f in report.flags)

    def test_no_renal_interaction_normal_creatinine(self):
        report = run_safety_checks(
            "pt-1",
            medications=[_med("Metformin")],
            allergies=[],
            observations=[_creatinine_obs(0.9)],
        )
        assert not any(f.code == "RENAL_INTERACTION" for f in report.flags)

    def test_high_alert_heparin_flagged(self):
        report = run_safety_checks("pt-1", medications=[_med("Heparin")], allergies=[], observations=[])
        assert any(f.code == "HIGH_ALERT_MED" for f in report.flags)

    def test_high_alert_insulin_flagged(self):
        report = run_safety_checks("pt-1", medications=[_med("Insulin glargine")], allergies=[], observations=[])
        assert any(f.code == "HIGH_ALERT_MED" for f in report.flags)

    def test_severity_ordering_high_before_info(self):
        report = run_safety_checks(
            "pt-1",
            medications=[_med("Heparin")],
            allergies=[_allergy("Heparin")],
            observations=[],
        )
        severities = [f.severity for f in report.flags]
        high_idx = severities.index("HIGH")
        info_idx = severities.index("INFO")
        assert high_idx < info_idx
