"""Canonical Citation model for agent tool outputs.

Every clinical assertion in a tool result must be backed by a Citation.
Conversational/UX text (greetings, not-found acknowledgments, disclaimer footer)
does not require citations — see docs/UX_SPEC.md §6 claim taxonomy.

This is distinct from briefing.context_builder.Citation, which is an internal
FHIR-index object used during briefing context assembly. This model is the
standardized, patient-level citation included in every tool return dict.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

ClaimClass = Literal[
    "lab_value",
    "vital",
    "medication",
    "condition",
    "allergy",
    "code_status",
    "isolation",
]

CLINICAL_CLAIM_CLASSES: frozenset[str] = frozenset(
    ["lab_value", "vital", "medication", "condition", "allergy", "code_status", "isolation"]
)

CONVERSATIONAL_PATTERNS: tuple[str, ...] = (
    "good morning",
    "good afternoon",
    "good evening",
    "here is the briefing",
    "i searched",
    "did you mean",
    "i can surface",
    "the clinical decision is yours",
    "this summary is generated from ehr data",
    "verify critical values directly in the chart",
)


@dataclasses.dataclass(frozen=True)
class Citation:
    """Immutable citation linking a clinical claim to its FHIR source."""

    patient_id: str
    resource_type: str          # e.g. "Observation", "MedicationRequest", "Condition"
    resource_id: str            # FHIR resource.id
    effective_datetime: str | None   # ISO datetime string or None
    value_summary: str          # human-readable: "K+ 5.9 mEq/L (2026-04-28)"
    claim_class: ClaimClass     # one of the 7 clinical claim classes

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable plain dict."""
        return {
            "patient_id": self.patient_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "effective_datetime": self.effective_datetime,
            "value_summary": self.value_summary,
            "claim_class": self.claim_class,
        }


CitationList = list[Citation]


def is_conversational_text(text: str) -> bool:
    """Return True if text matches known conversational/UX patterns (no citation needed)."""
    lower = text.lower().strip()
    return any(pattern in lower for pattern in CONVERSATIONAL_PATTERNS)


def citations_for_fhir_resource(
    patient_id: str,
    resource: dict,
    claim_class: ClaimClass,
    value_summary: str,
) -> Citation:
    """Build a Citation from a FHIR resource dict."""
    resource_type = resource.get("resourceType", "Unknown")
    resource_id = resource.get("id", "")
    effective_datetime: str | None = (
        resource.get("effectiveDateTime")
        or resource.get("recordedDate")
        or resource.get("authoredOn")
        or resource.get("onsetDateTime")
        or None
    )
    return Citation(
        patient_id=patient_id,
        resource_type=resource_type,
        resource_id=resource_id,
        effective_datetime=effective_datetime,
        value_summary=value_summary,
        claim_class=claim_class,
    )
