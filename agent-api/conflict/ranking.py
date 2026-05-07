"""Source-trust ordering helpers (Phase 9 Slice 9.7).

Deterministic source-trust order per W2_ARCHITECTURE §5.11:

    HL7 ORU > FHIR Observation > DOCX > XLSX

Recency (``extracted_at``) breaks ties within the same source-format bucket.
The ordering is *deterministic* — there is no LLM in the loop. The Co-Pilot
surfaces both/all values with explicit source labels; it never silently
picks one. This module just provides the canonical sort order used by the
brief renderer and the audit emitter so the same value list is rendered
consistently across surfaces.
"""

from __future__ import annotations

from typing import Iterable, List

from .schemas import CrossSourceConflictValue

# Lower rank = higher trust. Unknown source formats sort last.
_TRUST_RANK: dict[str, int] = {
    "hl7_oru": 0,
    "fhir_observation": 1,
    "docx": 2,
    "xlsx": 3,
}

_UNKNOWN_RANK: int = 99


def _trust_rank(source_format: str) -> int:
    return _TRUST_RANK.get(source_format, _UNKNOWN_RANK)


def rank_sources(
    values: Iterable[CrossSourceConflictValue],
) -> List[CrossSourceConflictValue]:
    """Return ``values`` sorted by source-trust then by recency.

    Tie-breaker: more-recent ``extracted_at_iso`` wins (later ISO string sorts
    later in lex order, so we negate by sorting descending on that field
    *within* a trust bucket). Missing ``extracted_at_iso`` sorts oldest.
    """
    # Python's sort is stable. We do a single key-sort: (trust_rank,
    # -extracted_at_position). To express "most recent first" with a
    # string-sort key we use the negative-lex trick: sort by ``"" if None
    # else extracted_at_iso`` descending, which is equivalent to sorting
    # ascending on the negated key. Easiest correct expression: sort once
    # by extracted_at descending, then again by trust ascending.
    by_recency = sorted(
        values,
        key=lambda v: v.extracted_at_iso or "",
        reverse=True,
    )
    by_trust = sorted(by_recency, key=lambda v: _trust_rank(v.source_format))
    return by_trust


def source_pair_label(source_formats: Iterable[str]) -> str:
    """Return a stable Prometheus label for the participating source formats.

    * Two unique formats →  ``"<lhs>|<rhs>"``  (lex-sorted)
    * Three or more     →  ``"multi"``
    * One               →  ``"<format>"``  (defensive — caller should not
      have produced a conflict group with a single source, but the label
      stays well-defined)
    """
    uniq = sorted(set(source_formats))
    if len(uniq) == 0:
        return "unknown"
    if len(uniq) == 1:
        return uniq[0]
    if len(uniq) == 2:
        return f"{uniq[0]}|{uniq[1]}"
    return "multi"


__all__ = ["rank_sources", "source_pair_label"]
