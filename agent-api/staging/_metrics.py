"""Prometheus instruments owned by the staging package (Phase 9 Slice 9.3).

Why these live here and not in ``agent/metrics.py``:

The importlinter contract ``staging-isolated`` forbids ``staging -> agent``.
Centralising every metric in ``agent/metrics`` would invert that boundary.
Instead, the staging package owns its own instruments; ``agent/metrics``
carries a marker comment pointing operators here so a single
``grep agent_staging_`` still surfaces the catalog entry.

The names + labels match the §5.5 metric table in ``W1_ARCHITECTURE.md``
(updated by Slice 9.10) and the structured log events emitted from
``staging/store.py`` and ``staging/router.py``.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# ── Endpoint + transition counters ────────────────────────────────────────────

agent_staging_transitions_total = Counter(
    "agent_staging_transitions_total",
    "Pending-extraction state-machine transitions, labelled by from/to/role",
    ["from", "to", "role"],
)

agent_staging_endpoint_total = Counter(
    "agent_staging_endpoint_total",
    "Pending-extraction endpoint invocations, labelled by endpoint and outcome",
    ["endpoint", "outcome"],
)

agent_staging_endpoint_duration_seconds = Histogram(
    "agent_staging_endpoint_duration_seconds",
    "Pending-extraction endpoint wall-clock seconds, labelled by endpoint",
    ["endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

agent_staging_writer_total = Counter(
    "agent_staging_writer_total",
    "Approved → written transitions, labelled by target_resource_type and outcome",
    ["target_resource_type", "outcome", "write_error"],
)

# ── Watchdog instruments ──────────────────────────────────────────────────────

agent_staging_watchdog_runs_total = Counter(
    "agent_staging_watchdog_runs_total",
    "Staging watchdog job runs, labelled by job and outcome",
    ["job", "outcome"],
)

agent_staging_watchdog_rows_total = Counter(
    "agent_staging_watchdog_rows_total",
    "Rows touched by the staging watchdog, labelled by job and action",
    ["job", "action"],
)

agent_staging_watchdog_duration_seconds = Histogram(
    "agent_staging_watchdog_duration_seconds",
    "Staging watchdog job wall-clock seconds, labelled by job",
    ["job"],
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

__all__ = [
    "agent_staging_transitions_total",
    "agent_staging_endpoint_total",
    "agent_staging_endpoint_duration_seconds",
    "agent_staging_writer_total",
    "agent_staging_watchdog_runs_total",
    "agent_staging_watchdog_rows_total",
    "agent_staging_watchdog_duration_seconds",
]
