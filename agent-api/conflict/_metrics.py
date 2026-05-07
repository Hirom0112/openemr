"""Prometheus instruments owned by the conflict package (Phase 9 Slice 9.7).

Why these live here and not in ``agent/metrics.py``:

The importlinter contract ``conflict-is-mostly-leaf`` forbids
``conflict -> agent``. Centralising every metric in ``agent/metrics`` would
invert that boundary. The conflict package owns its own instruments;
``agent/metrics`` carries a marker comment pointing operators here so a
single ``grep agent_cross_source_conflict_`` still surfaces the catalog
entry. Same pattern as ``parsers/hl7/_metrics`` and ``staging/_metrics``.

The metric name + labels match the §5.5 metric table in
``ARCHITECTURE.md`` (updated by Slice 9.10) and the structured log
event ``cross_source_conflict_pass_complete`` emitted from
``graph/nodes/cross_source_conflict.py``.
"""

from __future__ import annotations

from prometheus_client import Counter

# Outcome labels: ``collapse`` (tier-1 §7.5 dedup match, citations merged),
# ``conflict_soft_warn`` (tier-2 disagreement), ``date_missing`` (tier-2
# could not run because ``collection_date`` is absent on at least one side
# — defer rather than over-collapse).
#
# source_pair: stable lex-sorted "lhs|rhs" string of the two source-format
# tokens involved in the comparison (e.g. ``"hl7_oru|xlsx"``). When more
# than two sources participate the label uses ``"multi"``.
#
# tier: ``"1"`` (§7.5 collapse key) or ``"2"`` (wider conflict key).
agent_cross_source_conflict_total = Counter(
    "agent_cross_source_conflict_total",
    "Cross-source conflict pass outcomes by tier and participating sources",
    ["outcome", "source_pair", "tier"],
)

__all__ = [
    "agent_cross_source_conflict_total",
]
