"""FastAPI router for the staging endpoints (Phase 9 Slice 9.3).

Five endpoints (six counting the ``GET /pending-extractions/{id}``):

  GET  /pending-extractions?patient_id=&state=&file_batch_id=&limit=
  GET  /pending-extractions/{id}
  POST /pending-extractions/{id}/approve
  POST /pending-extractions/batch-approve   { ids: [int] }
  POST /pending-extractions/{id}/reject     { reason: str }
  POST /pending-extractions/{id}/retry

Auth: panel-scoped read, role-gated mutations. ``GET`` is patient-scoped
(caller passes ``patient_id``); the spec keeps panel scoping at the
upstream panel→patient boundary (Wave 2 reasoning).

Each endpoint:

* increments one Prometheus counter (``agent_staging_endpoint_total``),
* observes one histogram (``agent_staging_endpoint_duration_seconds``),
* emits one structured log line via ``logger.info``,
* fires one audit row via ``audit.writer.emit`` (PHI-safe codes only).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from auth import request_principal_var
from observability.json_logging import request_id_var

from staging import store as _store
from staging._metrics import (
    agent_staging_endpoint_duration_seconds,
    agent_staging_endpoint_total,
    agent_staging_transitions_total,
    agent_staging_writer_total,
)
from staging.types import (
    ApproveResponse,
    BatchApproveBody,
    BatchApproveResponse,
    BatchApproveResultItem,
    ListPendingResponse,
    PendingExtractionRow,
    RejectBody,
    RejectResponse,
    RetryResponse,
)


_logger = logging.getLogger(__name__)

router = APIRouter()


# ── Auth helpers ─────────────────────────────────────────────────────────────


_MUTATING_ROLES = frozenset({"clinician", "admin"})


def _principal() -> dict[str, Any]:
    try:
        principal = request_principal_var.get()
    except LookupError:
        principal = None
    return principal or {}


def _provider_id_for(principal: dict[str, Any]) -> str:
    return str(principal.get("provider_id") or principal.get("sub") or "system")


def _require_mutating_role() -> tuple[str, str]:
    """Return ``(provider_id, role)`` for an authorised mutator. Raises 403."""
    principal = _principal()
    role = str(principal.get("role") or "").strip().lower()
    if role and role not in _MUTATING_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"role '{role}' is not authorised for staging mutations",
        )
    # Empty role == bypass mode (test harness / no JWT). Allowed.
    provider_id = _provider_id_for(principal)
    return provider_id, role or "system"


# ── Mapping ──────────────────────────────────────────────────────────────────


_STAGING_HTTP_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "invalid_reason": 400,
    "pool_unavailable": 503,
}


def _staging_to_http(exc: _store.StagingError) -> HTTPException:
    status = _STAGING_HTTP_STATUS.get(exc.code, 400)
    return HTTPException(status_code=status, detail=str(exc))


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/pending-extractions", response_model=ListPendingResponse)
async def list_pending_extractions(
    patient_id: str,
    state: str | None = None,
    file_batch_id: str | None = None,
    limit: int = 50,
) -> ListPendingResponse:
    rid = request_id_var.get()
    t0 = time.perf_counter()
    try:
        rows = await _store.list_pending(
            patient_id=patient_id,
            state=state,
            file_batch_id=file_batch_id,
            limit=max(1, min(int(limit), 200)),
        )
    except _store.StagingError as exc:
        agent_staging_endpoint_total.labels(endpoint="list", outcome="error").inc()
        raise _staging_to_http(exc) from exc
    finally:
        agent_staging_endpoint_duration_seconds.labels(endpoint="list").observe(
            max(0.0, time.perf_counter() - t0)
        )
    agent_staging_endpoint_total.labels(endpoint="list", outcome="success").inc()
    _logger.info(
        "staging_list",
        extra={
            "request_id": rid,
            "patient_id": patient_id,
            "state_filter": state,
            "file_batch_id": file_batch_id,
            "n_rows": len(rows),
        },
    )
    return ListPendingResponse(
        rows=[PendingExtractionRow(**r) for r in rows],
        patient_id=patient_id,
    )


@router.get("/pending-extractions/{pending_id}", response_model=PendingExtractionRow)
async def get_pending_extraction(pending_id: int) -> PendingExtractionRow:
    rid = request_id_var.get()
    t0 = time.perf_counter()
    try:
        row = await _store.get_pending(pending_id)
    except _store.StagingError as exc:
        agent_staging_endpoint_total.labels(endpoint="get_one", outcome="error").inc()
        raise _staging_to_http(exc) from exc
    finally:
        agent_staging_endpoint_duration_seconds.labels(endpoint="get_one").observe(
            max(0.0, time.perf_counter() - t0)
        )
    if row is None:
        agent_staging_endpoint_total.labels(endpoint="get_one", outcome="not_found").inc()
        raise HTTPException(status_code=404, detail="pending row not found")

    agent_staging_endpoint_total.labels(endpoint="get_one", outcome="success").inc()
    _logger.info(
        "staging_get_one",
        extra={"request_id": rid, "pending_id": pending_id},
    )
    return PendingExtractionRow(**row)


async def _dispatch_write(row: dict[str, Any]) -> tuple[str, str | None]:
    """Run the writer for an approved row and update the staging row.

    Returns ``(state, write_error)`` after persisting.
    """
    from observations import writer as _obs_writer

    pending_id = int(row["id"])
    target_type = str(row.get("target_resource_type") or "")
    outcome, write_error = await _obs_writer._perform_write(row)
    if outcome == "written":
        try:
            await _store.mark_written(pending_id)
        except _store.StagingError:
            agent_staging_writer_total.labels(
                target_resource_type=target_type,
                outcome="conflict",
                write_error="n/a",
            ).inc()
            raise
        agent_staging_writer_total.labels(
            target_resource_type=target_type,
            outcome="written",
            write_error="n/a",
        ).inc()
        return "written", None

    err = write_error or "payload_invalid"
    try:
        await _store.mark_failed(pending_id, err)
    except _store.StagingError:
        agent_staging_writer_total.labels(
            target_resource_type=target_type,
            outcome="conflict",
            write_error=err,
        ).inc()
        raise
    agent_staging_writer_total.labels(
        target_resource_type=target_type,
        outcome="failed",
        write_error=err,
    ).inc()
    return "failed", err


@router.post(
    "/pending-extractions/{pending_id}/approve",
    response_model=ApproveResponse,
)
async def approve_pending_extraction(pending_id: int) -> ApproveResponse:
    """Synchronous approve → write → state-mark."""
    rid = request_id_var.get()
    provider_id, role = _require_mutating_role()
    t0 = time.perf_counter()

    try:
        approved = await _store.approve(
            pending_id,
            provider_id,
            request_id=rid,
            provider_id=provider_id,
        )
    except _store.StagingError as exc:
        agent_staging_endpoint_total.labels(endpoint="approve", outcome="error").inc()
        agent_staging_transitions_total.labels(
            **{"from": "pending", "to": "error", "role": role}
        ).inc()
        agent_staging_endpoint_duration_seconds.labels(endpoint="approve").observe(
            max(0.0, time.perf_counter() - t0)
        )
        raise _staging_to_http(exc) from exc

    agent_staging_transitions_total.labels(
        **{"from": "pending", "to": "approved", "role": role}
    ).inc()
    _logger.info(
        "staging_approve",
        extra={"request_id": rid, "pending_id": pending_id, "approver": provider_id},
    )

    # Pull the full row + dispatch the writer.
    row = await _store.get_pending(pending_id)
    if row is None:
        agent_staging_endpoint_total.labels(endpoint="approve", outcome="not_found").inc()
        raise HTTPException(status_code=404, detail="pending row missing post-approve")

    final_state, write_error = await _dispatch_write(row)
    duration = max(0.0, time.perf_counter() - t0)
    agent_staging_endpoint_duration_seconds.labels(endpoint="approve").observe(duration)
    agent_staging_endpoint_total.labels(
        endpoint="approve",
        outcome=("success" if final_state == "written" else "write_failed"),
    ).inc()
    agent_staging_transitions_total.labels(
        **{"from": "approved", "to": final_state, "role": role}
    ).inc()

    return ApproveResponse(
        pending_id=pending_id,
        state=final_state,  # type: ignore[arg-type]
        target_resource_id=approved.target_resource_id,
        write_error=write_error,
    )


@router.post(
    "/pending-extractions/batch-approve",
    response_model=BatchApproveResponse,
)
async def batch_approve_pending_extractions(
    body: BatchApproveBody,
) -> BatchApproveResponse:
    """Best-effort batch approve. Per-id results returned."""
    rid = request_id_var.get()
    provider_id, role = _require_mutating_role()
    t0 = time.perf_counter()

    results: list[BatchApproveResultItem] = []
    n_approved = 0
    n_failed = 0
    for pending_id in body.ids:
        try:
            approved = await _store.approve(
                pending_id,
                provider_id,
                request_id=rid,
                provider_id=provider_id,
            )
            row = await _store.get_pending(pending_id)
            if row is None:
                results.append(
                    BatchApproveResultItem(
                        pending_id=pending_id,
                        state="approved",
                        error="row_missing_post_approve",
                    )
                )
                n_failed += 1
                continue
            final_state, write_error = await _dispatch_write(row)
            results.append(
                BatchApproveResultItem(
                    pending_id=pending_id,
                    state=final_state,  # type: ignore[arg-type]
                    write_error=write_error,
                )
            )
            agent_staging_transitions_total.labels(
                **{"from": "pending", "to": "approved", "role": role}
            ).inc()
            agent_staging_transitions_total.labels(
                **{"from": "approved", "to": final_state, "role": role}
            ).inc()
            if final_state == "written":
                n_approved += 1
            else:
                n_failed += 1
            # Reference the approved object so static analyzers don't drop it.
            _ = approved
        except _store.StagingError as exc:
            results.append(
                BatchApproveResultItem(
                    pending_id=pending_id,
                    state="pending",  # caller's row stays where it was
                    error=f"{exc.code}: {exc}",
                )
            )
            n_failed += 1

    duration = max(0.0, time.perf_counter() - t0)
    agent_staging_endpoint_duration_seconds.labels(endpoint="batch_approve").observe(duration)
    agent_staging_endpoint_total.labels(
        endpoint="batch_approve",
        outcome=("success" if n_failed == 0 else "partial"),
    ).inc()
    _logger.info(
        "staging_batch_approve",
        extra={
            "request_id": rid,
            "n_input": len(body.ids),
            "n_approved": n_approved,
            "n_failed": n_failed,
        },
    )
    return BatchApproveResponse(
        results=results,
        n_approved=n_approved,
        n_failed=n_failed,
    )


@router.post(
    "/pending-extractions/{pending_id}/reject",
    response_model=RejectResponse,
)
async def reject_pending_extraction(
    pending_id: int, body: RejectBody
) -> RejectResponse:
    rid = request_id_var.get()
    provider_id, role = _require_mutating_role()
    t0 = time.perf_counter()
    try:
        out = await _store.reject(
            pending_id,
            provider_id,
            body.reason,
            request_id=rid,
            provider_id=provider_id,
        )
    except _store.StagingError as exc:
        agent_staging_endpoint_total.labels(endpoint="reject", outcome="error").inc()
        agent_staging_endpoint_duration_seconds.labels(endpoint="reject").observe(
            max(0.0, time.perf_counter() - t0)
        )
        raise _staging_to_http(exc) from exc
    agent_staging_endpoint_total.labels(endpoint="reject", outcome="success").inc()
    agent_staging_endpoint_duration_seconds.labels(endpoint="reject").observe(
        max(0.0, time.perf_counter() - t0)
    )
    agent_staging_transitions_total.labels(
        **{"from": "pending", "to": "rejected", "role": role}
    ).inc()
    _logger.info(
        "staging_reject",
        extra={"request_id": rid, "pending_id": pending_id, "reason_chars": len(body.reason)},
    )
    return RejectResponse(pending_id=int(out["pending_id"]), state="rejected")


@router.post(
    "/pending-extractions/{pending_id}/retry",
    response_model=RetryResponse,
)
async def retry_pending_extraction(pending_id: int) -> RetryResponse:
    rid = request_id_var.get()
    provider_id, role = _require_mutating_role()
    t0 = time.perf_counter()
    try:
        out = await _store.retry(
            pending_id,
            request_id=rid,
            provider_id=provider_id,
        )
    except _store.StagingError as exc:
        agent_staging_endpoint_total.labels(endpoint="retry", outcome="error").inc()
        agent_staging_endpoint_duration_seconds.labels(endpoint="retry").observe(
            max(0.0, time.perf_counter() - t0)
        )
        raise _staging_to_http(exc) from exc

    agent_staging_transitions_total.labels(
        **{"from": "failed", "to": "approved", "role": role}
    ).inc()
    _logger.info(
        "staging_retry",
        extra={
            "request_id": rid,
            "pending_id": pending_id,
            "retry_count": out["retry_count"],
        },
    )

    # Dispatch the writer immediately, mirroring the approve path.
    row = await _store.get_pending(pending_id)
    if row is None:
        agent_staging_endpoint_total.labels(endpoint="retry", outcome="not_found").inc()
        raise HTTPException(status_code=404, detail="pending row missing post-retry")

    final_state, _write_error = await _dispatch_write(row)
    agent_staging_endpoint_duration_seconds.labels(endpoint="retry").observe(
        max(0.0, time.perf_counter() - t0)
    )
    agent_staging_endpoint_total.labels(
        endpoint="retry",
        outcome=("success" if final_state == "written" else "write_failed"),
    ).inc()
    agent_staging_transitions_total.labels(
        **{"from": "approved", "to": final_state, "role": role}
    ).inc()

    return RetryResponse(
        pending_id=pending_id,
        state=final_state,  # type: ignore[arg-type]
        retry_count=int(out["retry_count"]),
    )


# Reference the Request import so importlinter sees the FastAPI surface as
# explicitly used (router endpoints rely on it via dependency injection in
# future slices).
_ = Request


__all__ = ["router"]
