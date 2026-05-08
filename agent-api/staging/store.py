"""asyncpg-backed CRUD against ``copilot_pending_extractions`` (Slice 9.3).

State machine (from schema.sql §9.1):

    pending → approved → written      (happy path)
    pending → rejected                (clinician declined)
    approved → failed                 (writer error)
    failed   → approved               (explicit retry; only legal terminal
                                       → non-terminal transition)

Terminal states (rejected, written, failed) are otherwise immutable —
enforced server-side by ``copilot_pending_extractions_block_revival_trg``.
The store mirrors that taxonomy in Python so callers see a 409 instead
of a 5xx when they try an illegal transition.

Design contract
---------------
* Single shared asyncpg pool via ``audit.writer.get_pool()`` (the same
  pool ``demographics/quarantine.py`` uses).
* Every state mutation emits one ``copilot_audit_events`` row via
  ``audit.writer.emit`` with PHI-safe ``detail_json`` (codes/counts only —
  NEVER values, prose, or raw clinical fields). See W1_ARCHITECTURE.md §9.2.
* Functions raise :class:`StagingError` with a stable ``code`` string the
  HTTP layer maps to a 4xx response (404 not_found, 409 conflict).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from audit import writer as audit_writer
from audit.models import AuditEvent

_logger = logging.getLogger(__name__)


PendingState = Literal["pending", "approved", "rejected", "written", "failed"]
TargetResourceType = Literal["Observation", "Task", "AllergyIntolerance"]

MAX_RETRIES: int = 3
REJECT_REASON_MAX_CHARS: int = 256


class StagingError(Exception):
    """Raised when a staging transition violates the state machine."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── SQL ──────────────────────────────────────────────────────────────────────

_INSERT_SQL = """
    INSERT INTO copilot_pending_extractions
        (document_reference_id, file_batch_id, patient_id,
         target_resource_type, target_resource_id, payload)
    VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb)
    ON CONFLICT (document_reference_id, target_resource_id)
        WHERE state = 'pending'
        DO UPDATE SET
            payload = EXCLUDED.payload,
            file_batch_id = EXCLUDED.file_batch_id,
            patient_id = EXCLUDED.patient_id,
            target_resource_type = EXCLUDED.target_resource_type
    RETURNING id, state
"""


_SELECT_ONE_SQL = """
    SELECT id, document_reference_id, file_batch_id::text AS file_batch_id,
           patient_id, target_resource_type, target_resource_id, state,
           payload, write_error, retry_count,
           staged_at, decided_at, decided_by, written_at
      FROM copilot_pending_extractions
     WHERE id = $1
"""


_LIST_SQL_BASE = """
    SELECT id, document_reference_id, file_batch_id::text AS file_batch_id,
           patient_id, target_resource_type, target_resource_id, state,
           payload, write_error, retry_count,
           staged_at, decided_at, decided_by, written_at
      FROM copilot_pending_extractions
     WHERE patient_id = $1
"""


_APPROVE_SQL = """
    UPDATE copilot_pending_extractions
       SET state = 'approved',
           decided_at = NOW(),
           decided_by = $2
     WHERE id = $1
       AND state = 'pending'
    RETURNING id, target_resource_id, state
"""


_REJECT_SQL = """
    UPDATE copilot_pending_extractions
       SET state = 'rejected',
           decided_at = NOW(),
           decided_by = $2,
           write_error = $3
     WHERE id = $1
       AND state = 'pending'
    RETURNING id, state
"""


_MARK_WRITTEN_SQL = """
    UPDATE copilot_pending_extractions
       SET state = 'written',
           written_at = NOW(),
           write_error = NULL
     WHERE id = $1
       AND state = 'approved'
    RETURNING id, state
"""


_MARK_FAILED_SQL = """
    UPDATE copilot_pending_extractions
       SET state = 'failed',
           write_error = $2
     WHERE id = $1
       AND state = 'approved'
    RETURNING id, state
"""


_RETRY_SQL = """
    UPDATE copilot_pending_extractions
       SET state = 'approved',
           retry_count = retry_count + 1,
           write_error = NULL
     WHERE id = $1
       AND state = 'failed'
       AND retry_count < $2
    RETURNING id, state, retry_count
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _coerce_jsonb(value: Any) -> str:
    if value is None:
        return "null"
    return json.dumps(value, default=str, ensure_ascii=False)


def _row_to_dict(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if hasattr(v, "isoformat"):
            try:
                out[k] = v.isoformat()
                continue
            except Exception:
                pass
        if k == "payload" and isinstance(v, str):
            try:
                out[k] = json.loads(v)
                continue
            except Exception:
                pass
        out[k] = v
    return out


async def _require_pool() -> Any:
    pool = await audit_writer.get_pool()
    if pool is None:
        raise StagingError(
            "pool_unavailable",
            "staging.store requires audit_db_url to be configured (no Postgres pool available)",
        )
    return pool


async def _emit_audit(
    *,
    event_type: str,
    request_id: str | None,
    provider_id: str | None,
    patient_id: str | None,
    outcome: str,
    detail_json: dict[str, Any],
) -> None:
    """Fire-and-forget audit emission. NEVER raises (mirrors writer.emit)."""
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type=event_type,
                request_id=request_id,
                provider_id=provider_id,
                patient_id=patient_id,
                outcome=outcome,
                detail_json=detail_json,
            )
        )
    except Exception:  # pragma: no cover — audit must never break the path
        _logger.warning("staging_audit_emit_failed", extra={"event_type": event_type})


# ── Public API ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApproveResult:
    pending_id: int
    target_resource_id: str
    state: PendingState


async def stage_pending(
    *,
    document_reference_id: str,
    file_batch_id: str,
    patient_id: str,
    source_format: str,
    target_resource_type: TargetResourceType,
    target_resource_id: str,
    payload: dict[str, Any],
    locator: str | None = None,
    confidence: float | None = None,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> int:
    """Insert a pending row OR update-in-place if a pending row already exists.

    The partial unique constraint on
    ``(document_reference_id, target_resource_id) WHERE state='pending'``
    means a re-stage during the pending window updates the existing row.
    Once a row goes terminal (rejected / written / failed) a fresh stage
    inserts a NEW row — the partial index allows the same key pair to
    co-exist in distinct terminal states.

    Returns the ``id`` of the staged row (existing or new).
    """
    pool = await _require_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_SQL,
            document_reference_id,
            str(file_batch_id),
            str(patient_id),
            target_resource_type,
            target_resource_id,
            _coerce_jsonb(payload),
        )

    if row is None:
        # Should be unreachable — ON CONFLICT path returns the updated row;
        # fresh INSERT also RETURNs. A None implies a terminal-state
        # collision the partial unique index didn't catch (e.g. the trigger
        # rejected an UPDATE we didn't intend). Surface as conflict.
        raise StagingError(
            "conflict",
            f"stage_pending: no row returned for target_resource_id={target_resource_id}",
        )

    pending_id = int(row["id"])
    _logger.info(
        "extraction_staged",
        extra={
            "request_id": request_id,
            "pending_id": pending_id,
            "document_reference_id": document_reference_id,
            "target_resource_type": target_resource_type,
            "source_format": source_format,
            "locator": locator,
            "confidence": confidence,
        },
    )
    await _emit_audit(
        event_type="extraction_staged",
        request_id=request_id,
        provider_id=provider_id,
        patient_id=str(patient_id),
        outcome="success",
        detail_json={
            "pending_id": pending_id,
            "target_resource_type": target_resource_type,
            "source_format": source_format,
            "has_locator": locator is not None,
        },
    )
    return pending_id


async def get_pending(pending_id: int) -> dict[str, Any] | None:
    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT_ONE_SQL, pending_id)
    if row is None:
        return None
    return _row_to_dict(row)


async def list_pending(
    *,
    patient_id: str,
    state: str | None = None,
    file_batch_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    pool = await _require_pool()
    sql = _LIST_SQL_BASE
    args: list[Any] = [str(patient_id)]
    idx = 2
    if state is not None:
        sql += f" AND state = ${idx}"
        args.append(state)
        idx += 1
    if file_batch_id is not None:
        sql += f" AND file_batch_id = ${idx}::uuid"
        args.append(file_batch_id)
        idx += 1
    sql += f" ORDER BY staged_at DESC LIMIT ${idx}"
    args.append(int(limit))

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [_row_to_dict(r) for r in rows]


async def approve(
    pending_id: int,
    approver: str,
    *,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> ApproveResult:
    """Atomic ``pending → approved`` transition. 409 if not pending."""
    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_APPROVE_SQL, pending_id, str(approver))
    if row is None:
        existing = await get_pending(pending_id)
        if existing is None:
            raise StagingError("not_found", f"pending_id={pending_id} not found")
        raise StagingError(
            "conflict",
            f"pending_id={pending_id} not in state 'pending' (state={existing['state']})",
        )

    result = ApproveResult(
        pending_id=int(row["id"]),
        target_resource_id=str(row["target_resource_id"]),
        state="approved",
    )
    _logger.info(
        "extraction_approved",
        extra={
            "request_id": request_id,
            "pending_id": result.pending_id,
            "approver": approver,
        },
    )
    await _emit_audit(
        event_type="extraction_approved",
        request_id=request_id,
        provider_id=provider_id or approver,
        patient_id=None,
        outcome="success",
        detail_json={"pending_id": result.pending_id},
    )
    return result


async def reject(
    pending_id: int,
    approver: str,
    reason: str,
    *,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """Atomic ``pending → rejected``. 400 if reason exceeds the cap."""
    if not reason or not reason.strip():
        raise StagingError("invalid_reason", "reason must not be empty")
    if len(reason) > REJECT_REASON_MAX_CHARS:
        raise StagingError(
            "invalid_reason",
            f"reason exceeds {REJECT_REASON_MAX_CHARS} characters",
        )

    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_REJECT_SQL, pending_id, str(approver), reason)
    if row is None:
        existing = await get_pending(pending_id)
        if existing is None:
            raise StagingError("not_found", f"pending_id={pending_id} not found")
        raise StagingError(
            "conflict",
            f"pending_id={pending_id} not in state 'pending' (state={existing['state']})",
        )

    out = {"pending_id": int(row["id"]), "state": "rejected"}
    _logger.info(
        "extraction_rejected",
        extra={
            "request_id": request_id,
            "pending_id": out["pending_id"],
            "approver": approver,
            "reason_chars": len(reason),
        },
    )
    await _emit_audit(
        event_type="extraction_rejected",
        request_id=request_id,
        provider_id=provider_id or approver,
        patient_id=None,
        outcome="success",
        detail_json={
            "pending_id": out["pending_id"],
            "reason_chars": len(reason),
        },
    )
    return out


async def mark_written(
    pending_id: int,
    *,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> None:
    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_MARK_WRITTEN_SQL, pending_id)
    if row is None:
        existing = await get_pending(pending_id)
        if existing is None:
            raise StagingError("not_found", f"pending_id={pending_id} not found")
        raise StagingError(
            "conflict",
            f"pending_id={pending_id} not in state 'approved' (state={existing['state']})",
        )
    _logger.info(
        "extraction_written",
        extra={"request_id": request_id, "pending_id": pending_id},
    )
    await _emit_audit(
        event_type="extraction_written",
        request_id=request_id,
        provider_id=provider_id,
        patient_id=None,
        outcome="success",
        detail_json={"pending_id": pending_id},
    )


async def mark_failed(
    pending_id: int,
    write_error: str,
    *,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> None:
    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_MARK_FAILED_SQL, pending_id, write_error)
    if row is None:
        existing = await get_pending(pending_id)
        if existing is None:
            raise StagingError("not_found", f"pending_id={pending_id} not found")
        raise StagingError(
            "conflict",
            f"pending_id={pending_id} not in state 'approved' (state={existing['state']})",
        )
    _logger.warning(
        "extraction_write_failed",
        extra={
            "request_id": request_id,
            "pending_id": pending_id,
            "write_error": write_error,
        },
    )
    await _emit_audit(
        event_type="extraction_write_failed",
        request_id=request_id,
        provider_id=provider_id,
        patient_id=None,
        outcome="failure",
        detail_json={
            "pending_id": pending_id,
            "write_error": write_error,
        },
    )


async def retry(
    pending_id: int,
    *,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """Re-arm a ``failed`` row for another approve→write attempt.

    Caps at :data:`MAX_RETRIES` increments — beyond that, raises 409.
    """
    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_RETRY_SQL, pending_id, MAX_RETRIES)
    if row is None:
        existing = await get_pending(pending_id)
        if existing is None:
            raise StagingError("not_found", f"pending_id={pending_id} not found")
        if existing["state"] != "failed":
            raise StagingError(
                "conflict",
                f"pending_id={pending_id} not in state 'failed' (state={existing['state']})",
            )
        # state was 'failed' but UPDATE skipped — retry_count cap reached.
        raise StagingError(
            "conflict",
            f"pending_id={pending_id} retry_count cap ({MAX_RETRIES}) reached",
        )

    out = {
        "pending_id": int(row["id"]),
        "state": "approved",
        "retry_count": int(row["retry_count"]),
    }
    _logger.info(
        "extraction_retry",
        extra={
            "request_id": request_id,
            "pending_id": out["pending_id"],
            "retry_count": out["retry_count"],
        },
    )
    await _emit_audit(
        event_type="extraction_approved",
        request_id=request_id,
        provider_id=provider_id,
        patient_id=None,
        outcome="success",
        detail_json={
            "pending_id": out["pending_id"],
            "retry": True,
            "retry_count": out["retry_count"],
        },
    )
    return out


__all__ = [
    "ApproveResult",
    "MAX_RETRIES",
    "REJECT_REASON_MAX_CHARS",
    "PendingState",
    "StagingError",
    "TargetResourceType",
    "approve",
    "get_pending",
    "list_pending",
    "mark_failed",
    "mark_written",
    "reject",
    "retry",
    "stage_pending",
]
