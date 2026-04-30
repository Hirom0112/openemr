"""Eval suite — Query router classifier tests (deterministic).

Covers 12 of the 47 required tests.
Hard failure gate: 100% pass required.
"""

import pytest

from query.router import _classify, QueryRoute


@pytest.mark.clinical_accuracy
class TestQueryRouterClassifier:
    @pytest.mark.hard_failure
    def test_potassium_routes_to_observation_lab(self):
        result = _classify("What is her potassium?")
        assert result is not None
        assert result.resource == "Observation"
        assert result.params.get("category") == "laboratory"

    @pytest.mark.hard_failure
    def test_blood_pressure_routes_to_vital_signs(self):
        result = _classify("What is the blood pressure?")
        assert result is not None
        assert result.resource == "Observation"
        assert result.params.get("category") == "vital-signs"

    def test_heart_rate_routes_to_vital_signs(self):
        result = _classify("What is the HR?")
        assert result is not None
        assert result.resource == "Observation"
        assert result.params.get("category") == "vital-signs"

    @pytest.mark.hard_failure
    def test_medication_routes_to_medication_request(self):
        result = _classify("What medications is she on?")
        assert result is not None
        assert result.resource == "MedicationRequest"

    def test_specific_drug_routes_to_medication_request(self):
        result = _classify("Is she on metoprolol?")
        assert result is not None
        assert result.resource == "MedicationRequest"

    @pytest.mark.hard_failure
    def test_allergy_routes_to_allergy_intolerance(self):
        result = _classify("Does she have any penicillin allergy?")
        assert result is not None
        assert result.resource == "AllergyIntolerance"

    def test_diagnosis_routes_to_condition(self):
        result = _classify("What are her active diagnoses?")
        assert result is not None
        assert result.resource == "Condition"

    def test_sepsis_routes_to_condition(self):
        result = _classify("Is sepsis in the problem list?")
        assert result is not None
        assert result.resource == "Condition"

    def test_creatinine_routes_to_observation_lab(self):
        result = _classify("What is the latest creatinine?")
        assert result is not None
        assert result.resource == "Observation"
        assert result.params.get("category") == "laboratory"

    def test_spo2_routes_to_vital_signs(self):
        result = _classify("What is the SpO2?")
        assert result is not None
        assert result.resource == "Observation"
        assert result.params.get("category") == "vital-signs"

    def test_imaging_routes_to_diagnostic_report(self):
        result = _classify("What did the chest X-ray show?")
        assert result is not None
        assert result.resource == "DiagnosticReport"

    @pytest.mark.hard_failure
    def test_unrecognized_query_returns_none(self):
        result = _classify("What is the weather today?")
        assert result is None

    def test_high_confidence_for_specific_labs(self):
        result = _classify("What is the WBC?")
        assert result is not None
        assert result.confidence >= 0.9
