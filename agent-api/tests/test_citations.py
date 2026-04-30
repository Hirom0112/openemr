"""Tests for Citation model and claim taxonomy (docs/UX_SPEC.md §6).

All tests are hard_failure — citation infrastructure is a safety-critical
component of the verification layer.
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.citation import (
    CLINICAL_CLAIM_CLASSES,
    Citation,
    CitationList,
    citations_for_fhir_resource,
    is_conversational_text,
)
from verification.source_attribution import extract_citations


# ── Fixtures ──────────────────────────────────────────────────────────────────

PATIENT_ID = "pt-test-001"


def _obs_lab(resource_id: str, display: str, value: str, unit: str) -> dict:
    return {
        "resourceType": "Observation",
        "id": resource_id,
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"text": display, "coding": [{"code": "2823-3", "display": display}]},
        "valueQuantity": {"value": float(value), "unit": unit},
        "effectiveDateTime": "2026-04-29T06:00:00Z",
    }


def _obs_vital(resource_id: str, display: str, value: str, unit: str) -> dict:
    return {
        "resourceType": "Observation",
        "id": resource_id,
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
        "code": {"text": display, "coding": [{"code": "9279-1", "display": display}]},
        "valueQuantity": {"value": float(value), "unit": unit},
        "effectiveDateTime": "2026-04-29T06:00:00Z",
    }


def _obs_code_status(resource_id: str) -> dict:
    return {
        "resourceType": "Observation",
        "id": resource_id,
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
        "valueCodeableConcept": {"text": "Full Code"},
        "effectiveDateTime": "2026-04-28T10:00:00Z",
    }


def _med_request(resource_id: str, med_name: str) -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": resource_id,
        "status": "active",
        "medicationCodeableConcept": {"text": med_name},
        "authoredOn": "2026-04-28T08:00:00Z",
    }


def _allergy(resource_id: str, substance: str) -> dict:
    return {
        "resourceType": "AllergyIntolerance",
        "id": resource_id,
        "code": {"text": substance, "coding": [{"display": substance}]},
        "reaction": [{"manifestation": [{"text": "rash"}]}],
        "recordedDate": "2026-04-01T00:00:00Z",
    }


def _isolation_flag(resource_id: str, precaution: str) -> dict:
    return {
        "resourceType": "Flag",
        "id": resource_id,
        "status": "active",
        "code": {
            "coding": [{"system": "http://snomed.info/sct", "code": "409498004", "display": precaution}],
            "text": precaution,
        },
        "period": {"start": "2026-04-28T00:00:00Z"},
    }


def _bundle_with(*resources: dict) -> dict:
    entries: dict[str, list[dict]] = {}
    for r in resources:
        rt = r["resourceType"]
        entries.setdefault(rt, [])
        entries[rt].append({"resource": r})
    return {"resources": entries}


# ── Clinical assertion tests — citations required ─────────────────────────────

@pytest.mark.hard_failure
def test_lab_value_produces_citation_with_lab_value_class() -> None:
    bundle = _bundle_with(_obs_lab("obs-k", "Potassium", "5.9", "mEq/L"))
    citations = extract_citations("K+ 5.9 mEq/L", bundle, PATIENT_ID)
    lab_citations = [c for c in citations if c.claim_class == "lab_value"]
    assert len(lab_citations) >= 1


@pytest.mark.hard_failure
def test_vital_sign_produces_citation_with_vital_class() -> None:
    bundle = _bundle_with(_obs_vital("obs-rr", "Respiratory Rate", "26", "/min"))
    citations = extract_citations("RR 26", bundle, PATIENT_ID)
    vital_citations = [c for c in citations if c.claim_class == "vital"]
    assert len(vital_citations) >= 1


@pytest.mark.hard_failure
def test_medication_produces_citation_with_medication_class() -> None:
    bundle = _bundle_with(_med_request("med-001", "Lisinopril 10mg"))
    citations = extract_citations("Lisinopril 10mg daily", bundle, PATIENT_ID)
    med_citations = [c for c in citations if c.claim_class == "medication"]
    assert len(med_citations) >= 1


@pytest.mark.hard_failure
def test_allergy_produces_citation_with_allergy_class() -> None:
    bundle = _bundle_with(_allergy("allergy-001", "Penicillin"))
    citations = extract_citations("Allergy: Penicillin — rash", bundle, PATIENT_ID)
    allergy_citations = [c for c in citations if c.claim_class == "allergy"]
    assert len(allergy_citations) >= 1


@pytest.mark.hard_failure
def test_code_status_produces_citation_with_code_status_class() -> None:
    bundle = _bundle_with(_obs_code_status("obs-cs"))
    citations = extract_citations("Code status: Full Code", bundle, PATIENT_ID)
    cs_citations = [c for c in citations if c.claim_class == "code_status"]
    assert len(cs_citations) >= 1


@pytest.mark.hard_failure
def test_isolation_produces_citation_with_isolation_class() -> None:
    bundle = _bundle_with(_isolation_flag("flag-001", "Contact Precautions"))
    citations = extract_citations("Contact isolation — MRSA", bundle, PATIENT_ID)
    iso_citations = [c for c in citations if c.claim_class == "isolation"]
    assert len(iso_citations) >= 1


# ── Conversational/UX text — no citations required ───────────────────────────

@pytest.mark.hard_failure
def test_greeting_is_conversational() -> None:
    assert is_conversational_text("Good morning, Dr. Chen.") is True


@pytest.mark.hard_failure
def test_not_found_acknowledgment_is_conversational() -> None:
    assert is_conversational_text(
        "I searched 24 months of encounters and found no echo results for this patient."
    ) is True


@pytest.mark.hard_failure
def test_disclaimer_footer_is_conversational() -> None:
    assert is_conversational_text(
        "This summary is generated from EHR data as of 06:30. Verify critical values directly in the chart."
    ) is True


# ── Citation structure tests ──────────────────────────────────────────────────

@pytest.mark.hard_failure
def test_citation_has_all_required_fields() -> None:
    c = Citation(
        patient_id="pt-001",
        resource_type="Observation",
        resource_id="obs-001",
        effective_datetime="2026-04-29T06:00:00Z",
        value_summary="K+ 5.9 mEq/L",
        claim_class="lab_value",
    )
    assert c.patient_id == "pt-001"
    assert c.resource_type == "Observation"
    assert c.resource_id == "obs-001"
    assert c.effective_datetime == "2026-04-29T06:00:00Z"
    assert c.value_summary == "K+ 5.9 mEq/L"
    assert c.claim_class == "lab_value"


@pytest.mark.hard_failure
def test_citation_is_frozen_immutable() -> None:
    c = Citation(
        patient_id="pt-001",
        resource_type="Observation",
        resource_id="obs-001",
        effective_datetime=None,
        value_summary="RR 26",
        claim_class="vital",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.patient_id = "pt-002"  # type: ignore[misc]


@pytest.mark.hard_failure
def test_citation_to_dict_returns_plain_dict_with_all_keys() -> None:
    c = Citation(
        patient_id="pt-001",
        resource_type="MedicationRequest",
        resource_id="med-001",
        effective_datetime="2026-04-28T08:00:00Z",
        value_summary="Lisinopril 10mg",
        claim_class="medication",
    )
    d = c.to_dict()
    assert isinstance(d, dict)
    required_keys = {"patient_id", "resource_type", "resource_id", "effective_datetime", "value_summary", "claim_class"}
    assert required_keys == set(d.keys())
    # All values must be JSON-serializable (str or None)
    for v in d.values():
        assert v is None or isinstance(v, str)


@pytest.mark.hard_failure
def test_extract_citations_returns_citation_list_type() -> None:
    bundle = _bundle_with(
        _obs_lab("obs-k", "Potassium", "5.9", "mEq/L"),
        _obs_vital("obs-rr", "Respiratory Rate", "26", "/min"),
        _med_request("med-001", "Lisinopril 10mg"),
    )
    citations = extract_citations("some output", bundle, PATIENT_ID)
    assert isinstance(citations, list)
    assert all(isinstance(c, Citation) for c in citations)


@pytest.mark.hard_failure
def test_empty_bundle_returns_empty_citation_list() -> None:
    citations = extract_citations("anything", {"resources": {}}, PATIENT_ID)
    assert citations == []


@pytest.mark.hard_failure
def test_all_claim_classes_are_clinical() -> None:
    assert CLINICAL_CLAIM_CLASSES == {
        "lab_value", "vital", "medication", "condition", "allergy", "code_status", "isolation"
    }


@pytest.mark.hard_failure
def test_citations_for_fhir_resource_helper() -> None:
    resource = _obs_lab("obs-na", "Sodium", "138", "mEq/L")
    citation = citations_for_fhir_resource(PATIENT_ID, resource, "lab_value", "Sodium 138 mEq/L")
    assert citation.resource_type == "Observation"
    assert citation.resource_id == "obs-na"
    assert citation.claim_class == "lab_value"
    assert citation.effective_datetime == "2026-04-29T06:00:00Z"
