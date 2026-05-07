"""Pydantic schemas for the cross-source conflict pass (Phase 9 Slice 9.7).

See W2_ARCHITECTURE.md §5.11. The detector emits one
:class:`CrossSourceConflict` per dedup group it inspects — collapse, conflict
soft-warn, or deferred-when-date-missing — so callers (graph node, audit
emitter) can iterate uniformly without branching on outcome shape.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from extractors.schemas import Citation


# Outcome taxonomy:
#
# * ``collapse``            — Tier-1 §7.5 key match. All participating
#                             rows agree on ``(normalized_test_name,
#                             normalized_value, normalized_unit)``; their
#                             citations merge into one canonical row.
# * ``conflict_soft_warn``  — Tier-2 wider key match (test+date+unit) but
#                             distinct values. Surfaced via critic
#                             ``soft_warns``; both/all values rendered in
#                             the brief ordered by source trust.
# * ``date_missing``        — Tier-2 could not run because at least one
#                             participating row has no ``collection_date``.
#                             Deferred rather than over-collapsed; the
#                             pass emits the group for audit but takes no
#                             action.
ConflictOutcome = Literal["collapse", "conflict_soft_warn", "date_missing"]


class CrossSourceConflictValue(BaseModel):
    """One participating value in a cross-source conflict group."""

    model_config = ConfigDict(strict=True, extra="forbid")

    source_format: str           # ``hl7_oru | fhir_observation | docx | xlsx``
    source_id: str               # document_reference_id or observation id
    normalized_test_name: str
    normalized_value: str
    normalized_unit: Optional[str] = None
    collection_date_iso: Optional[str] = None  # ISO 8601 string; PHI-safe (no PHI implied by date alone)
    extracted_at_iso: Optional[str] = None     # tie-breaker for source-trust ordering


class CrossSourceConflict(BaseModel):
    """One dedup group surfaced by the cross-source conflict pass.

    The ``dedup_key`` carries the tier-1 collapse key for ``collapse`` outcomes
    and the tier-2 wider key for ``conflict_soft_warn`` / ``date_missing``
    outcomes. Two distinct fields would have been more honest, but every
    consumer (audit emitter, soft-warn renderer, eval rubric) treats the
    group as a single unit identified by *some* key — collapsing into one
    field keeps the JSON shape stable across outcomes.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    outcome: ConflictOutcome
    tier: Literal["1", "2"]
    dedup_key: Tuple[str, ...]   # tier-1: 3-tuple; tier-2: 3-tuple (test, date, unit)
    values: List[CrossSourceConflictValue] = Field(min_length=1)
    citations: List[Citation] = Field(default_factory=list)
    source_trust_ordered: List[str] = Field(default_factory=list)


__all__ = [
    "CrossSourceConflict",
    "CrossSourceConflictValue",
    "ConflictOutcome",
]
