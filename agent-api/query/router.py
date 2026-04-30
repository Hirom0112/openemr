"""Query router for UC-3 Targeted Record Query.

Routes natural-language queries to the appropriate FHIR resource(s).

Strategy: classifier-first, LLM fallback on low confidence.
- The classifier is a keyword/regex map (fast, deterministic, ~80% hit rate).
- If classifier confidence is below threshold, the LLM classifies.
- The router returns a list of (resource_type, params) tuples for FHIR search.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

_CLASSIFIER_CONFIDENCE_THRESHOLD = 0.7
_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class QueryRoute:
    resource: str
    params: dict[str, str]
    confidence: float
    source: str  # "classifier" or "llm"


# ── Keyword classifier ────────────────────────────────────────────────────────

_PATTERNS: list[tuple[re.Pattern, str, dict[str, str], float]] = [
    (re.compile(r"\b(potassium|k\+|sodium|chloride|magnesium|calcium|bicarb|bun|creatinine|glucose|hemoglobin|hgb|hematocrit|hct|wbc|platelet|inr|troponin|bnp|lactate|albumin)\b", re.I), "Observation", {"category": "laboratory"}, 0.95),
    (re.compile(r"\b(blood pressure|bp|systolic|diastolic|heart rate|hr|pulse|respiratory rate|rr|temperature|temp|spo2|oxygen sat|o2 sat|saturation|weight|bmi)\b", re.I), "Observation", {"category": "vital-signs"}, 0.95),
    (re.compile(r"\b(medications?|meds?|drugs?|prescriptions?|dosage|metoprolol|lisinopril|aspirin|furosemide|insulin|heparin|coumadin|warfarin|antibiotics?)\b", re.I), "MedicationRequest", {"status": "active"}, 0.90),
    (re.compile(r"\b(diagnos\w*|conditions?|problems?|diseases?|disorders?|diabetes|hypertension|heart failure|copd|pneumonia|sepsis|uti|afib)\b", re.I), "Condition", {"clinical-status": "active"}, 0.90),
    (re.compile(r"\b(allerg\w*|reaction|intolerance|penicillin|sulfa|contrast|latex|nsaid)\b", re.I), "AllergyIntolerance", {}, 0.92),
    (re.compile(r"\b(procedures?|surgery|operation|catheter|intubat\w*|dialysis|transfusion|biopsy)\b", re.I), "Procedure", {}, 0.88),
    (re.compile(r"\b(imaging|ct|mri|x.ray|xray|ultrasound|echo|echocardiogram|radiology|report)\b", re.I), "DiagnosticReport", {"category": "LAB"}, 0.85),
]


def _classify(query: str) -> QueryRoute | None:
    for pattern, resource, params, confidence in _PATTERNS:
        if pattern.search(query):
            return QueryRoute(resource=resource, params=params, confidence=confidence, source="classifier")
    return None


# ── LLM fallback classifier ───────────────────────────────────────────────────

_LLM_SYSTEM = """You are a FHIR query classifier. Given a clinical question, output JSON:
{"resource": "<FHIR resource type>", "params": {"key": "value"}, "reasoning": "one sentence"}

Valid resource types: Observation, MedicationRequest, Condition, AllergyIntolerance, Procedure, DiagnosticReport, Patient
For Observation, include category: "vital-signs" or "laboratory" as appropriate.
Output only valid JSON, no preamble."""


async def _llm_classify(query: str, patient_id: str) -> QueryRoute:
    import anthropic  # lazy — not needed for classifier path
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=200,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": f"Patient ID: {patient_id}\nQuery: {query}"}],
        )
        import json
        data = json.loads(response.content[0].text.strip())
        return QueryRoute(
            resource=data.get("resource", "Observation"),
            params=data.get("params", {}),
            confidence=0.75,
            source="llm",
        )
    except Exception as exc:
        logger.warning("LLM classifier failed, defaulting to Observation", extra={"error": str(exc)})
        return QueryRoute(resource="Observation", params={}, confidence=0.5, source="llm-fallback")


async def route(query: str, patient_id: str) -> QueryRoute:
    """Classify query and return the appropriate FHIR route."""
    result = _classify(query)
    if result and result.confidence >= _CLASSIFIER_CONFIDENCE_THRESHOLD:
        logger.debug("Classifier routed query", extra={"resource": result.resource, "confidence": result.confidence})
        return result

    logger.debug("Classifier confidence low, using LLM", extra={"query": query[:80]})
    return await _llm_classify(query, patient_id)
