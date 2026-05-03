"""Full source-attribution verification for UC-2 Pre-Encounter Briefing.

Every ClinicalClaim in a BriefingResponse must be traceable to a record
in the BriefingContext.  Claims that cannot be attributed are removed
and logged as attribution failures.

Attribution match rules:
  - source_resource must match a resource type in BriefingContext
  - source_code must match an entry's LOINC/SNOMED code in that resource list
  - source_value must be a substring of the entry's value (case-insensitive)

Strict mode (default): remove unattributed claims.
Lenient mode: attach a warning but keep the claim.

Also provides extract_citations() — the general-purpose citation extractor
that maps LLM output and FHIR bundle data to structured Citation objects
(agent.citation.Citation) for all tool return dicts.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from agent.citation import (
    Citation as AgentCitation,
    CitationList,
    ClaimClass,
    citations_for_fhir_resource,
    is_conversational_text,
)
from briefing.context_builder import BriefingContext
from briefing.context_builder import Citation as BriefingCitation
from briefing.schema import BriefingResponse, BriefingSection, ClinicalClaim

logger = logging.getLogger(__name__)

# Maps FHIR resource types to their canonical claim class
_RESOURCE_TYPE_TO_CLAIM_CLASS: dict[str, ClaimClass] = {
    "Observation": "lab_value",     # further narrowed below by category
    "MedicationRequest": "medication",
    "MedicationStatement": "medication",
    "AllergyIntolerance": "allergy",
    "Condition": "condition",
    "Flag": "isolation",
}

_VITAL_LOINC_PREFIXES = {"8310", "9279", "8867", "59408", "8480", "8462", "29463", "39156"}


def _claim_class_for_observation(resource: dict[str, Any]) -> ClaimClass:
    """Narrow an Observation to vital vs lab_value by category."""
    categories = resource.get("category", [])
    for cat in categories:
        for coding in cat.get("coding", []):
            if coding.get("code") == "vital-signs":
                return "vital"
    code_codings = resource.get("code", {}).get("coding", [])
    for coding in code_codings:
        code_str = coding.get("code", "")
        if any(code_str.startswith(p) for p in _VITAL_LOINC_PREFIXES):
            return "vital"
        # LOINC 81638-3 = code status
        if code_str == "81638-3":
            return "code_status"
    return "lab_value"


def extract_citations(
    llm_output: str | dict[str, Any],
    fhir_bundle: dict[str, Any],
    patient_id: str,
) -> CitationList:
    """Map FHIR resources in the bundle to AgentCitation objects.

    This function does not parse LLM output text — it builds citations from the
    FHIR resources present in the bundle, which are the authoritative sources.
    LLM output is accepted as a parameter for future content-matching extensions
    but is not used in this implementation.

    Produces one Citation per FHIR resource that carries clinical data.
    """
    resources: dict[str, list[Any]] = fhir_bundle.get("resources", {})
    result: CitationList = []

    for resource_type, entries in resources.items():
        for entry in entries:
            resource = entry.get("resource", entry) if isinstance(entry, dict) else entry
            if not isinstance(resource, dict):
                continue

            if resource_type == "Observation":
                claim_class: ClaimClass = _claim_class_for_observation(resource)
            elif resource_type == "Flag":
                # isolation precaution flags
                claim_class = "isolation"
                code_text = resource.get("code", {}).get("text", "")
                if not code_text:
                    continue
                result.append(
                    AgentCitation(
                        patient_id=patient_id,
                        resource_type=resource_type,
                        resource_id=resource.get("id", ""),
                        effective_datetime=resource.get("period", {}).get("start"),
                        value_summary=code_text,
                        claim_class=claim_class,
                    )
                )
                continue
            else:
                claim_class = _RESOURCE_TYPE_TO_CLAIM_CLASS.get(resource_type)  # type: ignore[assignment]
                if claim_class is None:
                    continue

            value_summary = _summarise_resource(resource, resource_type)
            result.append(
                citations_for_fhir_resource(patient_id, resource, claim_class, value_summary)
            )

    return result


def _summarise_resource(resource: dict[str, Any], resource_type: str) -> str:
    """Build a short human-readable value summary for a FHIR resource."""
    if resource_type == "Observation":
        display = resource.get("code", {}).get("text") or (
            resource.get("code", {}).get("coding", [{}])[0].get("display", "")
        )
        value: str = ""
        for key in ("valueQuantity", "valueDecimal", "valueInteger", "valueString", "valueRatio"):
            v = resource.get(key)
            if v is None:
                continue
            if isinstance(v, dict):
                value = f"{v.get('value', '')} {v.get('unit', '')}".strip()
            else:
                value = str(v)
            break
        effective = resource.get("effectiveDateTime", "")
        parts = [p for p in [display, value, effective] if p]
        return " — ".join(parts) if parts else resource.get("id", "")

    if resource_type in ("MedicationRequest", "MedicationStatement"):
        return (
            resource.get("medicationCodeableConcept", {}).get("text")
            or resource.get("medicationReference", {}).get("display")
            or resource.get("id", "")
        )

    if resource_type == "AllergyIntolerance":
        substance = (
            resource.get("code", {}).get("text")
            or resource.get("code", {}).get("coding", [{}])[0].get("display", "")
        )
        reaction = ""
        reactions = resource.get("reaction", [])
        if reactions:
            manifestations = reactions[0].get("manifestation", [])
            if manifestations:
                reaction = manifestations[0].get("text") or (
                    manifestations[0].get("coding", [{}])[0].get("display", "")
                )
        return f"{substance} — {reaction}" if reaction else substance

    if resource_type == "Condition":
        return (
            resource.get("code", {}).get("text")
            or resource.get("code", {}).get("coding", [{}])[0].get("display", "")
            or resource.get("id", "")
        )

    return resource.get("id", resource_type)


def _build_citation_index(ctx: BriefingContext) -> dict[str, list[Citation]]:
    """Build resource_type -> list[Citation] index from BriefingContext."""
    index: dict[str, list[Citation]] = {}

    def add(citations: list[Any]) -> None:
        for item in citations:
            c: Citation = item.citation if hasattr(item, "citation") else item
            index.setdefault(c.resource_type, []).append(c)

    add(ctx.active_conditions)
    add(ctx.allergies)
    add(ctx.active_medications)
    add(ctx.recent_vitals)
    add(ctx.recent_labs)
    return index


def _claim_is_attributed(claim: ClinicalClaim, index: dict[str, list[Citation]]) -> bool:
    citations = index.get(claim.source_resource, [])
    for citation in citations:
        code_match = not claim.source_code or citation.code == claim.source_code
        value_match = not claim.source_value or claim.source_value.lower() in citation.value.lower()
        if code_match and value_match:
            return True
    return False


def verify_briefing(
    briefing: BriefingResponse,
    ctx: BriefingContext,
    strict: bool = True,
) -> BriefingResponse:
    """Remove or warn on unattributed claims; enforce domain constraints."""
    index = _build_citation_index(ctx)
    alerts = list(briefing.alerts)
    verified_sections: list[BriefingSection] = []
    removed_count = 0

    # Domain constraints
    if ctx.has_blank_code_status and "BLANK_CODE_STATUS" not in " ".join(alerts):
        alerts.append("BLANK_CODE_STATUS: code-status field is empty — clarification required")

    if ctx.has_blank_allergy_section:
        for section in briefing.sections:
            if section.section == "allergies":
                lower_summary = section.summary.lower()
                if "no known allerg" in lower_summary or "nkda" in lower_summary:
                    alerts.append(
                        "ATTRIBUTION_VIOLATION: cannot assert NKDA — allergy section is blank"
                    )

    for section in briefing.sections:
        verified_claims: list[ClinicalClaim] = []
        for claim in section.claims:
            if _claim_is_attributed(claim, index):
                verified_claims.append(claim)
            else:
                removed_count += 1
                citations_for_resource = index.get(claim.source_resource, [])
                if not citations_for_resource:
                    failure_reason = "no_citations_for_source_resource"
                elif claim.source_value and not any(
                    claim.source_value.lower() in c.value.lower() for c in citations_for_resource
                ):
                    failure_reason = "source_value_no_substring_match"
                elif claim.source_code and not any(
                    c.code == claim.source_code for c in citations_for_resource
                ):
                    failure_reason = "source_code_mismatch"
                else:
                    failure_reason = "unknown"
                logger.warning(
                    "Unattributed claim removed",
                    extra={
                        "patient_id": ctx.patient_id,
                        "section": section.section,
                        "claim_text": claim.text,
                        "source_resource": claim.source_resource,
                        "source_code": claim.source_code,
                        "source_value": claim.source_value,
                        "failure_reason": failure_reason,
                        "citations_in_resource": len(citations_for_resource),
                    },
                )
                if not strict:
                    alerts.append(f"UNATTRIBUTED_CLAIM in {section.section}: {claim.text[:80]}")
                    verified_claims.append(claim)

        verified_sections.append(
            BriefingSection(
                section=section.section,
                summary=section.summary,
                claims=verified_claims,
            )
        )

    if removed_count:
        logger.info(
            "Attribution verification complete",
            extra={"patient_id": ctx.patient_id, "removed_claims": removed_count},
        )

    return BriefingResponse(
        patient_id=briefing.patient_id,
        name=briefing.name,
        sections=verified_sections,
        alerts=alerts,
        generated_at=briefing.generated_at,
    )
