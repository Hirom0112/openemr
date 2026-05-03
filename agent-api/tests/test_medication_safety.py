"""Eval suite — Medication safety checks (deterministic).

Covers 7 of the 47 required tests.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from medication.safety import run_safety_checks, MedicationSafetyReport
from agent.tools import _normalize_patient_id


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


@pytest.mark.hard_failure
class TestPatientIdNormalization:
    """Verify the single-patient tool entry-point normalization helper.

    Covers the 2026-05 latency fix: synthetic ``pt-NNN`` ids must collapse
    to the OpenEMR pid form before the dispatcher's census-scope check or
    downstream tools see them, eliminating the 6-12s misroute round-trip.
    """

    def test_synthetic_to_pid_strips_zero_pad(self):
        assert _normalize_patient_id("pt-008", {}) == "8"
        assert _normalize_patient_id("pt-2", {}) == "2"
        assert _normalize_patient_id("pt-100", {}) == "100"

    def test_already_pid_passes_through(self):
        assert _normalize_patient_id("8", {}) == "8"
        assert _normalize_patient_id("100", {}) == "100"

    def test_uuid_passes_through_unchanged(self):
        uuid = "9f86d081-884c-7d65-9b27-2bcccaf09c5a"
        assert _normalize_patient_id(uuid, {}) == uuid

    def test_none_returns_none(self):
        assert _normalize_patient_id(None, {}) is None

    def test_empty_string_returns_empty(self):
        # Empty string is unrecognised but must not raise — the calling
        # tool surfaces a clean validation error from its own input check.
        assert _normalize_patient_id("", {}) == ""

    def test_get_medication_safety_normalizes_synthetic_pid(self):
        """End-to-end: pt-008 must resolve to "8" before fhir_client is called."""
        from agent import tools as tools_mod

        captured: dict[str, str] = {}

        async def _fake_get_bundle(pid: str) -> dict:
            captured["pid"] = pid
            return {"resources": {"MedicationRequest": [], "AllergyIntolerance": [], "Observation": []}}

        async def _fake_add_summary(report, langfuse=None):
            return report

        with patch.object(tools_mod.fhir_client, "get_bundle_for_patient", side_effect=_fake_get_bundle), \
             patch("agent.tools.add_llm_summary", side_effect=_fake_add_summary):
            asyncio.new_event_loop().run_until_complete(
                tools_mod.get_medication_safety(
                    {"patient_id": "pt-008"},
                    {},
                ),
            )

        assert captured["pid"] == "8", "synthetic pt-008 must normalize to OpenEMR pid '8'"
