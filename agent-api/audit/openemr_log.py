"""HIPAA-style audit emission for agent tool calls.

Why this exists
---------------
Every tool call the agent makes against patient data needs an audit trail
(who, what, when, outcome, duration).  OpenEMR has a MySQL ``log`` table
designed exactly for this and is the system-of-record audit surface that
compliance reviewers and the OpenEMR audit UI both consult.

Implementation
--------------
:func:`emit_audit_event` performs DUAL emission:

1. A structured log line on the ``agent.audit`` logger (consumed by Loki
   / Datadog / etc — useful for cross-service correlation).
2. An INSERT into the OpenEMR ``log`` table (the system of record for
   HIPAA audit).

Both run on every call.  The DB write happens asynchronously through a
lazily-initialised ``aiomysql.Pool``: if the call site is inside an
asyncio loop the write is scheduled via :func:`asyncio.create_task`
and never blocks the dispatcher.  If no loop is running (e.g. tests
calling ``emit_audit_event`` synchronously) the DB write is skipped —
the structured logger still fires.

Failure handling
----------------
Audit emission must NEVER cause a tool call to fail.  Both the logger
emit and the DB write are wrapped; failures are logged at WARNING and
counted on dedicated Prometheus counters
(``agent_audit_events_total`` / ``agent_audit_db_write_failures_total``).
The dispatcher continues regardless.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from prometheus_client import Counter

from config import settings

# Dedicated logger so audit events can be filtered separately from the
# operational ``agent.tool`` log channel.
_audit_logger = logging.getLogger("agent.audit")

# Operational logger — for audit-emission failures only.
_logger = logging.getLogger(__name__)

AuditOutcome = Literal["ok", "error", "blocked"]

agent_audit_events_total = Counter(
    "agent_audit_events_total",
    "Total audit events emitted by the dispatcher, by outcome.",
    ["outcome"],
)

agent_audit_db_write_failures_total = Counter(
    "agent_audit_db_write_failures_total",
    "Total failures writing audit rows to the OpenEMR `log` table.",
)

# Lazily-initialised connection pool.  Module import MUST NOT touch the
# database (test imports would explode); the pool is created on the first
# successful write attempt inside an active event loop.
_pool: Any = None
_pool_lock: asyncio.Lock | None = None


def _get_pool_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def _get_pool() -> Any:
    """Return the shared aiomysql pool, creating it on first use.

    Returns ``None`` if aiomysql is not installed or the pool cannot
    be created — callers must treat ``None`` as "skip the DB write".
    """
    global _pool
    if _pool is not None:
        return _pool

    async with _get_pool_lock():
        if _pool is not None:
            return _pool
        try:
            import aiomysql  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover — env-dependent
            _logger.warning(
                "audit_db_pool_unavailable",
                extra={"reason": "aiomysql_import_failed", "error": str(exc)},
            )
            return None
        try:
            _pool = await aiomysql.create_pool(
                host=settings.openemr_db_host,
                port=settings.openemr_db_port,
                user=settings.openemr_db_user,
                password=settings.openemr_db_password,
                db=settings.openemr_db_name,
                minsize=1,
                maxsize=5,
                autocommit=True,
            )
        except Exception as exc:
            _logger.warning(
                "audit_db_pool_create_failed",
                extra={"error": str(exc)},
            )
            return None
        return _pool


# OpenEMR `log` table INSERT — column ordering matches the schema in
# sql/database.sql.  ``date`` is filled by NOW() server-side.
_INSERT_SQL = (
    "INSERT INTO log "
    "(date, event, category, user, groupname, success, comments, "
    "crt_user, log_from, patient_id) "
    "VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def _success_flag(outcome: AuditOutcome | str | None) -> int:
    return 1 if outcome == "ok" else 0


def _build_row(
    *,
    session_id: str,
    provider_id: str | None,
    tool_name: str,
    outcome: AuditOutcome,
    duration_ms: int,
    patient_id: str | None,
    failure_class: str | None,
) -> tuple[Any, ...]:
    """Build the positional parameters for the INSERT statement.

    Mapped columns (10 of the 13 in OpenEMR's ``log`` table — the rest
    default at the DB layer): event, category, user, groupname,
    success, comments, crt_user, log_from, patient_id.
    """
    user_value = provider_id if provider_id is not None else "unknown"
    comments = json.dumps(
        {
            "session_id": session_id,
            "duration_ms": duration_ms,
            "failure_class": failure_class,
        },
        separators=(",", ":"),
    )
    # OpenEMR stores patient_id as INT; pass through as-is and let the
    # driver coerce.  None → SQL NULL.
    pid: Any = patient_id if patient_id is not None else None
    return (
        tool_name,        # event
        "copilot",        # category
        user_value,       # user
        "Default",        # groupname
        _success_flag(outcome),  # success
        comments,         # comments
        user_value,       # crt_user
        "copilot",        # log_from
        pid,              # patient_id
    )


async def write_audit_row(
    *,
    session_id: str,
    provider_id: str | None,
    tool_name: str,
    outcome: AuditOutcome,
    duration_ms: int,
    patient_id: str | None = None,
    failure_class: str | None = None,
) -> None:
    """Insert one audit row into OpenEMR's ``log`` table.

    Never raises; on any failure logs WARNING and increments
    ``agent_audit_db_write_failures_total``.
    """
    try:
        pool = await _get_pool()
        if pool is None:
            agent_audit_db_write_failures_total.inc()
            return
        params = _build_row(
            session_id=session_id,
            provider_id=provider_id,
            tool_name=tool_name,
            outcome=outcome,
            duration_ms=duration_ms,
            patient_id=patient_id,
            failure_class=failure_class,
        )
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT_SQL, params)
    except Exception as exc:
        try:
            agent_audit_db_write_failures_total.inc()
        except Exception:
            pass
        _logger.warning(
            "audit_db_write_failed",
            extra={
                "session_id": session_id,
                "tool": tool_name,
                "error": str(exc),
            },
        )


def emit_audit_event(
    *,
    session_id: str,
    provider_id: str | None,
    tool_name: str,
    outcome: AuditOutcome,
    duration_ms: int,
    patient_id: str | None = None,
    failure_class: str | None = None,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one audit event to BOTH the structured logger and the
    OpenEMR ``log`` table.  Safe to call from any tool-call path.

    Never raises — any internal failure is logged at WARNING on the
    operational logger and the audit metric is incremented under the
    ``error`` outcome so observability can detect a broken pipeline.
    The DB write is fire-and-forget via :func:`asyncio.create_task`
    when an event loop is running; it is skipped if no loop is active.
    """
    try:
        payload: dict[str, Any] = {
            "event": "audit.tool_call",
            "session_id": session_id,
            "provider_id": provider_id,
            "tool": tool_name,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        if patient_id is not None:
            payload["patient_id"] = patient_id
        if failure_class is not None:
            payload["failure_class"] = failure_class
        if request_id is not None:
            payload["request_id"] = request_id
        if extra:
            payload["extra"] = extra

        _audit_logger.info("audit.tool_call", extra=payload)
        agent_audit_events_total.labels(outcome=outcome).inc()
    except Exception as exc:  # pragma: no cover — defensive
        try:
            agent_audit_events_total.labels(outcome="error").inc()
        except Exception:
            pass
        _logger.warning(
            "audit_emit_failed",
            extra={
                "session_id": session_id,
                "tool": tool_name,
                "error": str(exc),
            },
        )

    # Schedule the DB write on the running loop.  In sync contexts (no
    # loop) we silently skip — production call sites are inside the
    # async dispatcher, so a loop is always present.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(
            write_audit_row(
                session_id=session_id,
                provider_id=provider_id,
                tool_name=tool_name,
                outcome=outcome,
                duration_ms=duration_ms,
                patient_id=patient_id,
                failure_class=failure_class,
            )
        )
    except Exception as exc:  # pragma: no cover — defensive
        try:
            agent_audit_db_write_failures_total.inc()
        except Exception:
            pass
        _logger.warning(
            "audit_db_schedule_failed",
            extra={
                "session_id": session_id,
                "tool": tool_name,
                "error": str(exc),
            },
        )
