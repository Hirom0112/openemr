"""Full context builder for UC-2 Pre-Encounter Briefing.

Assembles a structured BriefingContext from a FHIR patient bundle.
Every clinical claim carries a source citation (resource type, LOINC code,
effective date/time) so the verification layer can check attribution.

Output schema: BriefingContext (frozen dataclass) — used directly as the
structured output schema for Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Citation:
    resource_type: str
    code: str          # LOINC or SNOMED code
    display: str       # Human-readable code display
    effective_dt: str  # ISO-8601 timestamp or "" if unavailable
    value: str         # Formatted value string


@dataclass(frozen=True)
class ActiveMedication:
    name: str
    dose: str
    route: str
    status: str
    citation: Citation


@dataclass(frozen=True)
class ActiveCondition:
    display: str
    onset: str
    citation: Citation


@dataclass(frozen=True)
class AllergyEntry:
    substance: str
    reaction: str
    severity: str
    citation: Citation


@dataclass(frozen=True)
class VitalEntry:
    display: str
    value: str
    unit: str
    effective_dt: str
    is_critical: bool
    citation: Citation


@dataclass(frozen=True)
class LabEntry:
    display: str
    value: str
    unit: str
    interpretation: str   # "H", "L", "HH", "LL", "N", etc.
    effective_dt: str
    is_critical: bool
    citation: Citation


@dataclass(frozen=True)
class BriefingContext:
    patient_id: str
    name: str
    dob: str
    mrn: str
    code_status: str        # "" if blank — verification layer flags this
    active_conditions: list[ActiveCondition]
    allergies: list[AllergyEntry]
    active_medications: list[ActiveMedication]
    recent_vitals: list[VitalEntry]
    recent_labs: list[LabEntry]
    has_blank_allergy_section: bool
    has_blank_code_status: bool
    fetched_at: str          # ISO-8601 UTC timestamp


# ── Helpers ───────────────────────────────────────────────────────────────────

def _first_coding(resource: dict, path_keys: list[str]) -> dict:
    node = resource
    for key in path_keys:
        node = node.get(key, {})
    codings = node.get("coding", []) if isinstance(node, dict) else []
    return codings[0] if codings else {}


def _effective(obs: dict) -> str:
    return obs.get("effectiveDateTime") or obs.get("effectivePeriod", {}).get("start", "")


def _numeric_str(obs: dict) -> tuple[str, str]:
    qty = obs.get("valueQuantity", {})
    val = qty.get("value")
    unit = qty.get("unit", "")
    if val is not None:
        return f"{val:.2f}".rstrip("0").rstrip("."), unit
    text = obs.get("valueString", "")
    return text, unit


def _interpretation_code(obs: dict) -> str:
    interps = obs.get("interpretation", [])
    if interps:
        codings = interps[0].get("coding", [{}])
        return codings[0].get("code", "")
    return ""


def _is_critical_interp(code: str) -> bool:
    return code in ("HH", "LL", "AA")


# ── Builder ───────────────────────────────────────────────────────────────────

def build(patient: dict[str, Any], bundle: dict[str, Any]) -> BriefingContext:
    resources = bundle.get("resources", {})
    fetched_at = datetime.now(timezone.utc).isoformat()

    # ── Patient demographics ──────────────────────────────────────────────────
    names = patient.get("name", [])
    name_str = ""
    if names:
        n = names[0]
        given = " ".join(n.get("given", []))
        family = n.get("family", "")
        name_str = f"{given} {family}".strip()

    dob = patient.get("birthDate", "")
    mrn = next(
        (i.get("value", "") for i in patient.get("identifier", [])
         if i.get("type", {}).get("coding", [{}])[0].get("code") == "MR"),
        patient.get("id", ""),
    )

    # Code status — look for a Condition or Observation with LOINC 45473-6
    code_status = ""

    # ── Conditions ────────────────────────────────────────────────────────────
    conditions: list[ActiveCondition] = []
    for entry in resources.get("Condition", []):
        cond = entry.get("resource", entry)
        coding = _first_coding(cond, ["code"])
        display = coding.get("display") or cond.get("code", {}).get("text", "Unknown condition")
        onset = cond.get("onsetDateTime", cond.get("onsetPeriod", {}).get("start", ""))
        citation = Citation(
            resource_type="Condition",
            code=coding.get("code", ""),
            display=display,
            effective_dt=onset,
            value="active",
        )
        conditions.append(ActiveCondition(display=display, onset=onset, citation=citation))

    # ── Allergies ─────────────────────────────────────────────────────────────
    allergies: list[AllergyEntry] = []
    for entry in resources.get("AllergyIntolerance", []):
        allergy = entry.get("resource", entry)
        substance_coding = _first_coding(allergy, ["code"])
        substance = substance_coding.get("display") or allergy.get("code", {}).get("text", "Unknown")
        reactions = allergy.get("reaction", [])
        reaction_display = ""
        severity = ""
        if reactions:
            mani = reactions[0].get("manifestation", [{}])
            reaction_display = mani[0].get("coding", [{}])[0].get("display", "") if mani else ""
            severity = reactions[0].get("severity", "")
        citation = Citation(
            resource_type="AllergyIntolerance",
            code=substance_coding.get("code", ""),
            display=substance,
            effective_dt=allergy.get("recordedDate", ""),
            value=f"{reaction_display} ({severity})" if reaction_display else severity,
        )
        allergies.append(AllergyEntry(substance=substance, reaction=reaction_display, severity=severity, citation=citation))

    has_blank_allergy_section = len(allergies) == 0

    # ── Medications ───────────────────────────────────────────────────────────
    medications: list[ActiveMedication] = []
    for entry in resources.get("MedicationRequest", []):
        med = entry.get("resource", entry)
        med_coding = _first_coding(med, ["medicationCodeableConcept"])
        name = med_coding.get("display") or med.get("medicationCodeableConcept", {}).get("text", "Unknown")
        dosage = med.get("dosageInstruction", [{}])[0] if med.get("dosageInstruction") else {}
        dose_qty = dosage.get("doseAndRate", [{}])[0].get("doseQuantity", {}) if dosage.get("doseAndRate") else {}
        dose = f"{dose_qty.get('value', '')} {dose_qty.get('unit', '')}".strip()
        route_coding = _first_coding(dosage, ["route"])
        route = route_coding.get("display", "")
        status = med.get("status", "")
        citation = Citation(
            resource_type="MedicationRequest",
            code=med_coding.get("code", ""),
            display=name,
            effective_dt=med.get("authoredOn", ""),
            value=f"{dose} {route}".strip(),
        )
        medications.append(ActiveMedication(name=name, dose=dose, route=route, status=status, citation=citation))

    # ── Observations (vitals + labs) ─────────────────────────────────────────
    vitals: list[VitalEntry] = []
    labs: list[LabEntry] = []

    VITAL_SIGN_CATEGORY = "vital-signs"

    for entry in resources.get("Observation", []):
        obs = entry.get("resource", entry)
        categories = [
            c.get("coding", [{}])[0].get("code", "")
            for c in obs.get("category", [])
        ]
        loinc_coding = _first_coding(obs, ["code"])
        loinc_code = loinc_coding.get("code", "")
        display = loinc_coding.get("display") or obs.get("code", {}).get("text", loinc_code)
        val_str, unit = _numeric_str(obs)
        interp = _interpretation_code(obs)
        eff = _effective(obs)
        citation = Citation(
            resource_type="Observation",
            code=loinc_code,
            display=display,
            effective_dt=eff,
            value=f"{val_str} {unit}".strip(),
        )

        if VITAL_SIGN_CATEGORY in categories:
            vitals.append(VitalEntry(
                display=display,
                value=val_str,
                unit=unit,
                effective_dt=eff,
                is_critical=_is_critical_interp(interp),
                citation=citation,
            ))
        else:
            labs.append(LabEntry(
                display=display,
                value=val_str,
                unit=unit,
                interpretation=interp,
                effective_dt=eff,
                is_critical=_is_critical_interp(interp),
                citation=citation,
            ))

    vitals.sort(key=lambda v: v.effective_dt, reverse=True)
    labs.sort(key=lambda l: l.effective_dt, reverse=True)

    return BriefingContext(
        patient_id=patient.get("id", ""),
        name=name_str,
        dob=dob,
        mrn=mrn,
        code_status=code_status,
        active_conditions=conditions,
        allergies=allergies,
        active_medications=medications,
        recent_vitals=vitals[:20],
        recent_labs=labs[:30],
        has_blank_allergy_section=has_blank_allergy_section,
        has_blank_code_status=(code_status == ""),
        fetched_at=fetched_at,
    )
