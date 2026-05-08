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
TargetResourceType = Literal[
    "Observation",
    "Task",
    "AllergyIntolerance",
    # Phase-3 Documents-tab redesign: per-field intake-form rows (PDF/DOCX
    # allergies/meds/demographics/family-hx/chief-concern/code-status). No
    # FHIR writer; approval is informational. See ``observations.writer.
    # _perform_write`` for the short-circuit and ``audit/schema.sql`` for
    # the matching CHECK-constraint relaxation.
    "IntakeFormField",
]

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


def _walk_and_update_citations(
    obj: Any,
    field_or_chunk_id: str,
    new_bbox: list[float],
    new_page: int,
) -> tuple[Any, int, dict[str, Any] | None]:
    """Recursively walk a payload tree, find every citation whose
    ``field_or_chunk_id`` matches ``field_or_chunk_id``, replace its
    ``bbox`` + ``page`` in place, and return the updated tree + the
    number of citations updated + a copy of the previous citation
    (the FIRST match's pre-update value, for the audit log).

    The walker is deliberately permissive about payload shape — works
    against flat (medication / allergy / family_history) and nested
    (demographics with per-sub-field citations) layouts. Returns
    ``(payload, 0, None)`` when no match is found; the caller surfaces
    that as a 404.
    """
    matches = 0
    previous: dict[str, Any] | None = None

    def _walk(node: Any) -> Any:
        nonlocal matches, previous
        if isinstance(node, list):
            return [_walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "citations" and isinstance(v, list):
                new_list: list[Any] = []
                for c in v:
                    if (
                        isinstance(c, dict)
                        and isinstance(c.get("field_or_chunk_id"), str)
                        and c["field_or_chunk_id"] == field_or_chunk_id
                    ):
                        if previous is None:
                            previous = {
                                "bbox": c.get("bbox"),
                                "page": c.get("page"),
                            }
                        nc = dict(c)
                        nc["bbox"] = list(new_bbox)
                        nc["page"] = int(new_page)
                        # If the source had a polygon, drop it — a manual
                        # reshape invalidates the polygon's faithfulness.
                        if "polygon" in nc:
                            nc["polygon"] = None
                        new_list.append(nc)
                        matches += 1
                    else:
                        new_list.append(_walk(c))
                out[k] = new_list
            else:
                out[k] = _walk(v)
        return out

    updated = _walk(obj)
    return updated, matches, previous


_UPDATE_PAYLOAD_PENDING_ONLY_SQL = """
    UPDATE copilot_pending_extractions
       SET payload = $2::jsonb
     WHERE id = $1
       AND state = 'pending'
    RETURNING id
"""


async def update_citation_bbox(
    pending_id: int,
    *,
    field_or_chunk_id: str,
    new_bbox: list[float],
    new_page: int,
) -> dict[str, Any]:
    """Replace one citation's ``bbox`` inside a pending row's payload,
    keyed on ``field_or_chunk_id``.

    The page is validated against the citation's CURRENT page within
    the same transaction — a request whose ``new_page`` doesn't match
    the citation's existing page raises ``invalid_payload`` BEFORE
    any UPDATE runs, so a 400-bound request can never leave a
    half-mutated row behind.

    Returns a dict with:

    - ``row``: the updated row
    - ``previous``: the citation's prior bbox/page (for log replay)
    - ``n_updated``: number of citation entries actually mutated

    Raises :class:`StagingError` with code ``not_found`` when the row
    doesn't exist, ``conflict`` when it's not in ``pending`` state, and
    ``invalid_payload`` when the payload doesn't contain a citation
    matching ``field_or_chunk_id`` or when the requested page differs
    from the citation's existing page.
    """
    pool = await _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_SELECT_ONE_SQL, pending_id)
            if row is None:
                raise StagingError("not_found", f"row {pending_id} not found")
            row_dict = _row_to_dict(row)
            if row_dict.get("state") != "pending":
                raise StagingError(
                    "conflict",
                    f"row {pending_id} is in state '{row_dict.get('state')}', "
                    f"only 'pending' rows accept bbox edits",
                )
            payload = row_dict.get("payload")
            if not isinstance(payload, dict):
                raise StagingError("invalid_payload", "payload is not a dict")
            updated, n, previous = _walk_and_update_citations(
                payload, field_or_chunk_id, list(new_bbox), int(new_page)
            )
            if n == 0 or previous is None:
                raise StagingError(
                    "invalid_payload",
                    f"no citation with field_or_chunk_id={field_or_chunk_id!r}",
                )
            current_page = previous.get("page")
            if isinstance(current_page, int) and int(new_page) != current_page:
                raise StagingError(
                    "invalid_payload",
                    f"citation lives on page {current_page}; bbox edits cannot "
                    f"move it to page {new_page}",
                )
            upd = await conn.fetchrow(
                _UPDATE_PAYLOAD_PENDING_ONLY_SQL,
                pending_id,
                _coerce_jsonb(updated),
            )
            if upd is None:
                raise StagingError(
                    "conflict",
                    f"row {pending_id} state changed during update",
                )
            return {
                "row": {**row_dict, "payload": updated},
                "previous": previous,
                "n_updated": n,
            }


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


_UPDATE_PAYLOAD_SQL = """
    UPDATE copilot_pending_extractions
       SET payload = $2::jsonb
     WHERE id = $1
       AND state = 'pending'
    RETURNING id
"""


async def approve(
    pending_id: int,
    approver: str,
    *,
    request_id: str | None = None,
    provider_id: str | None = None,
    override_payload: dict[str, Any] | None = None,
) -> ApproveResult:
    """Atomic ``pending → approved`` transition. 409 if not pending.

    If ``override_payload`` is provided, the row's ``payload`` jsonb column
    is replaced inside the same DB transaction as the state transition, so
    the downstream writer sees the modified payload. Failures in either
    step roll back the entire transaction — the row stays ``pending``.
    """
    pool = await _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if override_payload is not None:
                upd = await conn.fetchrow(
                    _UPDATE_PAYLOAD_SQL,
                    pending_id,
                    _coerce_jsonb(override_payload),
                )
                if upd is None:
                    existing = await get_pending(pending_id)
                    if existing is None:
                        raise StagingError(
                            "not_found", f"pending_id={pending_id} not found"
                        )
                    raise StagingError(
                        "conflict",
                        f"pending_id={pending_id} not in state 'pending' (state={existing['state']})",
                    )
            row = await conn.fetchrow(_APPROVE_SQL, pending_id, str(approver))
            if row is None:
                # Look up the current state inside the same connection so the
                # raised error rolls back any payload-override write above.
                existing_row = await conn.fetchrow(_SELECT_ONE_SQL, pending_id)
                if existing_row is None:
                    raise StagingError(
                        "not_found", f"pending_id={pending_id} not found"
                    )
                raise StagingError(
                    "conflict",
                    f"pending_id={pending_id} not in state 'pending' (state={existing_row['state']})",
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
