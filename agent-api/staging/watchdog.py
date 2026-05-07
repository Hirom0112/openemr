"""Watchdog jobs for the staging subsystem (Phase 9 Slice 9.3).

Two jobs registered at FastAPI startup:

* ``stuck_approved_reaper`` — every 60s, pick up rows that have been in
  ``state='approved'`` for more than 5 minutes (the writer crashed mid-
  flight or the request died after approve but before mark_written /
  mark_failed). Re-dispatches ``observations.writer._perform_write``.
* ``stale_pending_notifier`` — every hour, scan rows in
  ``state='pending'`` older than 7 days. Emits one
  ``extraction_staged`` audit row with ``detail_json={"watchdog_age_days": N}``
  per stale row. Beyond 30 days the row is auto-rejected with
  ``rejected_reason='auto-rejected: stale >30d'``.

Implementation note (forced deviation): the original spec called for an
APScheduler ``AsyncIOScheduler`` but ``apscheduler`` is not on
``requirements.txt`` and adding it for two simple periodic tasks is
disproportionate. The watchdog runs as two long-lived
``asyncio.create_task`` loops with ``asyncio.sleep`` between iterations —
a smaller surface area, no new dependency, and the same observable
behaviour under load. The scheduler-vs-task swap is mechanical if a
later slice needs APScheduler features (cron expressions, persistence).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from audit import writer as audit_writer
from audit.models import AuditEvent

from staging._metrics import (
    agent_staging_watchdog_duration_seconds,
    agent_staging_watchdog_rows_total,
    agent_staging_watchdog_runs_total,
)
from staging import store as _store


_logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

REAPER_INTERVAL_SECONDS: float = 60.0
APPROVED_STUCK_TTL_SECONDS: int = 5 * 60  # 5 minutes

NOTIFIER_INTERVAL_SECONDS: float = 60.0 * 60.0  # 1 hour
PENDING_SOFT_ALERT_DAYS: int = 7
PENDING_AUTO_REJECT_DAYS: int = 30


# ── SQL ──────────────────────────────────────────────────────────────────────

_SELECT_STUCK_APPROVED_SQL = """
    SELECT id, document_reference_id, file_batch_id::text AS file_batch_id,
           patient_id, target_resource_type, target_resource_id, state,
           payload, write_error, retry_count
      FROM copilot_pending_extractions
     WHERE state = 'approved'
       AND decided_at < NOW() - ($1 || ' seconds')::interval
     ORDER BY decided_at ASC
     LIMIT 50
"""


_SELECT_STALE_PENDING_SQL = """
    SELECT id, patient_id, staged_at,
           EXTRACT(EPOCH FROM (NOW() - staged_at)) / 86400.0 AS age_days
      FROM copilot_pending_extractions
     WHERE state = 'pending'
       AND staged_at < NOW() - ($1 || ' days')::interval
     ORDER BY staged_at ASC
     LIMIT 200
"""


_AUTO_REJECT_SQL = """
    UPDATE copilot_pending_extractions
       SET state = 'rejected',
           decided_at = NOW(),
           decided_by = 'system:watchdog',
           write_error = $2
     WHERE id = $1
       AND state = 'pending'
    RETURNING id
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _emit_audit(
    event_type: str,
    *,
    outcome: str,
    detail_json: dict[str, Any],
    patient_id: str | None = None,
) -> None:
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type=event_type,
                provider_id="system:watchdog",
                patient_id=patient_id,
                outcome=outcome,
                detail_json=detail_json,
            )
        )
    except Exception:  # pragma: no cover — audit must never break a task
        _logger.warning("watchdog_audit_emit_failed", extra={"event_type": event_type})


def _row_to_dict(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in row.keys():
        out[k] = row[k]
    return out


# ── Job 1: stuck-approved reaper ─────────────────────────────────────────────


async def run_stuck_approved_reaper_once() -> dict[str, int]:
    """One pass of the stuck-approved reaper. Exported for tests."""
    from observations import writer as _obs_writer

    pool = await audit_writer.get_pool()
    if pool is None:
        agent_staging_watchdog_runs_total.labels(
            job="stuck_approved_reaper", outcome="pool_unavailable"
        ).inc()
        return {"picked_up": 0, "written": 0, "failed": 0}

    t0 = time.perf_counter()
    counts = {"picked_up": 0, "written": 0, "failed": 0}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _SELECT_STUCK_APPROVED_SQL, str(APPROVED_STUCK_TTL_SECONDS)
            )
        for row in rows:
            counts["picked_up"] += 1
            agent_staging_watchdog_rows_total.labels(
                job="stuck_approved_reaper", action="picked_up"
            ).inc()
            row_dict = _row_to_dict(row)
            outcome, write_error = await _obs_writer._perform_write(row_dict)
            try:
                if outcome == "written":
                    await _store.mark_written(int(row_dict["id"]))
                    counts["written"] += 1
                    agent_staging_watchdog_rows_total.labels(
                        job="stuck_approved_reaper", action="written"
                    ).inc()
                else:
                    err = f"watchdog_recovered_orphan:{write_error or 'unknown'}"
                    await _store.mark_failed(int(row_dict["id"]), err)
                    counts["failed"] += 1
                    agent_staging_watchdog_rows_total.labels(
                        job="stuck_approved_reaper", action="failed"
                    ).inc()
            except _store.StagingError as exc:
                _logger.warning(
                    "watchdog_reaper_mark_skipped",
                    extra={
                        "pending_id": int(row_dict["id"]),
                        "code": exc.code,
                    },
                )
        agent_staging_watchdog_runs_total.labels(
            job="stuck_approved_reaper", outcome="success"
        ).inc()
    except Exception as exc:
        agent_staging_watchdog_runs_total.labels(
            job="stuck_approved_reaper", outcome="error"
        ).inc()
        _logger.warning(
            "watchdog_reaper_failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )
    finally:
        agent_staging_watchdog_duration_seconds.labels(
            job="stuck_approved_reaper"
        ).observe(max(0.0, time.perf_counter() - t0))
    return counts


# ── Job 2: stale-pending notifier + auto-rejecter ────────────────────────────


async def run_stale_pending_notifier_once() -> dict[str, int]:
    """One pass of the stale-pending notifier. Exported for tests."""
    pool = await audit_writer.get_pool()
    if pool is None:
        agent_staging_watchdog_runs_total.labels(
            job="stale_pending_notifier", outcome="pool_unavailable"
        ).inc()
        return {"alerted": 0, "auto_rejected": 0}

    t0 = time.perf_counter()
    counts = {"alerted": 0, "auto_rejected": 0}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _SELECT_STALE_PENDING_SQL, str(PENDING_SOFT_ALERT_DAYS)
            )
            for row in rows:
                row_dict = _row_to_dict(row)
                age_days = float(row_dict.get("age_days") or 0.0)
                pending_id = int(row_dict["id"])
                patient_id = (
                    str(row_dict["patient_id"])
                    if row_dict.get("patient_id") is not None
                    else None
                )

                if age_days >= PENDING_AUTO_REJECT_DAYS:
                    rejected = await conn.fetchrow(
                        _AUTO_REJECT_SQL,
                        pending_id,
                        f"auto-rejected: stale >{PENDING_AUTO_REJECT_DAYS}d",
                    )
                    if rejected is not None:
                        counts["auto_rejected"] += 1
                        agent_staging_watchdog_rows_total.labels(
                            job="stale_pending_notifier", action="auto_rejected"
                        ).inc()
                        await _emit_audit(
                            "extraction_rejected",
                            outcome="success",
                            patient_id=patient_id,
                            detail_json={
                                "pending_id": pending_id,
                                "auto": True,
                                "watchdog_age_days": int(age_days),
                            },
                        )
                else:
                    counts["alerted"] += 1
                    agent_staging_watchdog_rows_total.labels(
                        job="stale_pending_notifier", action="alerted"
                    ).inc()
                    await _emit_audit(
                        "extraction_staged",
                        outcome="warning",
                        patient_id=patient_id,
                        detail_json={
                            "pending_id": pending_id,
                            "watchdog_age_days": int(age_days),
                            "stale": True,
                        },
                    )
        agent_staging_watchdog_runs_total.labels(
            job="stale_pending_notifier", outcome="success"
        ).inc()
    except Exception as exc:
        agent_staging_watchdog_runs_total.labels(
            job="stale_pending_notifier", outcome="error"
        ).inc()
        _logger.warning(
            "watchdog_notifier_failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )
    finally:
        agent_staging_watchdog_duration_seconds.labels(
            job="stale_pending_notifier"
        ).observe(max(0.0, time.perf_counter() - t0))
    return counts


# ── Scheduler ────────────────────────────────────────────────────────────────


_tasks: list[asyncio.Task[Any]] = []
_stop_event: asyncio.Event | None = None


async def _loop(name: str, interval: float, fn: Any) -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await fn()
        except Exception as exc:  # pragma: no cover — defensive
            _logger.warning(
                "watchdog_loop_iteration_failed",
                extra={"job": name, "error_type": type(exc).__name__},
            )
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


def start_watchdog() -> None:
    """Spawn the two background loops. Idempotent."""
    global _stop_event
    if _tasks:
        return
    _stop_event = asyncio.Event()
    _tasks.append(
        asyncio.create_task(
            _loop(
                "stuck_approved_reaper",
                REAPER_INTERVAL_SECONDS,
                run_stuck_approved_reaper_once,
            ),
            name="staging-stuck-approved-reaper",
        )
    )
    _tasks.append(
        asyncio.create_task(
            _loop(
                "stale_pending_notifier",
                NOTIFIER_INTERVAL_SECONDS,
                run_stale_pending_notifier_once,
            ),
            name="staging-stale-pending-notifier",
        )
    )
    _logger.info("staging_watchdog_started", extra={"n_jobs": len(_tasks)})


async def stop_watchdog() -> None:
    """Signal the loops to exit and await their completion."""
    global _stop_event
    if _stop_event is not None:
        _stop_event.set()
    for task in list(_tasks):
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception:  # pragma: no cover — best-effort
            pass
    _tasks.clear()
    _stop_event = None


__all__ = [
    "APPROVED_STUCK_TTL_SECONDS",
    "NOTIFIER_INTERVAL_SECONDS",
    "PENDING_AUTO_REJECT_DAYS",
    "PENDING_SOFT_ALERT_DAYS",
    "REAPER_INTERVAL_SECONDS",
    "run_stale_pending_notifier_once",
    "run_stuck_approved_reaper_once",
    "start_watchdog",
    "stop_watchdog",
]
