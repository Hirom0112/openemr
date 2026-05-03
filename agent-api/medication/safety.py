"""UC-4 Medication Safety Surface.

Surfaces medication safety signals for a patient:
  - Active medications with dose/route/status
  - Allergy conflicts (medication vs. known allergies)
  - Critical lab interactions (e.g. renal-clearance drugs + elevated creatinine)
  - High-alert medication flags (anticoagulants, insulin, opioids)

The safety check is deterministic (rule-based) for high-alert flags and
allergy conflicts.  The LLM is called only to produce a human-readable
summary — it does not determine safety status.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langfuse import Langfuse

from agent.response_schemas import PRODUCE_SAFETY_SUMMARY
from config import settings
from verification.domain_constraints import verify_safety_summary

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

# ── High-alert medication patterns ───────────────────────────────────────────

_HIGH_ALERT = re.compile(
    r"\b(heparin|warfarin|coumadin|insulin|morphine|hydromorphone|fentanyl|"
    r"oxycodone|methadone|vancomycin|gentamicin|tobramycin|amikacin|digoxin|"
    r"lithium|methotrexate|chemotherapy|cytarabine|vincristine)\b",
    re.IGNORECASE,
)

# Renal-clearance drugs that require dose adjustment at elevated creatinine
_RENAL_CLEARANCE = re.compile(
    r"\b(metformin|vancomycin|gentamicin|tobramycin|amikacin|digoxin|"
    r"gabapentin|pregabalin|enoxaparin|dabigatran|rivaroxaban)\b",
    re.IGNORECASE,
)

_HIGH_CREATININE_THRESHOLD = 1.5  # mg/dL
_LOINC_CREATININE = "33914-3"


@dataclass
class SafetyFlag:
    severity: str   # "HIGH" | "MODERATE" | "INFO"
    code: str       # machine-readable flag code
    message: str
    medication: str
    source: str     # "allergy-check" | "lab-interaction" | "high-alert"


@dataclass
class MedicationSafetyReport:
    patient_id: str
    flags: list[SafetyFlag] = field(default_factory=list)
    summary: str = ""
    medications_reviewed: int = 0
    # ISO-8601 UTC timestamp reflecting the freshness of the underlying FHIR
    # bundle (preferred) or the moment this report was assembled (fallback).
    # Mirrors the briefing's ``generated_at`` so the renderer can display a
    # "Data as of HH:MM · Refresh" freshness indicator.
    generated_at: str = ""


def _extract_med_name(med_resource: dict) -> str:
    coding = med_resource.get("medicationCodeableConcept", {}).get("coding", [{}])
    return coding[0].get("display", "") or med_resource.get("medicationCodeableConcept", {}).get("text", "Unknown")


def _extract_allergy_substance(allergy_resource: dict) -> str:
    coding = allergy_resource.get("code", {}).get("coding", [{}])
    return coding[0].get("display", "") or allergy_resource.get("code", {}).get("text", "")


def _check_allergy_conflict(med_name: str, allergies: list[dict]) -> list[SafetyFlag]:
    flags: list[SafetyFlag] = []
    med_lower = med_name.lower()
    for allergy in allergies:
        substance = _extract_allergy_substance(allergy).lower()
        if substance and (substance in med_lower or med_lower in substance):
            flags.append(SafetyFlag(
                severity="HIGH",
                code="ALLERGY_CONFLICT",
                message=f"Active medication '{med_name}' conflicts with documented allergy to '{substance}'.",
                medication=med_name,
                source="allergy-check",
            ))
    return flags


def _check_renal_interaction(med_name: str, observations: list[dict]) -> list[SafetyFlag]:
    flags: list[SafetyFlag] = []
    if not _RENAL_CLEARANCE.search(med_name):
        return flags

    for obs in observations:
        codings = obs.get("code", {}).get("coding", [{}])
        loinc = codings[0].get("code", "") if codings else ""
        if loinc == _LOINC_CREATININE:
            val = obs.get("valueQuantity", {}).get("value")
            if val is not None and float(val) > _HIGH_CREATININE_THRESHOLD:
                flags.append(SafetyFlag(
                    severity="MODERATE",
                    code="RENAL_INTERACTION",
                    message=f"'{med_name}' is renally cleared; creatinine is {val:.2f} mg/dL (>{_HIGH_CREATININE_THRESHOLD}). Dose review may be indicated.",
                    medication=med_name,
                    source="lab-interaction",
                ))
    return flags


def run_safety_checks(
    patient_id: str,
    medications: list[dict],
    allergies: list[dict],
    observations: list[dict],
) -> MedicationSafetyReport:
    """Deterministic safety check — no LLM involved."""
    report = MedicationSafetyReport(patient_id=patient_id, medications_reviewed=len(medications))

    for med_resource in medications:
        med_name = _extract_med_name(med_resource)
        report.flags.extend(_check_allergy_conflict(med_name, allergies))
        report.flags.extend(_check_renal_interaction(med_name, observations))

        if _HIGH_ALERT.search(med_name):
            report.flags.append(SafetyFlag(
                severity="INFO",
                code="HIGH_ALERT_MED",
                message=f"'{med_name}' is a high-alert medication requiring double-check protocol.",
                medication=med_name,
                source="high-alert",
            ))

    report.flags.sort(key=lambda f: {"HIGH": 0, "MODERATE": 1, "INFO": 2}.get(f.severity, 3))
    return report


async def add_llm_summary(
    report: "MedicationSafetyReport",
    langfuse: "Langfuse | None" = None,
) -> "MedicationSafetyReport":
    """Attach a one-paragraph LLM summary to the safety report."""
    if not report.flags:
        report.summary = "No medication safety flags detected for the active medication list."
        return report

    import anthropic  # lazy — only needed for LLM summary path
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    flag_text = "\n".join(f"[{f.severity}] {f.message}" for f in report.flags)
    prompt = (
        f"Summarize the following medication safety flags for a hospitalist in 2-3 sentences. "
        f"State each flag severity and drug name. Do not make recommendations. "
        f"Call the produce_safety_summary tool with your summary.\n\n{flag_text}"
    )

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
            tools=[PRODUCE_SAFETY_SUMMARY],
            tool_choice={"type": "any"},
        )
        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            raise ValueError("No tool_use block in safety summary response")
        raw_summary = tool_block.input.get("summary", "").strip()
        report.summary = verify_safety_summary(raw_summary, report.patient_id, has_flags=bool(report.flags))
    except Exception as exc:
        logger.warning("Safety summary LLM call failed", extra={"patient_id": report.patient_id, "error": str(exc)})
        report.summary = f"{len(report.flags)} safety flag(s) detected. Review flags above."

    return report
