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


def keyword_match_in_citation(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Evidence-retrieval rubric — boolean, evaluated against real retrieval output.

    Vacuously ``True`` for cases without evidence-retrieval expectations
    (i.e. ``case.evidence_query`` is falsy or unset).

    For evidence-retrieval cases this inspects ``outcome.retrieval`` —
    the snippet list returned by ``rag.retrieve.search`` via the
    LangGraph ``evidence_retriever`` node — and returns ``True`` iff:

      * at least one snippet's ``source_id`` matches one of
        ``case.expected_must_cite_source_id``, AND
      * for every keyword in ``case.expected_keywords_in_quote``, at
        least one snippet's ``content`` (case-insensitive) contains it.

    When ``expected_keywords_in_quote`` is empty the keyword check is
    skipped — the source-id match alone is sufficient.

    A skipped run (``outcome.skipped_reason`` set, e.g. when the host
    lacks ``AUDIT_DB_URL`` / ``VOYAGE_API_KEY``) is treated as vacuously
    ``True`` here; the suite-level reporter surfaces the skip status
    separately so it cannot mask a real regression.
    """
    if case is None:
        return True
    query = getattr(case, "evidence_query", None)
    if not query:
        return True
    sources = tuple(getattr(case, "expected_must_cite_source_id", ()) or ())
    keywords = tuple(getattr(case, "expected_keywords_in_quote", ()) or ())
    if not sources:
        # Definitional check — half-populated evidence cases must not
        # silently pass.
        return False

    # Skipped (no Postgres / no Voyage key) — don't fail the case.
    if getattr(outcome, "skipped_reason", None):
        return True

    retrieval = getattr(outcome, "retrieval", None) or {}
    snippets = retrieval.get("snippets") if isinstance(retrieval, dict) else None
    if not isinstance(snippets, list) or not snippets:
        return False

    expected_sources_lc = {str(s).lower() for s in sources}
    matching_snippets = [
        s for s in snippets
        if isinstance(s, dict)
        and str(s.get("source_id") or "").lower() in expected_sources_lc
    ]
    if not matching_snippets:
        return False

    if not keywords:
        return True

    # Every keyword must appear (case-insensitive) in at least one
    # snippet's content. Search across all snippets, not only the
    # source-matched ones — the source check above already gates the
    # citation; the content check here verifies the corpus actually
    # surfaced the answer text.
    haystacks = [str(s.get("content") or "").lower() for s in snippets if isinstance(s, dict)]
    for kw in keywords:
        needle = str(kw).lower()
        if not any(needle in hay for hay in haystacks):
            return False
    return True


__all__ = [
    "schema_valid",
    "citation_present",
    "correct_critic_decision",
    "no_phi_in_logs",
    "keyword_match_in_citation",
]
