"""Slice 5.3 — Mechanical rubric scorers.

Pure functions over a :class:`evals.runner.RunOutcome`. Each returns a
boolean per W2_ARCHITECTURE §11.2 — boolean rubrics keep the gate
unambiguous and turn each failure into a concrete fix task.
"""
from __future__ import annotations

import json
import re
import string
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

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


# --------------------------------------------------------------------------- #
# Citation-quality rubrics (Wave 2C — gate citation correctness mechanically)
# --------------------------------------------------------------------------- #


_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation})


def _normalize_text(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text.translate(_PUNCT_TABLE)).strip().lower()


def _tokenize(text: str) -> List[str]:
    """Token list = whitespace split of normalized text."""
    norm = _normalize_text(text)
    return norm.split() if norm else []


def _collect_layout_block_index(outcome: RunOutcome) -> dict[str, str]:
    """Build a {bbox_id: text} index from the outcome's OCR layout.

    Returns an empty dict when no layout was captured (probe disabled or graph
    skipped layout emission). Callers treat an empty index as "no resolution
    information available" — see each rubric for vacuous-True semantics.
    """
    index: dict[str, str] = {}
    layout = getattr(outcome, "ocr_layout", None) or []
    if not isinstance(layout, list):
        return index
    for blk in layout:
        if not isinstance(blk, dict):
            continue
        bid = blk.get("bbox_id") or blk.get("id") or blk.get("chunk_id")
        if not bid:
            continue
        # text might appear under various keys depending on extractor stage
        text = (
            blk.get("text")
            or blk.get("content")
            or blk.get("value")
            or ""
        )
        index[str(bid)] = str(text or "")
    return index


def _iter_citations_for_item(item: dict) -> Iterable[dict]:
    """Yield each citation dict on a cited item."""
    cits = item.get("citations") or []
    if not isinstance(cits, list):
        return
    for c in cits:
        if isinstance(c, dict):
            yield c


def _value_text_for_item(item: dict) -> str:
    """Best-effort 'value text' for an item.

    Lab values: ``value`` (numeric/string).
    KeyFact / TextField: ``text`` or ``value``.
    Medications: ``name`` (and ``dose`` if present).
    Allergies: ``substance`` (and ``reaction`` if present).
    Family history: ``relation`` + ``condition``.
    CodeStatus: ``value``.

    Falls back to the citation's ``quote_or_value`` if no item-level field
    surfaces — the citation contract guarantees that field exists.
    """
    parts: list[str] = []
    for k in ("value", "text", "name", "substance", "condition", "relation"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    if "dose" in item and isinstance(item["dose"], str) and item["dose"].strip():
        parts.append(item["dose"])
    if "reaction" in item and isinstance(item["reaction"], str) and item["reaction"].strip():
        parts.append(item["reaction"])
    return " ".join(parts).strip()


def citation_resolvable(outcome: RunOutcome) -> bool:
    """Every Citation's ``field_or_chunk_id`` resolves to a real LayoutBlock.

    Resolution is checked against the outcome's ``ocr_layout`` list — every
    citation's ``field_or_chunk_id`` must equal the ``bbox_id`` (or ``id`` /
    ``chunk_id``) of some block in the index.

    Vacuous-True cases (rubric does not gate):
      - Extraction is missing or not a dict.
      - No cited items at all (empty extraction; ``citation_present`` will
        catch the "should-have-had-claims" case via schema min_length).
      - No layout blocks were captured (probe disabled / runner skipped) —
        we cannot prove resolution either way; the rubric is informational.
      - Citation's ``source_type`` is not ``document`` (observation /
        guideline citations point at non-layout sources).
    """
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    layout_index = _collect_layout_block_index(outcome)
    if not layout_index:
        # No layout to check against — treat as vacuously satisfied.
        return True

    saw_any = False
    for item in _iter_cited_items(extraction):
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            saw_any = True
            field_id = cit.get("field_or_chunk_id")
            if not field_id or str(field_id) not in layout_index:
                return False
    # No document-typed citations at all → vacuous True.
    return True if not saw_any else True


def citation_row_match(outcome: RunOutcome) -> bool:
    """The cited block's text contains every value-token (case + punct
    normalized), in any order.

    Normalization rules (applied to BOTH the cited block text and the
    item's value text):
      1. Lower-case.
      2. Replace each ASCII-punctuation character with a space.
      3. Collapse runs of whitespace to single spaces, strip ends.

    A "token" is one whitespace-separated word of the normalized value.
    The rubric is True for an item iff EVERY value token appears as a
    token in the normalized cited block (set-membership; no order
    requirement). The block text is taken from the citation's resolved
    ``LayoutBlock`` (looked up by ``field_or_chunk_id``); if the citation
    cannot be resolved, the rubric falls back to the citation's
    ``quote_or_value`` field (which carries the same text by contract).

    Vacuous-True cases:
      - Same as :func:`citation_resolvable` (missing extraction, empty
        cited items, no layout, non-document citation).
      - Empty value text after normalization (no tokens to compare).
    """
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    layout_index = _collect_layout_block_index(outcome)

    for item in _iter_cited_items(extraction):
        value_text = _value_text_for_item(item)
        value_tokens = _tokenize(value_text)
        if not value_tokens:
            continue
        # Combine candidate haystacks: every cited block's resolved text,
        # plus the citation's quote_or_value as fallback.
        any_block_satisfied = False
        had_doc_citation = False
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            had_doc_citation = True
            field_id = str(cit.get("field_or_chunk_id") or "")
            block_text = layout_index.get(field_id, "") if layout_index else ""
            if not block_text:
                # Fall back to the citation's verbatim quote.
                block_text = str(cit.get("quote_or_value") or "")
            haystack_tokens = set(_tokenize(block_text))
            if all(tok in haystack_tokens for tok in value_tokens):
                any_block_satisfied = True
                break
        if had_doc_citation and not any_block_satisfied:
            return False
    return True


def citation_token_match(outcome: RunOutcome) -> bool:
    """Stricter than ``citation_row_match``: every value token appears
    in the cited block in the SAME ORDER (subsequence match).

    Other tokens may appear between value tokens — only the relative
    ordering of the value tokens themselves matters. The same
    normalization rules as :func:`citation_row_match` apply.

    Algorithm: walk the haystack token list once, advancing a pointer
    into the value-token list whenever a match is found. The rubric is
    True iff the pointer reaches the end of the value-token list. This
    is the classic O(n+m) subsequence test.

    Vacuous-True cases mirror :func:`citation_row_match`.
    """
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    layout_index = _collect_layout_block_index(outcome)

    for item in _iter_cited_items(extraction):
        value_text = _value_text_for_item(item)
        value_tokens = _tokenize(value_text)
        if not value_tokens:
            continue
        any_block_satisfied = False
        had_doc_citation = False
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            had_doc_citation = True
            field_id = str(cit.get("field_or_chunk_id") or "")
            block_text = layout_index.get(field_id, "") if layout_index else ""
            if not block_text:
                block_text = str(cit.get("quote_or_value") or "")
            haystack_tokens = _tokenize(block_text)
            # Subsequence test
            i = 0
            for tok in haystack_tokens:
                if i < len(value_tokens) and tok == value_tokens[i]:
                    i += 1
            if i == len(value_tokens):
                any_block_satisfied = True
                break
        if had_doc_citation and not any_block_satisfied:
            return False
    return True


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
    "citation_resolvable",
    "citation_row_match",
    "citation_token_match",
    "correct_critic_decision",
    "no_phi_in_logs",
    "keyword_match_in_citation",
]
