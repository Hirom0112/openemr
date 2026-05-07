"""Pending-write subsystem (``copilot_pending_extractions``) — Phase 9 Slice 9.3.

Public surface
--------------

* :mod:`staging.store`     — asyncpg CRUD against the pending-extractions table.
* :mod:`staging.router`    — FastAPI router mounting the 5 endpoints.
* :mod:`staging.watchdog`  — stuck-approved reaper + stale-pending notifier.
* :mod:`staging.types`     — Pydantic request/response models.
* :mod:`staging.migrations`— runtime schema verification helper.
* :mod:`staging._metrics`  — Prometheus instruments (kept here per the
  ``staging-isolated`` importlinter contract; mirrors Slice 9.4's pattern
  for ``parsers/hl7/_metrics.py``).
"""

from staging.store import (  # noqa: F401
    StagingError,
    approve,
    get_pending,
    list_pending,
    mark_failed,
    mark_written,
    reject,
    retry,
    stage_pending,
)

__all__ = [
    "StagingError",
    "approve",
    "get_pending",
    "list_pending",
    "mark_failed",
    "mark_written",
    "reject",
    "retry",
    "stage_pending",
]
