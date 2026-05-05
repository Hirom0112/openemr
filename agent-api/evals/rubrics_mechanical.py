"""Slice 5.3 — Mechanical rubric scorers.

Pure functions over a :class:`evals.runner.RunOutcome`. Each returns a
boolean per W2_ARCHITECTURE §11.2 — boolean rubrics keep the gate
unambiguous and turn each failure into a concrete fix task.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional, Set

from pydantic import ValidationError

from extractors.schemas import IntakeForm, LabReport, UnknownDocument

from .runner import RunOutcome


_SCHEMAS_BY_KIND = {
    "lab_report": LabReport,
    "intake_form": IntakeForm,
    "unknown": UnknownDocument,
}


_PHI_VALUES_PATH = Path(__file__).parent / "synthetic_phi_values.json"


def _load_default_phi_values() -> Set[str]:
    try:
        return set(json.loads(_PHI_VALUES_PATH.read_text()))
    except FileNotFoundError:
        return set()


# --------------------------------------------------------------------------- #
# Rubrics
# --------------------------------------------------------------------------- #


def schema_valid(outcome: RunOutcome) -> bool:
    """Pydantic v2 strict-mode validation against the kind-discriminated schema."""
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    kind = extraction.get("kind")
    schema = _SCHEMAS_BY_KIND.get(str(kind))
    if schema is None:
        return False
    try:
        schema.model_validate_json(json.dumps(extraction))
        return True
    except (ValidationError, ValueError, TypeError):
        return False


def _iter_cited_items(extraction: dict) -> Iterable[dict]:
    """Yield every dict in the extraction that should carry a ``citations`` list."""
    kind = extraction.get("kind")
    if kind == "lab_report":
        for value in extraction.get("values") or []:
            if isinstance(value, dict):
                yield value
    elif kind == "unknown":
        for fact in extraction.get("key_facts") or []:
            if isinstance(fact, dict):
                yield fact
    elif kind == "intake_form":
        # Every cited TextField/MedicationItem/etc. carries a citations list.
        demographics = extraction.get("demographics") or {}
        if isinstance(demographics, dict):
            for v in demographics.values():
                if isinstance(v, dict) and "citations" in v:
                    yield v
        chief = extraction.get("chief_concern")
        if isinstance(chief, dict) and "citations" in chief:
            yield chief
        for key in ("current_medications", "allergies", "family_history"):
            for item in extraction.get(key) or []:
                if isinstance(item, dict):
                    yield item
        code_status = extraction.get("code_status")
        if isinstance(code_status, dict) and "citations" in code_status:
            yield code_status


def citation_present(outcome: RunOutcome) -> bool:
    """Every cited item in the extraction must carry at least one citation."""
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    saw_any = False
    for item in _iter_cited_items(extraction):
        saw_any = True
        citations = item.get("citations")
        if not isinstance(citations, list) or len(citations) == 0:
            return False
    # Empty extractions (no clinical claims) are vacuously fine — the schema
    # rubric catches "should have had values" cases via min_length/required.
    return saw_any or extraction.get("kind") == "intake_form"


def correct_critic_decision(outcome: RunOutcome, *, expected: str) -> bool:
    return outcome.critic_decision == expected


def _record_contains(record: dict, needles: Set[str]) -> bool:
    """True iff any needle appears in the record's message OR any extra value."""
    haystack_parts: list[str] = [str(record.get("message") or "")]
    extra = record.get("extra") or {}
    if isinstance(extra, dict):
        for value in extra.values():
            try:
                haystack_parts.append(str(value))
            except Exception:  # pragma: no cover — defensive
                continue
    haystack = "\n".join(haystack_parts)
    return any(needle and needle in haystack for needle in needles)


def no_phi_in_logs(
    outcome: RunOutcome,
    *,
    synthetic_phi_values: Optional[Set[str]] = None,
) -> bool:
    """No log message or extra value may contain a synthetic-PHI token."""
    needles = synthetic_phi_values
    if needles is None:
        needles = _load_default_phi_values()
    if not needles:
        return True
    for record in outcome.captured_logs:
        if _record_contains(record, needles):
            return False
    return True


__all__ = [
    "schema_valid",
    "citation_present",
    "correct_critic_decision",
    "no_phi_in_logs",
]
