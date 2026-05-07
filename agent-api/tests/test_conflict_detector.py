"""Phase 9 Slice 9.7 — pure detector unit tests.

Covers the cases called out by the slice spec:

* tier-1 collapse — three sources agree → one collapse group with merged
  citations.
* tier-2 conflict — HL7=142 vs XLSX=140 same date → soft-warn group with
  both values, source-trust ordered (HL7 first).
* date-missing edge — XLSX value with no date vs HL7 with date → no
  silent collapse, ``date_missing`` deferral group emitted.
* OBX-11='C' v1 deferral — corrected HL7 result is treated like any other
  value at this point (v1 plumbing only). The collapse / conflict logic
  ignores the OBX-11 marker.
* persisted+staged interaction — a persisted FHIR Observation matches a
  newly-staged HL7 row → collapse outcome, no soft-warn.
"""

from __future__ import annotations

from datetime import date

import pytest

from conflict.detector import detect_cross_source_conflicts
from conflict.schemas import CrossSourceConflict

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


def _row(
    *,
    source_format: str,
    source_id: str,
    value: str = "142",
    test: str = "ldl",
    unit: str = "mg/dl",
    coll: date | None = date(2026, 4, 1),
    extracted: str = "2026-04-01T10:00:00Z",
    citations: list[dict] | None = None,
    obx11: str | None = None,
) -> dict:
    cite = citations or [
        {
            "source_type": "document",
            "source_id": source_id,
            "field_or_chunk_id": f"{source_id}-f1",
            "quote_or_value": value,
        }
    ]
    out = {
        "source_format": source_format,
        "source_id": source_id,
        "normalized_test_name": test,
        "normalized_value": value,
        "normalized_unit": unit,
        "collection_date": coll,
        "extracted_at": extracted,
        "citations": cite,
    }
    if obx11 is not None:
        out["obx_observation_result_status"] = obx11
    return out


def _by_outcome(groups: list[CrossSourceConflict]) -> dict[str, list[CrossSourceConflict]]:
    bucket: dict[str, list[CrossSourceConflict]] = {}
    for g in groups:
        bucket.setdefault(g.outcome, []).append(g)
    return bucket


# ── Tier-1 collapse ──────────────────────────────────────────────────────────


def test_tier1_collapse_three_sources_same_value() -> None:
    """3 sources with identical (test, value, unit) → one collapse, 3 citations."""
    rows = [
        _row(source_format="hl7_oru", source_id="hl7-1"),
        _row(source_format="xlsx", source_id="xlsx-1"),
        _row(source_format="docx", source_id="docx-1"),
    ]

    groups = detect_cross_source_conflicts(rows, [])
    bucket = _by_outcome(groups)

    assert len(bucket.get("collapse", [])) == 1
    assert "conflict_soft_warn" not in bucket
    grp = bucket["collapse"][0]
    assert grp.tier == "1"
    assert grp.dedup_key == ("ldl", "142", "mg/dl")
    assert len(grp.values) == 3
    assert len(grp.citations) == 3
    # Source-trust order: HL7 ORU > FHIR > DOCX > XLSX. With no FHIR row,
    # the order is HL7 → DOCX → XLSX.
    assert grp.source_trust_ordered == ["hl7_oru", "docx", "xlsx"]


# ── Tier-2 conflict ──────────────────────────────────────────────────────────


def test_tier2_conflict_hl7_vs_xlsx_distinct_values() -> None:
    """Same test+date+unit, distinct values → soft-warn with HL7 ranked first."""
    rows = [
        _row(source_format="hl7_oru", source_id="hl7-1", value="142"),
        _row(source_format="xlsx", source_id="xlsx-1", value="140"),
    ]

    groups = detect_cross_source_conflicts(rows, [])
    bucket = _by_outcome(groups)

    assert "collapse" not in bucket
    assert len(bucket.get("conflict_soft_warn", [])) == 1
    grp = bucket["conflict_soft_warn"][0]
    assert grp.tier == "2"
    assert grp.dedup_key == ("ldl", "2026-04-01", "mg/dl")
    values = {v.normalized_value for v in grp.values}
    assert values == {"140", "142"}
    # HL7 ORU outranks XLSX in source-trust order.
    assert grp.source_trust_ordered[0] == "hl7_oru"
    assert grp.source_trust_ordered[-1] == "xlsx"
    # Citations from BOTH rows must be carried so the brief can render
    # source labels alongside both values.
    assert len(grp.citations) == 2


# ── Date-missing edge ────────────────────────────────────────────────────────


def test_date_missing_emits_deferral_not_collapse() -> None:
    """Tier-2 cannot fire when one row has no collection_date."""
    rows = [
        _row(source_format="hl7_oru", source_id="hl7-1", value="142"),
        _row(source_format="xlsx", source_id="xlsx-1", value="140", coll=None),
    ]

    groups = detect_cross_source_conflicts(rows, [])
    bucket = _by_outcome(groups)

    # Distinct values + missing date → date_missing deferral, not silent
    # collapse and not conflict_soft_warn.
    assert "collapse" not in bucket
    assert "conflict_soft_warn" not in bucket
    assert len(bucket.get("date_missing", [])) == 1
    grp = bucket["date_missing"][0]
    assert grp.outcome == "date_missing"
    # Both rows are surfaced for audit; caller can still render them
    # explicitly side-by-side without picking one.
    assert len(grp.values) == 2


# ── OBX-11 'C' v1 deferral ───────────────────────────────────────────────────


def test_obx11_corrected_v1_no_special_handling() -> None:
    """Corrected HL7 result is plumbed through but does NOT change outcomes in v1.

    The OBX-11='C' marker is carried on the input row so a v2 can promote
    the corrected value automatically; v1 still surfaces both values via
    the same conflict_soft_warn path.
    """
    rows = [
        _row(
            source_format="hl7_oru",
            source_id="hl7-original",
            value="142",
            obx11="F",
        ),
        _row(
            source_format="hl7_oru",
            source_id="hl7-correction",
            value="138",
            obx11="C",
            extracted="2026-04-02T10:00:00Z",
        ),
    ]

    groups = detect_cross_source_conflicts(rows, [])
    bucket = _by_outcome(groups)

    # Two HL7 rows, same key, different values, no FHIR/XLSX → still a
    # tier-2 conflict_soft_warn. v1 does not silently pick the 'C' row.
    assert len(bucket.get("conflict_soft_warn", [])) == 1


# ── Persisted + staged interaction ───────────────────────────────────────────


def test_persisted_observation_matches_staged_hl7_collapse() -> None:
    """Persisted FHIR Observation + new HL7 stage with same fact → collapse."""
    persisted = [
        _row(
            source_format="fhir_observation",
            source_id="obs-existing-1",
            value="142",
        ),
    ]
    staged = [
        _row(source_format="hl7_oru", source_id="hl7-newly-staged", value="142"),
    ]

    groups = detect_cross_source_conflicts(staged, persisted)
    bucket = _by_outcome(groups)

    assert len(bucket.get("collapse", [])) == 1
    grp = bucket["collapse"][0]
    assert {v.source_format for v in grp.values} == {"hl7_oru", "fhir_observation"}
    # HL7 ORU outranks FHIR Observation per source-trust order.
    assert grp.source_trust_ordered[0] == "hl7_oru"


def test_persisted_observation_disagrees_with_staged_emits_softwarn() -> None:
    """Persisted FHIR Obs (140) + newly-staged HL7 (142) same date → soft-warn."""
    persisted = [
        _row(source_format="fhir_observation", source_id="obs-existing", value="140"),
    ]
    staged = [
        _row(source_format="hl7_oru", source_id="hl7-new", value="142"),
    ]

    groups = detect_cross_source_conflicts(staged, persisted)
    bucket = _by_outcome(groups)

    assert "collapse" not in bucket
    assert len(bucket.get("conflict_soft_warn", [])) == 1


def test_no_inputs_returns_empty() -> None:
    """No staged + no persisted → no groups (common case must be cheap)."""
    assert detect_cross_source_conflicts([], []) == []


def test_single_row_no_conflict() -> None:
    """Lone row with no peer cannot conflict — no group emitted."""
    rows = [_row(source_format="hl7_oru", source_id="hl7-1")]
    assert detect_cross_source_conflicts(rows, []) == []
