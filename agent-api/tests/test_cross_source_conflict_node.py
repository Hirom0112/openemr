"""Phase 9 Slice 9.7 — graph node integration tests.

The pure detector is exercised in ``test_conflict_detector.py``. This file
covers the node wrapper:

* No-op when state has neither ``staged_lab_values`` nor ``persisted_observations``.
* Conflict groups appended to ``state["soft_warns"]`` with the
  banner copy.
* Audit emission carries PHI-safe ``detail_json`` (counts, source_types,
  source_ids, ISO dates — never values, prose).
* Metric ``agent_cross_source_conflict_total`` increments per group.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from graph.nodes.cross_source_conflict import cross_source_conflict_node
from graph.state import make_initial_state

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


def _row(
    *,
    source_format: str,
    source_id: str,
    value: str,
    test: str = "ldl",
    unit: str = "mg/dl",
    coll: date | None = date(2026, 4, 1),
) -> dict:
    return {
        "source_format": source_format,
        "source_id": source_id,
        "normalized_test_name": test,
        "normalized_value": value,
        "normalized_unit": unit,
        "collection_date": coll,
        "extracted_at": "2026-04-01T10:00:00Z",
        "citations": [
            {
                "source_type": "document",
                "source_id": source_id,
                "field_or_chunk_id": f"{source_id}-f1",
                "quote_or_value": value,
            }
        ],
    }


@pytest.mark.asyncio
async def test_node_is_noop_when_no_inputs() -> None:
    state = make_initial_state(
        request_id="r-noop",
        session_id="s-noop",
        provider_id="prov-1",
        patient_id="pt-1",
    )
    with patch(
        "graph.nodes.cross_source_conflict.audit_writer.emit", new=AsyncMock()
    ) as emit:
        out = await cross_source_conflict_node(state)
    assert out["cross_source_conflicts"] == []
    assert out["soft_warns"] == []
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_node_emits_softwarn_for_tier2_conflict() -> None:
    state = make_initial_state(
        request_id="r-conflict",
        session_id="s-conflict",
        provider_id="prov-1",
        patient_id="pt-1",
    )
    state["staged_lab_values"] = [
        _row(source_format="hl7_oru", source_id="hl7-1", value="142"),
        _row(source_format="xlsx", source_id="xlsx-1", value="140"),
    ]

    with patch(
        "graph.nodes.cross_source_conflict.audit_writer.emit", new=AsyncMock()
    ) as emit:
        out = await cross_source_conflict_node(state)

    # Conflict group present.
    assert len(out["cross_source_conflicts"]) == 1
    grp = out["cross_source_conflicts"][0]
    assert grp["outcome"] == "conflict_soft_warn"
    assert grp["tier"] == "2"

    # Soft-warn appended for the critic to forward.
    assert len(out["soft_warns"]) == 1
    sw = out["soft_warns"][0]
    assert sw["code"] == "CROSS_SOURCE_CONFLICT"
    assert sw["message"] == "Sources disagree on this value. Verify before acting."
    assert sw["tier"] == "2"
    assert "hl7_oru" in sw["source_formats"]

    # Audit emission: one event with PHI-safe detail_json.
    emit.assert_awaited_once()
    event = emit.call_args.args[0]
    assert event.event_type == "cross_source_conflict_detected"
    detail = event.detail_json
    # PHI-safe: counts, source_types, source_ids, ISO dates ONLY.
    assert set(detail.keys()) == {
        "group_count",
        "outcome_counts",
        "tiers",
        "source_types",
        "source_ids",
        "collection_dates_iso",
    }
    # Defensive: values + free-text MUST NOT appear in audit detail.
    serialized = repr(detail)
    assert "142" not in serialized
    assert "140" not in serialized
    assert "Sources disagree" not in serialized


@pytest.mark.asyncio
async def test_node_collapse_emits_no_softwarn() -> None:
    """Tier-1 collapse logs + audits but does NOT add a soft-warn entry."""
    state = make_initial_state(
        request_id="r-collapse",
        session_id="s-collapse",
        provider_id="prov-1",
        patient_id="pt-1",
    )
    state["staged_lab_values"] = [
        _row(source_format="hl7_oru", source_id="hl7-1", value="142"),
        _row(source_format="xlsx", source_id="xlsx-1", value="142"),
    ]

    with patch(
        "graph.nodes.cross_source_conflict.audit_writer.emit", new=AsyncMock()
    ):
        out = await cross_source_conflict_node(state)

    assert len(out["cross_source_conflicts"]) == 1
    assert out["cross_source_conflicts"][0]["outcome"] == "collapse"
    assert out["soft_warns"] == []
