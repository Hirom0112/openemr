"""Postgres writer for the PHI audit log (``copilot_audit_events``).

Design contract
---------------
* Single asyncpg pool, lazy-initialised on first ``emit()`` call.
* ``emit()`` is fire-and-forget — it CATCHES every exception internally
  and logs a single warning. An audit-pipeline failure must NEVER break
  the request path.
* When ``settings.audit_db_url`` is empty (the default), ``emit()`` is a
  hard no-op: no pool init, no DB call, no warning.
* ``detail_json`` is sanity-checked against a small block-list and a 2 KB
  string-encoded ceiling. Events that fail the check are logged and
  dropped — never written, never raised.
* ``record_destruction()`` writes one immutable row to
  ``copilot_audit_destructions`` and returns its ``(id, ts)`` so the API
  caller can echo the receipt. Caller is expected to surface DB failures
  to the operator (not silently swallow them).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import Any

from prometheus_client import Counter

from audit.models import AuditEvent
from config import settings

_logger = logging.getLogger(__name__)


# Counters for cross-service correlation. Distinct from the existing
# ``agent_audit_events_total`` (which counts MySQL/log-table emissions in
# audit.openemr_log) so dashboards can graph the two pipelines side by
# side without label-cardinality collisions.
copilot_audit_writes_total = Counter(
    "copilot_audit_writes_total",
    "PHI audit rows written to copilot_audit_events, by outcome.",
    ["outcome"],
)

copilot_audit_drops_total = Counter(
    "copilot_audit_drops_total",
    "PHI audit events dropped before write, by reason.",
    ["reason"],
)


# Words that strongly suggest free clinical text snuck into ``detail_json``.
# The list is intentionally small + crude: it is a tripwire, not a content
# filter. Any hit produces a dropped event + a WARNING so the offending
# call site can be fixed at source.
_DETAIL_BLOCKLIST: frozenset[str] = frozenset(
    {
        "vancomycin",
        "patient",
        "diagnosis",
        "complaint",
        "narrative",
        "note",
        "symptom",
        "prescription",
    }
)

_DETAIL_MAX_BYTES = 2048


# ── Pool plumbing ────────────────────────────────────────────────────────────

_pool: Any = None
_pool_lock: asyncio.Lock | None = None


def _get_pool_lock() -> asyncio.Lock:
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def _get_pool() -> Any:
    """Return the shared asyncpg pool, creating it on first use.

    Returns ``None`` if asyncpg is missing or the pool cannot be created.
    Callers MUST treat ``None`` as "skip the write".
    """
    global _pool
    if _pool is not None:
        return _pool
    if not settings.audit_db_url:
        return None

    async with _get_pool_lock():
        if _pool is not None:
            return _pool
        try:
            import asyncpg  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover — env-dependent
            _logger.warning(
                "audit_pool_unavailable",
                extra={"reason": "asyncpg_import_failed", "error": str(exc)},
            )
            return None
        try:
            _pool = await asyncpg.create_pool(
                dsn=settings.audit_db_url,
                min_size=1,
                max_size=4,
                command_timeout=5.0,
            )
        except Exception as exc:
            _logger.warning("audit_pool_create_failed", extra={"error": str(exc)})
            return None
    return _pool


async def get_pool() -> Any:
    """Public re-export of the shared asyncpg pool primitive.

    Sibling modules (e.g. ``documents.store``) reuse this single pool
    instead of spinning up a parallel one.  Returns ``None`` when
    asyncpg is unavailable or no DSN is configured.
    """
    return await _get_pool()


async def close_pool() -> None:
    """Close the writer pool (used at app shutdown)."""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except Exception:
        pass
    _pool = None


# ── Validation ───────────────────────────────────────────────────────────────


def _validate_detail(detail: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(ok, reject_reason)`` for a candidate ``detail_json``.

    Rejects when:
      * the JSON-encoded form exceeds ``_DETAIL_MAX_BYTES``, or
      * any key OR string value contains a phrase from ``_DETAIL_BLOCKLIST``
        (case-insensitive).

    Designed as a tripwire against accidental PHI leakage, not a real
    content filter — the contract is "structured codes only".
    """
    if not detail:
        return True, None
    try:
        encoded = json.dumps(detail, default=str, ensure_ascii=False)
    except Exception:
        return False, "detail_unencodable"
    if len(encoded.encode("utf-8")) > _DETAIL_MAX_BYTES:
        return False, "detail_too_large"
    lowered = encoded.lower()
    for word in _DETAIL_BLOCKLIST:
        if word in lowered:
            return False, f"detail_blocklist:{word}"
    return True, None


_INSERT_SQL = """
    INSERT INTO copilot_audit_events
        (event_type, request_id, session_id, provider_id, patient_id,
         tool_name, method, path, status_code, duration_ms, outcome,
         detail_json)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
"""


# ── Public API ───────────────────────────────────────────────────────────────


async def emit(event: AuditEvent) -> None:
    """Fire-and-forget audit insert. NEVER raises.

    No-op when ``settings.audit_db_url`` is empty (the default for tests +
    most local-dev workflows).
    """
    if not settings.audit_db_url:
        return

    try:
        ok, reason = _validate_detail(event.detail_json or {})
        if not ok:
            copilot_audit_drops_total.labels(reason=reason or "validation_failed").inc()
            _logger.warning(
                "audit_event_dropped",
                extra={
                    "reason": reason,
                    "event_type": event.event_type,
                    "tool_name": event.tool_name,
                },
            )
            return

        pool = await _get_pool()
        if pool is None:
            copilot_audit_drops_total.labels(reason="pool_unavailable").inc()
            return

        detail_text = (
            json.dumps(event.detail_json, default=str, ensure_ascii=False)
            if event.detail_json
            else None
        )
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                event.event_type,
                event.request_id,
                event.session_id,
                event.provider_id,
                event.patient_id,
                event.tool_name,
                event.method,
                event.path,
                event.status_code,
                event.duration_ms,
                event.outcome,
                detail_text,
            )
        copilot_audit_writes_total.labels(outcome=event.outcome or "n/a").inc()
    except Exception as exc:
        try:
            copilot_audit_drops_total.labels(reason="write_failed").inc()
        except Exception:
            pass
        _logger.warning(
            "audit_write_failed",
            extra={
                "event_type": event.event_type,
                "tool_name": event.tool_name,
                "error": str(exc),
            },
        )


async def record_destruction(
    *,
    requested_by: str,
    request_id: str | None,
    target_session: str | None,
    target_patient: str | None,
    target_window_start: _dt.datetime | None,
    target_window_end: _dt.datetime | None,
    rows_affected: int,
    reason: str,
) -> tuple[int, _dt.datetime]:
    """Insert one row into ``copilot_audit_destructions``; return ``(id, ts)``.

    Unlike :func:`emit` this DOES raise on DB failure — destruction records
    are compliance receipts and the caller (HTTP route) needs to surface
    failure to the operator instead of silently dropping it.
    """
    if not settings.audit_db_url:
        raise RuntimeError("audit_db_url not configured — destruction record cannot be persisted")

    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("audit pool unavailable")

    # asyncpg accepts an asyncpg.Range for tstzrange; either bound may be NULL.
    target_window: Any
    if target_window_start is None and target_window_end is None:
        target_window = None
    else:
        try:
            from asyncpg import Range  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — guarded by pool init
            raise RuntimeError("asyncpg not installed") from exc
        target_window = Range(
            lower=target_window_start,
            upper=target_window_end,
            lower_inc=True,
            upper_inc=False,
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO copilot_audit_destructions
                (requested_by, request_id, target_session, target_patient,
                 target_window, rows_affected, reason)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, ts
            """,
            requested_by,
            request_id,
            target_session,
            target_patient,
            target_window,
            rows_affected,
            reason,
        )
    return int(row["id"]), row["ts"]
