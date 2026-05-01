"""Structured output schema for UC-2 Pre-Encounter Briefing responses.

Pydantic models define the wire format.  The LLM is instructed to produce
JSON conforming to BriefingResponse.  The verification layer validates
source attribution before the response is returned to the caller.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClinicalClaim(BaseModel):
    text: str = Field(description="The clinical statement in plain English.")
    source_resource: str = Field(description="FHIR resource type (Observation, Condition, etc.)")
    source_code: str = Field(description="LOINC or SNOMED code of the source record.")
    source_value: str = Field(description="Exact value from the source record.")
    source_dt: str = Field(description="ISO-8601 effective date/time of the source record.")


class BriefingSection(BaseModel):
    section: str = Field(description="Section name: diagnosis | medications | vitals | labs | allergies | alerts")
    summary: str = Field(description="1-2 sentence plain-English summary of the section.")
    claims: list[ClinicalClaim] = Field(description="Individual clinical claims with source attribution.")


class BriefingResponse(BaseModel):
    patient_id: str
    name: str
    # The LLM occasionally omits `sections` entirely when the only thing it has to
    # report are alerts (e.g., a patient with no documented vitals or labs visible
    # via FHIR). Default to [] rather than rejecting the response — the alerts list
    # carries the information either way, and rejection currently surfaces as a
    # blank briefing in the UI.
    sections: list[BriefingSection] = Field(default_factory=list)
    alerts: list[str] = Field(
        default_factory=list,
        description="Hard alerts that must always be shown: critical labs, blank code status, stale values.",
    )
    generated_at: str = Field(description="ISO-8601 UTC timestamp of generation.")
