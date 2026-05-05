"""Postgres writer for ``copilot_doc_extractions`` (W2 §4.3 / §4.4).

This module owns the stub-row INSERT...ON CONFLICT DO NOTHING dance that
turns concurrent agent-api workers into a single winner per
``(document_reference_id, content_sha256)`` pair, plus the completion +
failure transitions.  Idempotency by content hash means re-ingesting an
identical PDF returns the cached extraction instead of re-billing
Anthropic.

Dependency rules
----------------
* Reuses the shared asyncpg pool from :mod:`audit.writer`.  No second
  pool, no second DSN.
* ``audit`` is the only sibling we import; we MUST NOT touch ``auth``,
  ``triage``, ``query``, ``agent.tools``, etc.  See ``.importlinter``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from typing import Any, NamedTuple

from audit.writer import get_pool

_logger = logging.getLogger(__name__)


# Backoff schedule per W2 §4.6: 1m, 5m, 15m, 60m. Index by retry_count
# AFTER the increment (i.e. retry_count==1 → 1m wait, retry_count==4 →
# permanently_failed, no retry_after).
_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 3600)
_MAX_RETRIES: int = len(_BACKOFF_SECONDS)


class ClaimResult(NamedTuple):
    """Outcome of a :func:`claim_or_get` call.

    ``owns_claim=True`` means THIS caller is responsible for running the
    extraction graph; ``cached_payload`` is set only when a previous run
    completed successfully.
    """

    extraction_id: int
    owns_claim: bool
    cached_payload: dict[str, Any] | None


def compute_sha256(pdf_bytes: bytes) -> str:
    """Lower-case hex digest of ``pdf_bytes``.

    Used as the deduplication key together with ``document_reference_id``.
    """
    return hashlib.sha256(pdf_bytes).hexdigest()


def _backoff_delta(retry_count: int) -> _dt.timedelta:
    """Return the wait interval for the *next* retry attempt.

    ``retry_count`` is the count AFTER the increment (so 1, 2, 3, 4).
    """
    if retry_count <= 0 or retry_count > _MAX_RETRIES:
        # Defensive fallback; callers gate this through the
        # permanently_failed branch.
        return _dt.timedelta(seconds=_BACKOFF_SECONDS[-1])
    return _dt.timedelta(seconds=_BACKOFF_SECONDS[retry_count - 1])


def _decode_payload(raw: Any) -> dict[str, Any] | None:
    """Normalise asyncpg's JSONB return shape to ``dict | None``."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            decoded = json.loads(raw)
        except Exception:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


_INSERT_STUB_SQL = """
    INSERT INTO copilot_doc_extractions
        (document_reference_id, content_sha256, patient_id, status)
    VALUES ($1, $2, $3, 'processing')
    ON CONFLICT (document_reference_id, content_sha256) DO NOTHING
    RETURNING extraction_id
"""

_SELECT_EXISTING_SQL = """
    SELECT extraction_id, status, extraction_payload, retry_after, retry_count
    FROM copilot_doc_extractions
    WHERE document_reference_id = $1 AND content_sha256 = $2
"""

# Take over a stale ``failed`` row whose retry window has expired.  The
# WHERE clause ensures we only steal when the row is still in a
# retry-eligible state — if a parallel worker beat us to it the UPDATE
# silently affects zero rows and we fall through to the read-only
# branch.
_TAKE_RETRY_CLAIM_SQL = """
    UPDATE copilot_doc_extractions
       SET status = 'processing',
           processing_started_at = NOW(),
           last_error = NULL
     WHERE extraction_id = $1
       AND status = 'failed'
       AND retry_after IS NOT NULL
       AND retry_after <= NOW()
    RETURNING extraction_id
"""

_COMPLETE_SQL = """
    UPDATE copilot_doc_extractions
       SET status = 'complete',
           extraction_kind = $2,
           extraction_payload = $3::jsonb,
           classifier_confidence = $4,
           ocr_confidence_min = $5,
           ocr_confidence_max = $6,
           completed_at = NOW(),
           last_error = NULL,
           retry_after = NULL
     WHERE extraction_id = $1
"""

_FAIL_SQL = """
    UPDATE copilot_doc_extractions
       SET status = $2,
           last_error = $3,
           retry_count = retry_count + $4,
           retry_after = $5
     WHERE extraction_id = $1
    RETURNING retry_count, status
"""


async def _require_pool() -> Any:
    pool = await get_pool()
    if pool is None:
        raise RuntimeError(
            "documents.store requires audit_db_url to be configured (no Postgres pool available)"
        )
    return pool


async def claim_or_get(
    *,
    document_reference_id: str,
    content_sha256: str,
    patient_id: str,
) -> ClaimResult:
    """Atomically claim or look up an extraction record.

    Implements the W2 §4.3 stub-row dance plus the §4.6 retry-window
    rule:

    * If our INSERT wins, ``owns_claim=True`` and the caller MUST run the
      extraction graph.
    * If a row already exists:
        * ``status='complete'`` → returns the cached payload (re-ingest
          path; no Anthropic call).
        * ``status='processing'`` → another worker holds the claim;
          caller waits / polls.
        * ``status='failed'`` and ``retry_after <= NOW()`` → we steal
          the claim atomically.
        * ``status='permanently_failed'`` (or any other unowned state)
          → no claim, no payload.
    """
    pool = await _require_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                _INSERT_STUB_SQL,
                document_reference_id,
                content_sha256,
                patient_id,
            )

        if row is not None:
            extraction_id = int(row["extraction_id"])
            _logger.info(
                "doc_extraction_claimed",
                extra={
                    "extraction_id": extraction_id,
                    "document_reference_id": document_reference_id,
                    "owns_claim": True,
                    "claim_path": "fresh_insert",
                },
            )
            return ClaimResult(
                extraction_id=extraction_id,
                owns_claim=True,
                cached_payload=None,
            )

        existing = await conn.fetchrow(
            _SELECT_EXISTING_SQL,
            document_reference_id,
            content_sha256,
        )
        if existing is None:
            # Should not happen — the INSERT either wins or the conflict
            # row is visible.  Treat as "no claim" defensively.
            raise RuntimeError(
                "claim_or_get: INSERT lost the race but SELECT found no row"
            )

        extraction_id = int(existing["extraction_id"])
        status = str(existing["status"])

        if status == "complete":
            payload = _decode_payload(existing["extraction_payload"])
            _logger.info(
                "doc_extraction_cache_hit",
                extra={
                    "extraction_id": extraction_id,
                    "document_reference_id": document_reference_id,
                    "owns_claim": False,
                    "claim_path": "cached_complete",
                },
            )
            return ClaimResult(
                extraction_id=extraction_id,
                owns_claim=False,
                cached_payload=payload,
            )

        if status == "failed":
            taken = await conn.fetchrow(_TAKE_RETRY_CLAIM_SQL, extraction_id)
            if taken is not None:
                _logger.info(
                    "doc_extraction_claimed",
                    extra={
                        "extraction_id": extraction_id,
                        "document_reference_id": document_reference_id,
                        "owns_claim": True,
                        "claim_path": "retry_takeover",
                    },
                )
                return ClaimResult(
                    extraction_id=extraction_id,
                    owns_claim=True,
                    cached_payload=None,
                )
            _logger.info(
                "doc_extraction_not_claimed",
                extra={
                    "extraction_id": extraction_id,
                    "document_reference_id": document_reference_id,
                    "owns_claim": False,
                    "claim_path": "failed_not_yet_eligible",
                    "status": status,
                },
            )
            return ClaimResult(
                extraction_id=extraction_id,
                owns_claim=False,
                cached_payload=None,
            )

        # status in ('processing', 'permanently_failed') — no claim, no
        # payload.
        _logger.info(
            "doc_extraction_not_claimed",
            extra={
                "extraction_id": extraction_id,
                "document_reference_id": document_reference_id,
                "owns_claim": False,
                "claim_path": "in_flight_or_terminal",
                "status": status,
            },
        )
        return ClaimResult(
            extraction_id=extraction_id,
            owns_claim=False,
            cached_payload=None,
        )


async def complete(
    *,
    extraction_id: int,
    kind: str,
    payload: dict[str, Any],
    classifier_confidence: float,
    ocr_confidence_range: tuple[float, float],
) -> None:
    """Mark an owned claim as ``complete`` with its extraction payload."""
    pool = await _require_pool()
    payload_json = json.dumps(payload, default=str, ensure_ascii=False)
    ocr_min, ocr_max = ocr_confidence_range

    async with pool.acquire() as conn:
        await conn.execute(
            _COMPLETE_SQL,
            extraction_id,
            kind,
            payload_json,
            float(classifier_confidence),
            float(ocr_min),
            float(ocr_max),
        )

    _logger.info(
        "doc_extraction_completed",
        extra={
            "extraction_id": extraction_id,
            "kind": kind,
            "classifier_confidence": float(classifier_confidence),
            "ocr_confidence_min": float(ocr_min),
            "ocr_confidence_max": float(ocr_max),
        },
    )


async def fail(
    *,
    extraction_id: int,
    error: str,
    retry_count_increment: bool = True,
) -> None:
    """Mark an owned claim as ``failed`` and schedule the next retry.

    After ``_MAX_RETRIES`` increments the row is sealed as
    ``permanently_failed`` with ``retry_after = NULL`` and the watchdog
    leaves it alone (W2 §4.6).
    """
    pool = await _require_pool()
    increment = 1 if retry_count_increment else 0

    async with pool.acquire() as conn:
        # Read current retry_count so we can decide post-increment status
        # + retry_after in a single deterministic step.  Wrapped in a
        # transaction so the SELECT and UPDATE see a consistent row.
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT retry_count FROM copilot_doc_extractions WHERE extraction_id = $1 FOR UPDATE",
                extraction_id,
            )
            if current is None:
                raise RuntimeError(
                    f"fail: extraction_id={extraction_id} does not exist"
                )
            new_retry_count = int(current["retry_count"]) + increment

            if new_retry_count >= _MAX_RETRIES:
                next_status = "permanently_failed"
                retry_after: _dt.datetime | None = None
            else:
                next_status = "failed"
                retry_after = _dt.datetime.now(_dt.timezone.utc) + _backoff_delta(
                    max(new_retry_count, 1)
                )

            row = await conn.fetchrow(
                _FAIL_SQL,
                extraction_id,
                next_status,
                error,
                increment,
                retry_after,
            )

    final_status = str(row["status"]) if row is not None else next_status
    final_retry_count = int(row["retry_count"]) if row is not None else new_retry_count
    _logger.info(
        "doc_extraction_failed",
        extra={
            "extraction_id": extraction_id,
            "status": final_status,
            "retry_count": final_retry_count,
            "retry_after": retry_after.isoformat() if retry_after else None,
        },
    )


__all__ = [
    "ClaimResult",
    "claim_or_get",
    "complete",
    "compute_sha256",
    "fail",
]
