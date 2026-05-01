#!/usr/bin/env python3
"""
Generates 24 synthetic FHIR R4 patient bundles for the Clinical Co-Pilot pilot.
Fixed seed: 42. Output: synthetic_data/bundles/pt-NNN.json

Scenarios covered:
  S1  pt-001  Marcus Webb        qSOFA >= 2 (score=3), bed 501 — URGENT
  S2  pt-002  Delia Fontaine     Critical K+ 6.4 unacknowledged at 03:12 — URGENT
  S3  pt-003  Raymond Okafor     Blank code status — WATCH
  S4  pt-004  Gloria Tran        Incomplete allergy section — WATCH
  S5  pt-005  Bernard Kowalski   Penicillin allergy + overnight Amoxicillin order — WATCH
  S6  pt-006  Ingrid Nakamura    Discharge plan + chest CT pending — WATCH
  S7  pt-007  Darnell Simmons    Nephrology consult unanswered > 6 h — WATCH
  S8  pt-008  Yvonne Castillo    Conflicting creatinine values (1.2 vs 2.1) — conflict
  S9  --      (satisfied by 18-patient census total)
  S10 pt-018  Thomas Greer       Out-of-census patient (prov-other, not prov-chen)
  pt-009..017 Stable patients with varied diagnoses
"""

import json
import pathlib
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

SEED = 42
random.seed(SEED)

OUTPUT_DIR = pathlib.Path(__file__).parent / "bundles"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_DATE = datetime(2026, 4, 29, 7, 0, 0, tzinfo=timezone.utc)
PROVIDER_CHEN = "prov-chen"
PROVIDER_OTHER = "prov-other"


# ---------------------------------------------------------------------------
# Deterministic UUID helpers
# ---------------------------------------------------------------------------

_uuid_counter = 0


def det_uuid(label: str) -> str:
    """Return a stable UUID based on label + seed."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"seed42.{label}"))


def ts(hours_offset: float = 0.0, base: Optional[datetime] = None) -> str:
    """Return ISO-8601 UTC timestamp offset from BASE_DATE."""
    b = base if base is not None else BASE_DATE
    return (b + timedelta(hours=hours_offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def admit_date(days_ago: int) -> str:
    return (BASE_DATE - timedelta(days=days_ago)).strftime("%Y-%m-%dT08:00:00Z")


# ---------------------------------------------------------------------------
# FHIR resource builders
# ---------------------------------------------------------------------------

def patient_resource(pid: str, family: str, given: str, dob: str, mrn: str, gender: str = "male") -> dict:
    return {
        "resourceType": "Patient",
        "id": pid,
        "identifier": [{"system": "urn:oid:2.16.840.1.113883.4.1", "value": mrn}],
        "name": [{"use": "official", "family": family, "given": [given]}],
        "gender": gender,
        "birthDate": dob,
    }


def encounter_resource(
    eid: str,
    pid: str,
    bed: str,
    admit_dt: str,
    dx_text: str,
    dx_code: str,
    provider_id: str = PROVIDER_CHEN,
    discharge_note: bool = False,
) -> dict:
    enc: dict = {
        "resourceType": "Encounter",
        "id": eid,
        "status": "in-progress",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP"},
        "subject": {"reference": f"Patient/{pid}"},
        "participant": [{"individual": {"reference": f"Practitioner/{provider_id}"}}],
        "period": {"start": admit_dt},
        "location": [{"location": {"display": f"Bed {bed}"}}],
        "reasonCode": [{"coding": [{"system": "http://snomed.info/sct", "code": dx_code, "display": dx_text}]}],
    }
    if discharge_note:
        enc["extension"] = [
            {
                "url": "http://example.org/fhir/StructureDefinition/discharge-plan-status",
                "valueString": "Discharge plan documented — pending results review",
            }
        ]
    return enc


def observation_vital(oid: str, pid: str, eid: str, loinc: str, display: str, value: float, unit: str, obs_ts: str, performer: str = "prov-chen") -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{eid}"},
        "effectiveDateTime": obs_ts,
        "performer": [{"reference": f"Practitioner/{performer}"}],
        "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org"},
    }


def observation_lab(oid: str, pid: str, eid: str, loinc: str, display: str, value: float, unit: str, obs_ts: str,
                    interpretation_code: Optional[str] = None, performer: str = "prov-lab") -> dict:
    obs: dict = {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{eid}"},
        "effectiveDateTime": obs_ts,
        "performer": [{"reference": f"Practitioner/{performer}"}],
        "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org"},
    }
    if interpretation_code:
        obs["interpretation"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": interpretation_code}]}]
    return obs


def condition_resource(cid: str, pid: str, eid: str, snomed: str, display: str) -> dict:
    return {
        "resourceType": "Condition",
        "id": cid,
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": snomed, "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{eid}"},
    }


def med_request(mid: str, pid: str, eid: str, rxnorm: str, display: str, authored_ts: str, requester: str = "prov-chen") -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": mid,
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": rxnorm, "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{eid}"},
        "authoredOn": authored_ts,
        "requester": {"reference": f"Practitioner/{requester}"},
    }


def allergy_resource(aid: str, pid: str, substance_code: str, substance_display: str,
                     reaction_display: Optional[str] = None, allergy_type: Optional[str] = None,
                     criticality: str = "high") -> dict:
    allergy: dict = {
        "resourceType": "AllergyIntolerance",
        "id": aid,
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
        "patient": {"reference": f"Patient/{pid}"},
        "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": substance_code, "display": substance_display}]},
        "criticality": criticality,
    }
    if allergy_type:
        allergy["type"] = allergy_type
    if reaction_display:
        allergy["reaction"] = [{"manifestation": [{"coding": [{"system": "http://snomed.info/sct", "display": reaction_display}]}]}]
    return allergy


def no_known_allergy(aid: str, pid: str) -> dict:
    return {
        "resourceType": "AllergyIntolerance",
        "id": aid,
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://hl7.org/fhir/uv/ips/CodeSystem/absent-unknown-uv-ips", "code": "no-known-allergy", "display": "No known allergy"}]},
        "patient": {"reference": f"Patient/{pid}"},
    }


def diagnostic_report(rid: str, pid: str, eid: str, loinc: str, display: str,
                       status: str, authored_ts: str, conclusion: Optional[str] = None) -> dict:
    dr: dict = {
        "resourceType": "DiagnosticReport",
        "id": rid,
        "status": status,
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{eid}"},
        "issued": authored_ts,
    }
    if conclusion:
        dr["conclusion"] = conclusion
    return dr


def bundle(resources: list[dict]) -> dict:
    entries = []
    for r in resources:
        rtype = r["resourceType"]
        rid = r.get("id", det_uuid(rtype))
        entries.append({
            "fullUrl": f"urn:uuid:{det_uuid(rtype + rid)}",
            "resource": r,
            "request": {"method": "PUT", "url": f"{rtype}/{rid}"},
        })
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def save(pid_str: str, b: dict) -> None:
    path = OUTPUT_DIR / f"{pid_str}.json"
    path.write_text(json.dumps(b, indent=2))
    print(f"  wrote {path.name}")


# ===========================================================================
# S1 — pt-001  Marcus Webb  qSOFA=3, bed 501  (URGENT Priority 1)
# ===========================================================================
def build_pt001() -> dict:
    pid = "pt-001"
    eid = "enc-001"
    vt = ts(-1.5)  # ~05:30 most recent vitals
    resources = [
        patient_resource(pid, "Webb", "Marcus", "1968-03-14", "MRN-10001", "male"),
        encounter_resource(eid, pid, "501", admit_date(1), "Sepsis due to pneumonia", "281999006"),
        # qSOFA: SBP<=100, RR>=22, altered mental status (GCS<15 modeled as low Glasgow component)
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 88.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 54.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 118.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 26.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 38.9, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 88.0, "%", vt),
        # Altered mental status: Glasgow Coma Scale total < 15
        {
            "resourceType": "Observation",
            "id": f"{pid}-gcs",
            "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "9269-2", "display": "Glasgow coma score total"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "effectiveDateTime": vt,
            "valueInteger": 12,
            "interpretation": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": "L"}]}],
        },
        observation_lab(f"{pid}-lactate", pid, eid, "2518-9", "Lactate [Moles/volume] in Blood", 4.2, "mmol/L", ts(-2.0), "HH"),
        observation_lab(f"{pid}-wbc", pid, eid, "6690-2", "Leukocytes [#/volume] in Blood", 18.4, "10*3/uL", ts(-2.0), "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "281999006", "Sepsis"),
        condition_resource(f"{pid}-cond2", pid, eid, "233604007", "Pneumonia"),
        med_request(f"{pid}-med1", pid, eid, "7454", "Vancomycin 1g IV", ts(-3.0)),
        med_request(f"{pid}-med2", pid, eid, "25789", "Piperacillin-tazobactam 3.375g IV", ts(-3.0)),
        med_request(f"{pid}-med3", pid, eid, "1049521", "Norepinephrine 0.1 mcg/kg/min IV", ts(-1.0)),
        no_known_allergy(f"{pid}-allergy", pid),
        # code status explicit
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(1),
            "valueCodeableConcept": {"text": "Full Code"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# S2 — pt-002  Delia Fontaine  Critical K+ 6.4 unacknowledged 03:12  (URGENT Priority 2)
# ===========================================================================
def build_pt002() -> dict:
    pid = "pt-002"
    eid = "enc-002"
    critical_k_ts = "2026-04-29T03:12:00Z"
    vt = ts(-2.0)
    resources = [
        patient_resource(pid, "Fontaine", "Delia", "1952-11-07", "MRN-10002", "female"),
        encounter_resource(eid, pid, "512", admit_date(2), "Acute kidney injury on CKD stage 4", "40095003"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 148.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 88.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 74.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 16.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.1, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 97.0, "%", vt),
        # CRITICAL unacknowledged K+ — no performer acknowledgment field
        observation_lab(f"{pid}-k", pid, eid, "6298-4", "Potassium [Moles/volume] in Blood", 6.4, "mmol/L", critical_k_ts, "HH"),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", 4.8, "mg/dL", critical_k_ts, "H"),
        observation_lab(f"{pid}-bun", pid, eid, "3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma", 62.0, "mg/dL", critical_k_ts, "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "40095003", "Acute kidney injury"),
        condition_resource(f"{pid}-cond2", pid, eid, "709044004", "Chronic kidney disease stage 4"),
        med_request(f"{pid}-med1", pid, eid, "202991", "Sodium bicarbonate 8.4% IV", ts(-4.0)),
        med_request(f"{pid}-med2", pid, eid, "1191", "Aspirin 81mg PO daily", admit_date(2)),
        no_known_allergy(f"{pid}-allergy", pid),
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(2),
            "valueCodeableConcept": {"text": "Full Code"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# S3 — pt-003  Raymond Okafor  Blank code status  (WATCH Priority 7)
# ===========================================================================
def build_pt003() -> dict:
    pid = "pt-003"
    eid = "enc-003"
    vt = ts(-1.0)
    resources = [
        patient_resource(pid, "Okafor", "Raymond", "1945-06-22", "MRN-10003", "male"),
        encounter_resource(eid, pid, "507", admit_date(1), "COPD exacerbation", "195951007"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 132.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 78.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 88.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 20.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.4, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 91.0, "%", vt),
        observation_lab(f"{pid}-wbc", pid, eid, "6690-2", "Leukocytes [#/volume] in Blood", 11.2, "10*3/uL", ts(-3.0), "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "195951007", "COPD exacerbation"),
        med_request(f"{pid}-med1", pid, eid, "41493", "Ipratropium bromide inhaler", admit_date(1)),
        med_request(f"{pid}-med2", pid, eid, "1234995", "Methylprednisolone 125mg IV", ts(-6.0)),
        no_known_allergy(f"{pid}-allergy", pid),
        # S3: code_status field intentionally absent — no code status Observation
    ]
    return bundle(resources)


# ===========================================================================
# S4 — pt-004  Gloria Tran  Incomplete allergy section  (WATCH)
# ===========================================================================
def build_pt004() -> dict:
    pid = "pt-004"
    eid = "enc-004"
    vt = ts(-1.5)
    resources = [
        patient_resource(pid, "Tran", "Gloria", "1958-09-03", "MRN-10004", "female"),
        encounter_resource(eid, pid, "514", admit_date(2), "Acute decompensated heart failure", "84114007",
                           provider_id=PROVIDER_OTHER),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 156.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 94.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 96.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 18.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.0, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 94.0, "%", vt),
        observation_lab(f"{pid}-bnp", pid, eid, "42637-9", "Natriuretic peptide B [Units/volume] in Serum or Plasma", 1840.0, "pg/mL", ts(-4.0), "H"),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", 1.4, "mg/dL", ts(-4.0), "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "84114007", "Heart failure"),
        med_request(f"{pid}-med1", pid, eid, "4603", "Furosemide 80mg IV", ts(-2.0)),
        med_request(f"{pid}-med2", pid, eid, "29046", "Lisinopril 10mg PO", admit_date(2)),
        # S4: AllergyIntolerance present but reaction and type fields EMPTY — incomplete section
        {
            "resourceType": "AllergyIntolerance",
            "id": f"{pid}-allergy",
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
            "patient": {"reference": f"Patient/{pid}"},
            "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "7980", "display": "Sulfonamide"}]},
            # type and reaction deliberately omitted
        },
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(2),
            "valueCodeableConcept": {"text": "DNR/DNI"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# S5 — pt-005  Bernard Kowalski  Penicillin allergy + overnight Amoxicillin  (WATCH Priority 5)
# ===========================================================================
def build_pt005() -> dict:
    pid = "pt-005"
    eid = "enc-005"
    vt = ts(-1.0)
    resources = [
        patient_resource(pid, "Kowalski", "Bernard", "1973-02-18", "MRN-10005", "male"),
        encounter_resource(eid, pid, "503", admit_date(1), "Community-acquired pneumonia", "233604007",
                           provider_id=PROVIDER_OTHER),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 122.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 76.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 90.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 18.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 38.2, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 95.0, "%", vt),
        observation_lab(f"{pid}-wbc", pid, eid, "6690-2", "Leukocytes [#/volume] in Blood", 14.6, "10*3/uL", ts(-3.0), "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "233604007", "Pneumonia"),
        # Documented Penicillin allergy (anaphylaxis)
        allergy_resource(f"{pid}-allergy1", pid, "7980", "Penicillin", "Anaphylaxis", "allergy", "high"),
        # S5: overnight Amoxicillin order — CONFLICTS with documented Penicillin allergy
        med_request(f"{pid}-med1", pid, eid, "723", "Amoxicillin 875mg PO q12h", ts(-5.0), "prov-overnight"),
        med_request(f"{pid}-med2", pid, eid, "1649574", "Azithromycin 500mg PO daily", admit_date(1)),
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(1),
            "valueCodeableConcept": {"text": "Full Code"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# S6 — pt-006  Ingrid Nakamura  Discharge plan + chest CT pending  (WATCH Priority 6)
# ===========================================================================
def build_pt006() -> dict:
    pid = "pt-006"
    eid = "enc-006"
    vt = ts(-1.5)
    resources = [
        patient_resource(pid, "Nakamura", "Ingrid", "1965-07-29", "MRN-10006", "female"),
        encounter_resource(eid, pid, "518", admit_date(3), "Suspected pulmonary embolism", "59282003",
                           provider_id=PROVIDER_OTHER, discharge_note=True),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 118.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 72.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 102.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 19.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.2, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 93.0, "%", vt),
        observation_lab(f"{pid}-ddimer", pid, eid, "48066-5", "Fibrin D-dimer DDU [Mass/volume] in Platelet poor plasma", 3.2, "mg/L FEU", ts(-5.0), "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "59282003", "Pulmonary embolism"),
        med_request(f"{pid}-med1", pid, eid, "67108", "Heparin 5000 units IV bolus", ts(-6.0)),
        med_request(f"{pid}-med2", pid, eid, "1037045", "Enoxaparin 1mg/kg SC q12h", ts(-5.0)),
        no_known_allergy(f"{pid}-allergy", pid),
        # S6: chest CT ordered but status = "registered" (not resulted) — pending
        diagnostic_report(f"{pid}-ct", pid, eid, "24627-2", "CT Chest", "registered", ts(-8.0)),
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(3),
            "valueCodeableConcept": {"text": "Full Code"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# S7 — pt-007  Darnell Simmons  Nephrology consult unanswered > 6 h  (WATCH Priority 8)
# ===========================================================================
def build_pt007() -> dict:
    pid = "pt-007"
    eid = "enc-007"
    vt = ts(-1.0)
    resources = [
        patient_resource(pid, "Simmons", "Darnell", "1980-04-11", "MRN-10007", "male"),
        encounter_resource(eid, pid, "522", admit_date(2), "Hypertensive urgency with AKI", "38341003"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 192.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 114.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 82.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 15.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.0, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 98.0, "%", vt),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", 2.9, "mg/dL", ts(-3.0), "H"),
        observation_lab(f"{pid}-k", pid, eid, "6298-4", "Potassium [Moles/volume] in Blood", 5.2, "mmol/L", ts(-3.0), "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "38341003", "Hypertensive disorder"),
        condition_resource(f"{pid}-cond2", pid, eid, "40095003", "Acute kidney injury"),
        med_request(f"{pid}-med1", pid, eid, "214354", "Labetalol 200mg PO q8h", admit_date(2)),
        med_request(f"{pid}-med2", pid, eid, "29046", "Lisinopril 5mg PO daily", admit_date(2)),
        no_known_allergy(f"{pid}-allergy", pid),
        # S7: Nephrology consult requested > 6 hours ago, no response — status "registered"
        diagnostic_report(f"{pid}-consult", pid, eid, "11488-4", "Nephrology Consult Note", "registered",
                           ts(-9.0), conclusion="Consult requested: AKI evaluation — AWAITING RESPONSE"),
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(2),
            "valueCodeableConcept": {"text": "Full Code"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# S8 — pt-008  Yvonne Castillo  Conflicting creatinine values  (conflict surfacing)
# ===========================================================================
def build_pt008() -> dict:
    pid = "pt-008"
    eid = "enc-008"
    vt = ts(-1.0)
    same_ts = "2026-04-29T04:00:00Z"
    resources = [
        patient_resource(pid, "Castillo", "Yvonne", "1960-12-30", "MRN-10008", "female"),
        encounter_resource(eid, pid, "509", admit_date(2), "Diabetic nephropathy — monitoring", "127013003"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 138.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 82.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 76.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 14.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.0, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 98.0, "%", vt),
        # S8: Two creatinine values same date/time — different performers — different values
        observation_lab(f"{pid}-creat-nurse", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
                        1.2, "mg/dL", same_ts, performer="prov-nurse"),
        observation_lab(f"{pid}-creat-lab", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
                        2.1, "mg/dL", same_ts, "H", performer="prov-lab"),
        observation_lab(f"{pid}-glucose", pid, eid, "2345-7", "Glucose [Mass/volume] in Serum or Plasma", 182.0, "mg/dL", ts(-3.0), "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "127013003", "Diabetic nephropathy"),
        med_request(f"{pid}-med1", pid, eid, "860975", "Metformin 500mg PO BID", admit_date(2)),
        med_request(f"{pid}-med2", pid, eid, "29046", "Lisinopril 10mg PO daily", admit_date(2)),
        no_known_allergy(f"{pid}-allergy", pid),
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(2),
            "valueCodeableConcept": {"text": "DNR/DNI"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# Stable patients pt-009 through pt-017
# ===========================================================================

STABLE_PATIENTS = [
    # (num, family, given, dob, mrn, gender, bed, days_ago, dx_text, dx_snomed, meds, allergy_fn, labs)
    (
        "009", "Morales", "Elena", "1955-08-17", "MRN-10009", "female", "504", 1,
        "Urinary tract infection", "68566005",
        [("372687004", "Trimethoprim-sulfamethoxazole 160/800mg PO BID"), ("1191", "Aspirin 81mg PO daily")],
        "nka", [("6690-2", "Leukocytes [#/volume] in Blood", 13.2, "10*3/uL", "H")],
        PROVIDER_OTHER,
    ),
    (
        "010", "Patel", "Rajiv", "1948-03-24", "MRN-10010", "male", "506", 2,
        "Cellulitis left lower extremity", "128045006",
        [("7980", "Cephalexin 500mg PO QID"), ("29046", "Lisinopril 5mg PO daily")],
        "nka", [("6690-2", "Leukocytes [#/volume] in Blood", 14.8, "10*3/uL", "H")],
        PROVIDER_OTHER,
    ),
    (
        "011", "Bergstrom", "Karl", "1971-11-02", "MRN-10011", "male", "508", 2,
        "Upper GI bleed — peptic ulcer disease", "40845000",
        [("7646", "Pantoprazole 40mg IV q12h"), ("1049502", "Ondansetron 4mg IV q8h PRN")],
        "nka", [("2345-7", "Glucose [Mass/volume] in Serum or Plasma", 98.0, "mg/dL", None),
                ("718-7", "Hemoglobin [Mass/volume] in Blood", 8.4, "g/dL", "L")],
        PROVIDER_OTHER,
    ),
    (
        "012", "Johnson", "Miriam", "1939-05-15", "MRN-10012", "female", "510", 3,
        "Ischemic stroke — left MCA territory", "422504002",
        [("1191", "Aspirin 325mg PO daily"), ("41493", "Atorvastatin 40mg PO daily"), ("29046", "Lisinopril 10mg PO daily")],
        "nka", [("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", 1.0, "mg/dL", None)],
    ),
    (
        "013", "Reyes", "Carlos", "1977-09-09", "MRN-10013", "male", "511", 1,
        "Diabetic ketoacidosis", "420422005",
        [("51428", "Insulin glargine 20 units SC daily"), ("860975", "Metformin 500mg PO BID"),
         ("202991", "Sodium chloride 0.9% IV 125 mL/hr")],
        "nka", [("2345-7", "Glucose [Mass/volume] in Serum or Plasma", 420.0, "mg/dL", "HH"),
                ("6298-4", "Potassium [Moles/volume] in Blood", 3.3, "mmol/L", "L")],
    ),
    (
        "014", "Osei", "Abena", "1962-01-28", "MRN-10014", "female", "513", 2,
        "Acute pancreatitis — alcohol related", "197456007",
        [("7646", "Pantoprazole 40mg IV daily"), ("1049502", "Ondansetron 4mg IV q6h PRN"),
         ("1049521", "Morphine 2mg IV q4h PRN")],
        "nka", [("1742-7", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma", 88.0, "U/L", "H")],
        PROVIDER_OTHER,
    ),
    (
        "015", "Williams", "Dorothy", "1944-07-04", "MRN-10015", "female", "515", 3,
        "Stable systolic heart failure — diuresis", "84114007",
        [("4603", "Furosemide 40mg PO daily"), ("29046", "Lisinopril 5mg PO daily"),
         ("41493", "Carvedilol 6.25mg PO BID")],
        "nka", [("42637-9", "Natriuretic peptide B [Units/volume] in Serum or Plasma", 560.0, "pg/mL", "H")],
        PROVIDER_OTHER,
    ),
    (
        "016", "Huang", "Wei", "1983-10-20", "MRN-10016", "male", "516", 1,
        "Post-operative day 1 — laparoscopic cholecystectomy", "174431009",
        [("1049521", "Ketorolac 15mg IV q6h"), ("1049502", "Ondansetron 4mg IV q8h PRN")],
        "nka", [("6690-2", "Leukocytes [#/volume] in Blood", 10.4, "10*3/uL", None)],
    ),
    (
        "017", "Murphy", "Sean", "1957-03-13", "MRN-10017", "male", "517", 2,
        "Alcohol withdrawal — CIWA protocol", "191855006",
        [("5691", "Lorazepam 2mg IV q1h PRN CIWA>8"), ("202991", "Thiamine 100mg IV daily"),
         ("4891", "Folate 1mg PO daily")],
        "nka", [("2345-7", "Glucose [Mass/volume] in Serum or Plasma", 112.0, "mg/dL", None)],
        PROVIDER_OTHER,
    ),
]


def build_stable(num: str, family: str, given: str, dob: str, mrn: str, gender: str,
                 bed: str, days_ago: int, dx_text: str, dx_snomed: str,
                 meds: list, allergy_fn: str, labs: list,
                 provider_id: str = PROVIDER_CHEN) -> dict:
    pid = f"pt-{num}"
    eid = f"enc-{num}"
    vt = ts(-1.0)
    resources = [
        patient_resource(pid, family, given, dob, mrn, gender),
        encounter_resource(eid, pid, bed, admit_date(days_ago), dx_text, dx_snomed,
                           provider_id=provider_id),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure",
                          random.uniform(108, 145), "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure",
                          random.uniform(62, 88), "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate",
                          random.uniform(60, 98), "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate",
                          random.uniform(12, 18), "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature",
                          random.uniform(36.5, 37.8), "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation",
                          random.uniform(95, 99), "%", vt),
        condition_resource(f"{pid}-cond1", pid, eid, dx_snomed, dx_text),
    ]

    for i, (rxnorm, display) in enumerate(meds):
        resources.append(med_request(f"{pid}-med{i+1}", pid, eid, rxnorm, display, admit_date(days_ago)))

    if allergy_fn == "nka":
        resources.append(no_known_allergy(f"{pid}-allergy", pid))

    for j, lab in enumerate(labs):
        loinc, display, value, unit, *rest = lab
        interp = rest[0] if rest else None
        resources.append(observation_lab(f"{pid}-lab{j+1}", pid, eid, loinc, display, value, unit, ts(-3.0), interp))

    resources.append({
        "resourceType": "Observation",
        "id": f"{pid}-codestatus",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": admit_date(days_ago),
        "valueCodeableConcept": {"text": random.choice(["Full Code", "Full Code", "DNR/DNI"])},
    })

    return bundle(resources)


# ===========================================================================
# S10 — pt-018  Thomas Greer  Out-of-census patient  (prov-other)
# ===========================================================================
def build_pt018() -> dict:
    pid = "pt-018"
    eid = "enc-018"
    vt = ts(-1.5)
    resources = [
        patient_resource(pid, "Greer", "Thomas", "1950-06-01", "MRN-10018", "male"),
        # S10: provider is prov-other — NOT prov-chen — no CareTeam link to Dr. Chen
        encounter_resource(eid, pid, "520", admit_date(1), "Atrial fibrillation with RVR", "195080001",
                           provider_id=PROVIDER_OTHER),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 128.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 80.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 136.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 16.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.1, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 96.0, "%", vt),
        observation_lab(f"{pid}-tsh", pid, eid, "3016-3", "Thyrotropin [Units/volume] in Serum or Plasma", 0.8, "mIU/L", ts(-3.0)),
        condition_resource(f"{pid}-cond1", pid, eid, "195080001", "Atrial fibrillation"),
        med_request(f"{pid}-med1", pid, eid, "3407", "Metoprolol succinate 25mg PO daily", admit_date(1), PROVIDER_OTHER),
        med_request(f"{pid}-med2", pid, eid, "1037045", "Apixaban 5mg PO BID", admit_date(1), PROVIDER_OTHER),
        no_known_allergy(f"{pid}-allergy", pid),
        {
            "resourceType": "Observation",
            "id": f"{pid}-codestatus",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": admit_date(1),
            "valueCodeableConcept": {"text": "Full Code"},
        },
    ]
    return bundle(resources)


# ===========================================================================
# Helpers for pt-019..024 (Flag + code-status observation)
# ===========================================================================

def code_status_obs(pid: str, eff_ts: str, value: str = "Full Code") -> dict:
    return {
        "resourceType": "Observation",
        "id": f"{pid}-codestatus",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "81638-3", "display": "Code status"}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": eff_ts,
        "valueCodeableConcept": {"text": value},
    }


def flag_no_isolation(pid: str) -> dict:
    return {
        "resourceType": "Flag",
        "id": f"{pid}-isolation",
        "status": "inactive",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/flag-category",
            "code": "infection",
            "display": "Infection Control Flag",
        }]}],
        "code": {"text": "No Isolation Required"},
        "subject": {"reference": f"Patient/{pid}"},
    }


def flag_airborne(pid: str) -> dict:
    return {
        "resourceType": "Flag",
        "id": f"{pid}-isolation",
        "status": "active",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/flag-category",
            "code": "infection",
            "display": "Infection Control Flag",
        }]}],
        "code": {
            "text": "Airborne Precautions",
            "coding": [{"system": "http://snomed.info/sct", "code": "409526008", "display": "Airborne precautions"}],
        },
        "subject": {"reference": f"Patient/{pid}"},
    }


# ===========================================================================
# pt-019  Linda Okonkwo  Blank code status (no codestatus obs)  → P9
# ===========================================================================
def build_pt019() -> dict:
    pid = "pt-019"
    eid = "enc-019"
    vt = ts(-1.0)  # 06:00
    lab_t = ts(-3.0)  # 04:00
    resources = [
        patient_resource(pid, "Okonkwo", "Linda", "1966-08-30", "MRN-10019", "female"),
        encounter_resource(eid, pid, "523", admit_date(1),
                           "Observation after minor fall — no fracture identified", "312681000"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 124.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 76.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 72.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 15.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.0, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 97.0, "%", vt),
        observation_lab(f"{pid}-glucose", pid, eid, "2345-7", "Glucose [Mass/volume] in Serum or Plasma",
                        98.0, "mg/dL", lab_t),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
                        0.9, "mg/dL", lab_t),
        no_known_allergy(f"{pid}-allergy", pid),
        flag_no_isolation(pid),
        # Code status Observation intentionally absent — this drives blank_code_status → P9.
    ]
    return bundle(resources)


# ===========================================================================
# pt-020  Robert Finch  Pre-procedure observation  → P10 routine
# ===========================================================================
def build_pt020() -> dict:
    pid = "pt-020"
    eid = "enc-020"
    vt = ts(-1.0)  # 06:00
    lab_t = ts(-2.0)  # 05:00
    resources = [
        patient_resource(pid, "Finch", "Robert", "1959-03-17", "MRN-10020", "male"),
        encounter_resource(eid, pid, "524", admit_date(0),
                           "Pre-procedure observation — elective colonoscopy prep", "73761001"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 118.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 74.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 68.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 14.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 36.8, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 98.0, "%", vt),
        observation_lab(f"{pid}-glucose", pid, eid, "2345-7", "Glucose [Mass/volume] in Serum or Plasma",
                        94.0, "mg/dL", lab_t),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
                        0.8, "mg/dL", lab_t),
        no_known_allergy(f"{pid}-allergy", pid),
        code_status_obs(pid, admit_date(0)),
        flag_no_isolation(pid),
    ]
    return bundle(resources)


# ===========================================================================
# pt-021  Priya Anand  Suspected sepsis qSOFA=2 (SBP=96, RR=24)  → P2
# ===========================================================================
def build_pt021() -> dict:
    pid = "pt-021"
    eid = "enc-021"
    vt = ts(-0.5)  # 06:30
    lab_t = ts(-1.0)  # 06:00
    resources = [
        patient_resource(pid, "Anand", "Priya", "1975-04-12", "MRN-10021", "female"),
        encounter_resource(eid, pid, "525", admit_date(0),
                           "Suspected sepsis — source under investigation", "10001005"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 96.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 62.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 108.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 24.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 38.6, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 94.0, "%", vt),
        observation_lab(f"{pid}-wbc", pid, eid, "6690-2", "Leukocytes [#/volume] in Blood",
                        14.2, "10*3/uL", lab_t, "H"),
        observation_lab(f"{pid}-lactate", pid, eid, "2518-9", "Lactate [Moles/volume] in Blood",
                        1.8, "mmol/L", lab_t),
        condition_resource(f"{pid}-cond1", pid, eid, "10001005", "Septicemia"),
        med_request(f"{pid}-med1", pid, eid, "7454", "Vancomycin 1g IV", lab_t),
        med_request(f"{pid}-med2", pid, eid, "25789", "Piperacillin-tazobactam 3.375g IV", lab_t),
        no_known_allergy(f"{pid}-allergy", pid),
        code_status_obs(pid, admit_date(0)),
        flag_airborne(pid),
    ]
    return bundle(resources)


# ===========================================================================
# pt-022  James Whitfield  Acute delirium GCS=14  → P5 (mental_status_alert)
# ===========================================================================
def build_pt022() -> dict:
    pid = "pt-022"
    eid = "enc-022"
    vt = ts(-1.0)  # 06:00
    lab_t = ts(-3.0)  # 04:00
    resources = [
        patient_resource(pid, "Whitfield", "James", "1942-10-05", "MRN-10022", "male"),
        encounter_resource(eid, pid, "526", admit_date(1),
                           "Acute delirium — hyperactive type", "409966000"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 128.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 74.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 88.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 17.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.3, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 96.0, "%", vt),
        # GCS=14 → triggers mental_status_alert and qSOFA +1.
        {
            "resourceType": "Observation",
            "id": f"{pid}-gcs",
            "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                       "code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "9269-2",
                                  "display": "Glasgow coma score total"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "effectiveDateTime": vt,
            "valueInteger": 14,
            "interpretation": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                "code": "L",
            }]}],
        },
        observation_lab(f"{pid}-bmp", pid, eid, "2823-3", "Potassium [Moles/volume] in Blood",
                        4.0, "mmol/L", lab_t),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
                        1.1, "mg/dL", lab_t),
        condition_resource(f"{pid}-cond1", pid, eid, "409966000", "Delirium"),
        med_request(f"{pid}-med1", pid, eid, "1049502", "Haloperidol 0.5mg IV PRN", ts(-2.0)),
        no_known_allergy(f"{pid}-allergy", pid),
        code_status_obs(pid, admit_date(1)),
        flag_no_isolation(pid),
    ]
    return bundle(resources)


# ===========================================================================
# pt-023  Keisha Balogun  Sickle cell crisis pain=9  → P6 (severe pain)
# ===========================================================================
def build_pt023() -> dict:
    pid = "pt-023"
    eid = "enc-023"
    vt = ts(-1.0)  # 06:00
    lab_t = ts(-3.0)  # 04:00
    resources = [
        patient_resource(pid, "Balogun", "Keisha", "1988-07-19", "MRN-10023", "female"),
        encounter_resource(eid, pid, "527", admit_date(1),
                           "Sickle cell disease with acute vaso-occlusive crisis", "127040003"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 118.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 72.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 98.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 18.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 37.5, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 96.0, "%", vt),
        # Pain 9/10 → pain_score_high → P6
        {
            "resourceType": "Observation",
            "id": f"{pid}-pain",
            "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                       "code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "72514-3",
                                  "display": "Pain severity - 0-10 verbal numeric rating [Score] - Reported"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "effectiveDateTime": vt,
            "valueQuantity": {"value": 9.0, "unit": "{score}", "system": "http://unitsofmeasure.org"},
        },
        observation_lab(f"{pid}-hgb", pid, eid, "718-7", "Hemoglobin [Mass/volume] in Blood",
                        8.2, "g/dL", lab_t),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
                        0.8, "mg/dL", lab_t),
        condition_resource(f"{pid}-cond1", pid, eid, "127040003", "Sickle cell crisis"),
        med_request(f"{pid}-med1", pid, eid, "1049521", "Morphine 4mg IV q3h PRN pain", lab_t),
        med_request(f"{pid}-med2", pid, eid, "1049502", "Ketorolac 15mg IV q6h", lab_t),
        no_known_allergy(f"{pid}-allergy", pid),
        code_status_obs(pid, admit_date(1)),
        flag_no_isolation(pid),
    ]
    return bundle(resources)


# ===========================================================================
# pt-024  Alejandro Cruz  Intra-abdominal sepsis qSOFA=3 + critical lactate → P1
# ===========================================================================
def build_pt024() -> dict:
    pid = "pt-024"
    eid = "enc-024"
    vt = ts(-1.5)
    lab_t = ts(-2.0)
    resources = [
        patient_resource(pid, "Cruz", "Alejandro", "1971-05-20", "MRN-10024", "male"),
        encounter_resource(eid, pid, "528", admit_date(1),
                           "Intra-abdominal sepsis", "444814009"),
        observation_vital(f"{pid}-sbp", pid, eid, "8480-6", "Systolic blood pressure", 94.0, "mmHg", vt),
        observation_vital(f"{pid}-dbp", pid, eid, "8462-4", "Diastolic blood pressure", 58.0, "mmHg", vt),
        observation_vital(f"{pid}-hr",  pid, eid, "8867-4", "Heart rate", 114.0, "/min", vt),
        observation_vital(f"{pid}-rr",  pid, eid, "9279-1", "Respiratory rate", 26.0, "/min", vt),
        observation_vital(f"{pid}-temp",pid, eid, "8310-5", "Body temperature", 38.7, "Cel", vt),
        observation_vital(f"{pid}-spo2",pid, eid, "59408-5", "Oxygen saturation", 93.0, "%", vt),
        # GCS=13 → mental_status_alert and qSOFA +1.
        {
            "resourceType": "Observation",
            "id": f"{pid}-gcs",
            "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                       "code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "9269-2",
                                  "display": "Glasgow coma score total"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "effectiveDateTime": vt,
            "valueInteger": 13,
            "interpretation": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                "code": "L",
            }]}],
        },
        observation_lab(f"{pid}-lactate", pid, eid, "2518-9", "Lactate [Moles/volume] in Blood",
                        5.1, "mmol/L", lab_t, "HH"),
        observation_lab(f"{pid}-wbc", pid, eid, "6690-2", "Leukocytes [#/volume] in Blood",
                        24.6, "10*3/uL", lab_t, "H"),
        observation_lab(f"{pid}-creat", pid, eid, "2160-0", "Creatinine [Mass/volume] in Serum or Plasma",
                        2.1, "mg/dL", lab_t, "H"),
        condition_resource(f"{pid}-cond1", pid, eid, "444814009", "Intra-abdominal sepsis"),
        condition_resource(f"{pid}-cond2", pid, eid, "74474003", "Secondary peritonitis"),
        med_request(f"{pid}-med1", pid, eid, "7454", "Meropenem 1g IV q8h", ts(-3.0)),
        med_request(f"{pid}-med2", pid, eid, "41493", "Metronidazole 500mg IV q8h", ts(-3.0)),
        med_request(f"{pid}-med3", pid, eid, "1049521", "Norepinephrine 0.05 mcg/kg/min", ts(-1.0)),
        no_known_allergy(f"{pid}-allergy", pid),
        code_status_obs(pid, admit_date(1)),
    ]
    return bundle(resources)


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    print(f"Generating 24 FHIR R4 bundles (seed={SEED}) → {OUTPUT_DIR}")

    save("pt-001", build_pt001())  # S1 qSOFA >= 2
    save("pt-002", build_pt002())  # S2 critical unacknowledged K+
    save("pt-003", build_pt003())  # S3 blank code status
    save("pt-004", build_pt004())  # S4 incomplete allergy section
    save("pt-005", build_pt005())  # S5 allergy-med conflict
    save("pt-006", build_pt006())  # S6 discharge plan + pending CT
    save("pt-007", build_pt007())  # S7 consult unanswered > 6h
    save("pt-008", build_pt008())  # S8 conflicting creatinine

    for row in STABLE_PATIENTS:
        save(f"pt-{row[0]}", build_stable(*row))

    save("pt-018", build_pt018())  # S10 out-of-census patient
    save("pt-019", build_pt019())  # P9 blank code status
    save("pt-020", build_pt020())  # P10 routine
    save("pt-021", build_pt021())  # P2 sepsis concern
    save("pt-022", build_pt022())  # P5 altered mental status
    save("pt-023", build_pt023())  # P6 severe pain
    save("pt-024", build_pt024())  # P1 sepsis with critical lactate

    print("Done. 24 patients (14 prov-chen + 10 prov-other).")


if __name__ == "__main__":
    main()
