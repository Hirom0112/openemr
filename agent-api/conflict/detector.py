"""Pure cross-source conflict detector (Phase 9 Slice 9.7).

Operates on *normalized* dict rows — no I/O, no audit emission, no metrics.
The graph node ``graph.nodes.cross_source_conflict`` is the only caller in
production; the detector is split out so it can be exercised under
``pytest`` with hand-crafted fixtures and zero database setup.

Two-tier dedup (W2_ARCHITECTURE.md §5.11):

* **Tier 1 — collapse** key: ``(normalized_test_name, normalized_value,
  normalized_unit)``. Agreeing rows share one canonical fact; their
  citations merge.
* **Tier 2 — conflict** key: ``(normalized_test_name, collection_date,
  normalized_unit)``. Rows that share this wider key but disagree on
  ``normalized_value`` are surfaced as a soft-warn.

Cascade rule: the detector does NOT mutate inputs. Callers (the graph node)
decide which side to *not* re-stage based on the outcome — earlier-approved
rows are never re-staged; only the later-arriving disagreer is held as a
soft-warn pending row.

HL7 OBX-11 ``'C'`` (corrected result) handling: v1 defers — corrected
results are tagged on the input row but the detector treats them like any
other value at this point. Slice 9.10 / future work will surface OBX-11
explicitly. The plumbing is here so a v2 lands without a re-walk.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from extractors.schemas import Citation

from .ranking import rank_sources
from .schemas import CrossSourceConflict, CrossSourceConflictValue


# ── Row coercion ─────────────────────────────────────────────────────────────
#
# Inputs are dicts (not Pydantic) because callers pull them from heterogenous
# stores (staging table rows, FHIR Observation JSON, in-memory LabValue
# payloads). Coercion happens once here so the rest of the module can rely
# on a single shape.


def _row_to_value(row: Dict[str, Any]) -> Optional[CrossSourceConflictValue]:
    """Best-effort extract a :class:`CrossSourceConflictValue` from a row.

    Returns ``None`` when the row is missing the minimum-viable fields
    (test name, value). Missing date / unit are tolerated — the tiered key
    logic decides what to do with them.
    """
    test = row.get("normalized_test_name")
    value = row.get("normalized_value")
    if not test or value is None:
        return None
    coll = row.get("collection_date")
    coll_iso: Optional[str] = None
    if coll is not None:
        # Accept ``date``/``datetime`` or already-stringified ISO.
        coll_iso = coll.isoformat() if hasattr(coll, "isoformat") else str(coll)
    extracted = row.get("extracted_at")
    extracted_iso: Optional[str] = None
    if extracted is not None:
        extracted_iso = (
            extracted.isoformat() if hasattr(extracted, "isoformat") else str(extracted)
        )
    return CrossSourceConflictValue(
        source_format=str(row.get("source_format") or "unknown"),
        source_id=str(row.get("source_id") or ""),
        normalized_test_name=str(test),
        normalized_value=str(value),
        normalized_unit=row.get("normalized_unit"),
        collection_date_iso=coll_iso,
        extracted_at_iso=extracted_iso,
    )


def _row_citations(row: Dict[str, Any]) -> List[Citation]:
    raw = row.get("citations") or []
    out: List[Citation] = []
    for c in raw:
        try:
            out.append(c if isinstance(c, Citation) else Citation.model_validate(c))
        except Exception:
            # Fail-open on individual bad citations rather than discard
            # the whole row — the critic's citation rules cover hard
            # citation failures separately.
            continue
    return out


# ── Tiering ──────────────────────────────────────────────────────────────────


def _tier1_key(v: CrossSourceConflictValue) -> Tuple[str, str, str]:
    return (
        v.normalized_test_name,
        v.normalized_value,
        v.normalized_unit or "",
    )


def _tier2_key(v: CrossSourceConflictValue) -> Optional[Tuple[str, str, str]]:
    """Wider conflict key. ``None`` when ``collection_date`` is missing."""
    if v.collection_date_iso is None:
        return None
    return (
        v.normalized_test_name,
        v.collection_date_iso,
        v.normalized_unit or "",
    )


# ── Public API ───────────────────────────────────────────────────────────────


def detect_cross_source_conflicts(
    staged: Iterable[Dict[str, Any]],
    persisted: Iterable[Dict[str, Any]],
) -> List[CrossSourceConflict]:
    """Walk staged + persisted rows; emit one group per detected interaction.

    Pure deterministic function. Order of returned groups is stable: tier-1
    collapse groups first (sorted by dedup key), then tier-2 conflict
    groups, then date-missing deferrals.
    """
    all_rows: List[CrossSourceConflictValue] = []
    citations_by_idx: List[List[Citation]] = []
    for src in (staged, persisted):
        for row in src:
            v = _row_to_value(row)
            if v is None:
                continue
            all_rows.append(v)
            citations_by_idx.append(_row_citations(row))

    if not all_rows:
        return []

    # Tier-1 collapse: group by (test, value, unit). Any group with ≥2
    # rows is a collapse outcome — citations merge.
    tier1_groups: Dict[Tuple[str, str, str], List[int]] = {}
    for idx, v in enumerate(all_rows):
        tier1_groups.setdefault(_tier1_key(v), []).append(idx)

    collapse_emissions: List[CrossSourceConflict] = []
    collapsed_indices: set[int] = set()
    for key, idxs in sorted(tier1_groups.items()):
        if len(idxs) < 2:
            continue
        collapsed_indices.update(idxs)
        merged_citations: List[Citation] = []
        seen_cite_ids: set[str] = set()
        for i in idxs:
            for c in citations_by_idx[i]:
                cid = f"{c.source_type}|{c.source_id}|{c.field_or_chunk_id}|{c.quote_or_value}"
                if cid in seen_cite_ids:
                    continue
                seen_cite_ids.add(cid)
                merged_citations.append(c)
        ordered = rank_sources([all_rows[i] for i in idxs])
        collapse_emissions.append(
            CrossSourceConflict(
                outcome="collapse",
                tier="1",
                dedup_key=key,
                values=ordered,
                citations=merged_citations,
                source_trust_ordered=[v.source_format for v in ordered],
            )
        )

    # Tier-2 conflict: group by (test, date, unit) over rows NOT already
    # collapsed at tier-1. A group with ≥2 rows AND ≥2 distinct values
    # is a soft-warn. ≥2 rows but identical values were already caught at
    # tier-1 (above) — we don't re-emit.
    tier2_groups: Dict[Tuple[str, str, str], List[int]] = {}
    date_missing: List[int] = []
    for idx, v in enumerate(all_rows):
        if idx in collapsed_indices:
            continue
        k = _tier2_key(v)
        if k is None:
            date_missing.append(idx)
            continue
        tier2_groups.setdefault(k, []).append(idx)

    conflict_emissions: List[CrossSourceConflict] = []
    for key, idxs in sorted(tier2_groups.items()):
        if len(idxs) < 2:
            continue
        distinct_values = {all_rows[i].normalized_value for i in idxs}
        if len(distinct_values) < 2:
            continue  # tier-1 would have caught this; defensive
        merged_citations: List[Citation] = []
        seen_cite_ids: set[str] = set()
        for i in idxs:
            for c in citations_by_idx[i]:
                cid = f"{c.source_type}|{c.source_id}|{c.field_or_chunk_id}|{c.quote_or_value}"
                if cid in seen_cite_ids:
                    continue
                seen_cite_ids.add(cid)
                merged_citations.append(c)
        ordered = rank_sources([all_rows[i] for i in idxs])
        conflict_emissions.append(
            CrossSourceConflict(
                outcome="conflict_soft_warn",
                tier="2",
                dedup_key=key,
                values=ordered,
                citations=merged_citations,
                source_trust_ordered=[v.source_format for v in ordered],
            )
        )

    # Date-missing deferrals: only emit when a date-missing row shares a
    # (test, unit) bucket with at least one OTHER row (date-known or
    # date-missing) — a lone row has nothing to conflict with.
    date_missing_emissions: List[CrossSourceConflict] = []
    if date_missing:
        partner_buckets: Dict[Tuple[str, str], List[int]] = {}
        for idx, v in enumerate(all_rows):
            if idx in collapsed_indices:
                continue
            partner_buckets.setdefault(
                (v.normalized_test_name, v.normalized_unit or ""), []
            ).append(idx)

        emitted_partner_keys: set[Tuple[str, str]] = set()
        for missing_idx in date_missing:
            v = all_rows[missing_idx]
            partner_key = (v.normalized_test_name, v.normalized_unit or "")
            if partner_key in emitted_partner_keys:
                continue
            partners = partner_buckets.get(partner_key, [])
            if len(partners) < 2:
                continue
            emitted_partner_keys.add(partner_key)
            ordered = rank_sources([all_rows[i] for i in partners])
            merged_citations: List[Citation] = []
            seen_cite_ids: set[str] = set()
            for i in partners:
                for c in citations_by_idx[i]:
                    cid = f"{c.source_type}|{c.source_id}|{c.field_or_chunk_id}|{c.quote_or_value}"
                    if cid in seen_cite_ids:
                        continue
                    seen_cite_ids.add(cid)
                    merged_citations.append(c)
            date_missing_emissions.append(
                CrossSourceConflict(
                    outcome="date_missing",
                    tier="2",
                    dedup_key=(partner_key[0], "", partner_key[1]),
                    values=ordered,
                    citations=merged_citations,
                    source_trust_ordered=[v.source_format for v in ordered],
                )
            )

    return collapse_emissions + conflict_emissions + date_missing_emissions


__all__ = ["detect_cross_source_conflicts"]
