"""Cross-source conflict graph node (Phase 9 Slice 9.7).

Runs **post-stage, pre-write** between :mod:`graph.nodes.structured` and
:mod:`graph.nodes.critic`. Reads staged-row metadata + already-persisted
Observation metadata from state, walks the pure detector in
:mod:`conflict.detector`, and:

* writes the full set of detected groups to ``state["cross_source_conflicts"]``
  (additive ``NotRequired`` key — see W2_ARCHITECTURE.md §5.10),
* appends one entry to ``state["soft_warns"]`` per ``conflict_soft_warn``
  group so the critic (which we do NOT modify) carries the banner forward
  unchanged,
* emits one ``cross_source_conflict_detected`` audit row per detection (with
  PHI-safe ``detail_json``: counts, source_types, source_ids, ISO dates —
  NEVER values, prose, free clinical text), and
* increments ``agent_cross_source_conflict_total{outcome,source_pair,tier}``
  per group.

The node is a no-op when the input lists are empty — the W2 graph runs
this in front of the critic on every structured-path request, so the
common case (single-source ingest, no persisted same-fact rows) MUST be
near-zero cost.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from audit import writer as audit_writer
from audit.models import AuditEvent

from conflict._metrics import agent_cross_source_conflict_total
from conflict.detector import detect_cross_source_conflicts
from conflict.ranking import source_pair_label
from conflict.schemas import CrossSourceConflict

from ..state import W2State

logger = logging.getLogger(__name__)


_SOFT_WARN_BANNER: str = (
    "Sources disagree on this value. Verify before acting."
)


def _state_rows(state: W2State, key: str) -> List[Dict[str, Any]]:
    raw = state.get(key)  # type: ignore[arg-type]
    if not raw:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


async def cross_source_conflict_node(state: W2State) -> dict[str, Any]:
    """Detect cross-source conflicts; surface as soft-warns + audit."""
    t0 = time.monotonic()

    # Staged rows expected to be populated by upstream wiring (Slice 9.3
    # post-stage hook) under ``state["staged_lab_values"]``. Already-
    # persisted Observations are read from ``state["persisted_observations"]``
    # (Slice 9.6 retrieval-vs-record harness). Both keys are optional and
    # additive — the node is a no-op when neither is set, keeping the
    # common single-source path cheap.
    staged = _state_rows(state, "staged_lab_values")
    persisted = _state_rows(state, "persisted_observations")

    conflicts: List[CrossSourceConflict] = []
    error: str | None = None
    if staged or persisted:
        try:
            conflicts = detect_cross_source_conflicts(staged, persisted)
        except Exception as exc:  # noqa: BLE001 — boundary; never break the graph
            error = type(exc).__name__
            logger.exception(
                "cross_source_conflict_pass_failed",
                extra={
                    "request_id": state.get("request_id"),
                    "error_type": error,
                },
            )
            conflicts = []

    # Append soft-warn entries to the critic's input list so the unchanged
    # critic node forwards the banner without code changes there.
    soft_warns: list[dict[str, Any]] = list(state.get("soft_warns") or [])
    for group in conflicts:
        if group.outcome == "conflict_soft_warn":
            soft_warns.append(
                {
                    "code": "CROSS_SOURCE_CONFLICT",
                    "message": _SOFT_WARN_BANNER,
                    "tier": group.tier,
                    "source_formats": list(group.source_trust_ordered),
                }
            )

    # Metrics — one observation per group.
    for group in conflicts:
        try:
            label = source_pair_label(v.source_format for v in group.values)
            agent_cross_source_conflict_total.labels(
                outcome=group.outcome,
                source_pair=label,
                tier=group.tier,
            ).inc()
        except Exception:  # pragma: no cover — metrics must never break the graph
            logger.warning(
                "cross_source_conflict_metric_failed",
                extra={"request_id": state.get("request_id")},
            )

    # Audit — one event per detection PASS (single row carrying counts and
    # source-format / source-id arrays). Per spec the detail_json is
    # PHI-safe: counts, source_types, source_ids, ISO dates only.
    if conflicts:
        try:
            counts: Dict[str, int] = {"collapse": 0, "conflict_soft_warn": 0, "date_missing": 0}
            source_types: set[str] = set()
            source_ids: set[str] = set()
            tiers: set[str] = set()
            iso_dates: set[str] = set()
            for g in conflicts:
                counts[g.outcome] = counts.get(g.outcome, 0) + 1
                tiers.add(g.tier)
                for v in g.values:
                    source_types.add(v.source_format)
                    if v.source_id:
                        source_ids.add(v.source_id)
                    if v.collection_date_iso:
                        iso_dates.add(v.collection_date_iso)
            await audit_writer.emit(
                AuditEvent(
                    event_type="cross_source_conflict_detected",
                    request_id=state.get("request_id"),
                    session_id=state.get("session_id"),
                    provider_id=state.get("provider_id"),
                    patient_id=state.get("patient_id"),
                    outcome="success" if error is None else "failure",
                    detail_json={
                        "group_count": len(conflicts),
                        "outcome_counts": counts,
                        "tiers": sorted(tiers),
                        "source_types": sorted(source_types),
                        "source_ids": sorted(source_ids),
                        "collection_dates_iso": sorted(iso_dates),
                    },
                )
            )
        except Exception:  # pragma: no cover
            logger.warning(
                "cross_source_conflict_audit_emit_failed",
                extra={"request_id": state.get("request_id")},
            )

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "cross_source_conflict_pass_complete",
        extra={
            "request_id": state.get("request_id"),
            "duration_ms": duration_ms,
            "group_count": len(conflicts),
            "staged_count": len(staged),
            "persisted_count": len(persisted),
            "error_type": error,
        },
    )

    return {
        "cross_source_conflicts": [g.model_dump(mode="json") for g in conflicts],
        "soft_warns": soft_warns,
    }


__all__ = ["cross_source_conflict_node"]
