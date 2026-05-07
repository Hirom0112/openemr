"""Quarantine state machine for the pre-extraction resolver (Slice 9.2).

State machine
-------------

    unclaimed ─claim──▶ claimed ─match──▶ matched   (terminal)
        │                  │
        │                  └─reject──▶ rejected     (terminal)
        │
        └─(36h TTL elapses)──▶ expired              (terminal)

    A claimed row whose ``claim_expires_at`` has passed (10 min default)
    is treated as ``unclaimed`` again — another operator may pick it up.

Audit + observability are emitted by the HTTP caller (this module is part
of the ``demographics`` leaf per ``.importlinter``; it cannot import
``audit`` or ``observability``). Functions here return enough metadata
for the caller to drive both the response and the audit pipeline.

Pool injection
--------------

Every public coroutine takes the asyncpg pool as a parameter. The HTTP
layer fetches it once via ``audit.writer.get_pool()`` (the shared pool
documented in :mod:`documents.store`) and threads it in. This keeps the
``demographics`` package free of cross-leaf imports.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any, Literal


# ── State + outcome types ────────────────────────────────────────────────────

QuarantineState = Literal["unclaimed", "claimed", "matched", "rejected", "expired"]

# Caller-supplied role tag — only "clinician" may match/reject; "system"
# transitions are reserved for the TTL reaper (Slice 9.2 leaves the reaper
# stub out — production deploys can wire APScheduler in Slice 9.3).
TransitionRole = Literal["clinician", "system"]


CLAIM_TTL_SECONDS: int = 600  # 10 min
QUARANTINE_TTL_SECONDS: int = 36 * 3600  # 36 hours

REJECT_REASON_MAX_CHARS: int = 500


@dataclass(frozen=True)
class TransitionResult:
    """Returned by every state-changing operation."""

    quarantine_id: str
    state: QuarantineState
    previous_state: QuarantineState
    claim_expires_at: _dt.datetime | None = None
    resolved_patient_id: str | None = None


class QuarantineError(Exception):
    """Raised when a transition violates the state machine.

    The ``code`` field is a stable string the HTTP layer maps to a 4xx
    response (409 for conflicts, 404 for missing rows, 410 for expired).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── DB plumbing ──────────────────────────────────────────────────────────────


_INSERT_SQL = """
    INSERT INTO copilot_quarantined_documents
        (document_reference_id, file_batch_id, panel_id,
         parsed_identity, candidate_matches, reason_code)
    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
    RETURNING quarantine_id, expires_at
"""


_LIST_SQL_BASE = """
    SELECT quarantine_id, document_reference_id, file_batch_id, panel_id,
           parsed_identity, candidate_matches, reason_code, state,
           claimed_by, claimed_at, claim_expires_at,
           resolved_patient_id, resolved_at, resolved_by,
           rejected_reason, quarantined_at, expires_at
    FROM copilot_quarantined_documents
    WHERE panel_id IS NOT DISTINCT FROM $1
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _coerce_jsonb(value: Any) -> str:
    """Encode a Python dict / list to a Postgres jsonb-castable text payload.

    asyncpg can serialise dicts directly when no codec is registered, but
    routes that share connections with other libraries occasionally trip on
    type-introspection — encoding to text + casting in SQL keeps the
    behaviour invariant.
    """
    if value is None:
        return "null"
    return json.dumps(value, default=str, ensure_ascii=False)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """asyncpg Record → plain dict, with dates as ISO strings."""
    out: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, _dt.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, (bytes, bytearray)):
            try:
                out[k] = v.decode("utf-8")
            except Exception:
                out[k] = repr(v)
        elif isinstance(v, str) and k in {"parsed_identity", "candidate_matches"}:
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        else:
            out[k] = v
    # Stringify the UUID for JSON friendliness — asyncpg returns uuid.UUID.
    qid = out.get("quarantine_id")
    if qid is not None and not isinstance(qid, str):
        out["quarantine_id"] = str(qid)
    return out


# ── Public API ───────────────────────────────────────────────────────────────


async def quarantine_document(
    *,
    pool: Any,
    document_reference_id: str,
    file_batch_id: str | None,
    panel_id: str | None,
    parsed_identity: dict[str, Any],
    candidate_matches: list[dict[str, Any]] | None,
    reason_code: str,
) -> dict[str, Any]:
    """Insert a new ``unclaimed`` quarantine row.

    Returns ``{"quarantine_id", "expires_at", "reason_code"}``. Raises
    ``RuntimeError`` if no pool is available — the caller surfaces that
    as 500 Internal Server Error (an unmatched document is a clinical
    safety event, we do not silently drop it).
    """
    if pool is None:
        raise RuntimeError("audit asyncpg pool unavailable")

    parsed_text = _coerce_jsonb(parsed_identity)
    candidates_text = _coerce_jsonb(candidate_matches or [])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_SQL,
            document_reference_id,
            file_batch_id,
            panel_id,
            parsed_text,
            candidates_text,
            reason_code,
        )
    qid = row["quarantine_id"]
    expires_at = row["expires_at"]
    return {
        "quarantine_id": str(qid),
        "expires_at": (
            expires_at.isoformat()
            if isinstance(expires_at, _dt.datetime)
            else str(expires_at)
        ),
        "reason_code": reason_code,
    }


async def list_quarantined(
    *,
    pool: Any,
    panel_id: str | None,
    state: QuarantineState | None = None,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    """Panel-scoped queue read.

    Mirrors ``f41827440`` precedent — ``panel_id`` is sourced from
    ``request_principal_var`` upstream. ``None`` panel matches rows whose
    ``panel_id`` is also NULL (legacy / system-ingested uploads).
    """
    if pool is None:
        return []

    sql = _LIST_SQL_BASE
    params: list[Any] = [panel_id]
    if state is not None:
        sql += " AND state = $2"
        params.append(state)
    if not include_expired:
        sql += f" AND quarantined_at > NOW() - INTERVAL '{QUARANTINE_TTL_SECONDS} seconds'"
    sql += " ORDER BY quarantined_at DESC LIMIT 200"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [_row_to_dict(r) for r in rows]


async def claim(
    *,
    pool: Any,
    quarantine_id: str,
    claimer_provider_id: str,
) -> TransitionResult:
    """Acquire the 10-minute exclusive claim lock on a row.

    Uses a single UPDATE...WHERE that gates on ``state='unclaimed'`` OR
    (``state='claimed'`` AND claim_expires_at < NOW()). Postgres's
    UPDATE...WHERE is atomic per row, so two concurrent claimers race
    and exactly one wins.
    """
    if pool is None:
        raise RuntimeError("audit asyncpg pool unavailable")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE copilot_quarantined_documents
               SET state            = 'claimed',
                   claimed_by       = $2,
                   claimed_at       = NOW(),
                   claim_expires_at = NOW() + ($3 || ' seconds')::interval
             WHERE quarantine_id    = $1
               AND state            IN ('unclaimed', 'claimed')
               AND (
                     state = 'unclaimed'
                  OR claim_expires_at IS NULL
                  OR claim_expires_at < NOW()
               )
            RETURNING quarantine_id, state, claim_expires_at,
                      (SELECT state FROM copilot_quarantined_documents
                       WHERE quarantine_id = $1) AS prev_state_unused
            """,
            quarantine_id,
            claimer_provider_id,
            str(CLAIM_TTL_SECONDS),
        )

        if row is None:
            existing = await conn.fetchrow(
                "SELECT state FROM copilot_quarantined_documents "
                "WHERE quarantine_id = $1",
                quarantine_id,
            )
            if existing is None:
                raise QuarantineError("not_found", f"unknown quarantine_id {quarantine_id}")
            raise QuarantineError(
                "conflict",
                f"quarantine row is in terminal state {existing['state']!r}",
            )

    return TransitionResult(
        quarantine_id=str(row["quarantine_id"]),
        state="claimed",
        previous_state="unclaimed",
        claim_expires_at=row["claim_expires_at"],
    )


async def match(
    *,
    pool: Any,
    quarantine_id: str,
    target_patient_id: str,
    claimer_provider_id: str,
    panel_patient_ids: list[str] | None = None,
) -> TransitionResult:
    """Resolve a quarantined upload to ``target_patient_id``.

    Validates:
      * the caller currently holds an unexpired claim, AND
      * (when ``panel_patient_ids`` is supplied) ``target_patient_id``
        sits inside the caller's panel.

    Both checks are mandatory to prevent a clinician quarantining the
    document into another panel's chart.
    """
    if pool is None:
        raise RuntimeError("audit asyncpg pool unavailable")
    if not target_patient_id:
        raise QuarantineError("invalid_target", "target_patient_id is required")
    if panel_patient_ids is not None:
        in_panel = {str(p) for p in panel_patient_ids}
        if str(target_patient_id) not in in_panel:
            raise QuarantineError(
                "panel_violation",
                f"target_patient_id {target_patient_id} is not on the caller's panel",
            )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE copilot_quarantined_documents
               SET state               = 'matched',
                   resolved_patient_id = $2,
                   resolved_at         = NOW(),
                   resolved_by         = $3
             WHERE quarantine_id      = $1
               AND state              = 'claimed'
               AND claimed_by         = $3
               AND claim_expires_at   > NOW()
            RETURNING quarantine_id, resolved_patient_id
            """,
            quarantine_id,
            target_patient_id,
            claimer_provider_id,
        )

        if row is None:
            existing = await conn.fetchrow(
                "SELECT state, claimed_by, claim_expires_at "
                "FROM copilot_quarantined_documents WHERE quarantine_id = $1",
                quarantine_id,
            )
            if existing is None:
                raise QuarantineError("not_found", f"unknown quarantine_id {quarantine_id}")
            if existing["state"] != "claimed":
                raise QuarantineError(
                    "conflict",
                    f"quarantine row is in state {existing['state']!r}, expected 'claimed'",
                )
            if existing["claimed_by"] != claimer_provider_id:
                raise QuarantineError(
                    "claim_violation",
                    "quarantine row is claimed by a different operator",
                )
            raise QuarantineError("claim_expired", "claim has expired; re-claim before matching")

    return TransitionResult(
        quarantine_id=str(row["quarantine_id"]),
        state="matched",
        previous_state="claimed",
        resolved_patient_id=str(row["resolved_patient_id"]),
    )


async def reject(
    *,
    pool: Any,
    quarantine_id: str,
    reason: str,
    claimer_provider_id: str,
    role: TransitionRole = "clinician",
) -> TransitionResult:
    """Permanently reject a quarantined upload."""
    if pool is None:
        raise RuntimeError("audit asyncpg pool unavailable")
    cleaned = (reason or "").strip()
    if not cleaned:
        raise QuarantineError("invalid_reason", "reason is required")
    if len(cleaned) > REJECT_REASON_MAX_CHARS:
        raise QuarantineError(
            "invalid_reason",
            f"reason exceeds {REJECT_REASON_MAX_CHARS} chars",
        )

    async with pool.acquire() as conn:
        # Clinicians must hold the claim; system transitions (TTL reaper)
        # bypass the claim gate.
        if role == "clinician":
            row = await conn.fetchrow(
                """
                UPDATE copilot_quarantined_documents
                   SET state           = 'rejected',
                       rejected_reason = $2,
                       resolved_at     = NOW(),
                       resolved_by     = $3
                 WHERE quarantine_id   = $1
                   AND state           = 'claimed'
                   AND claimed_by      = $3
                   AND claim_expires_at > NOW()
                RETURNING quarantine_id
                """,
                quarantine_id,
                cleaned,
                claimer_provider_id,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE copilot_quarantined_documents
                   SET state           = 'rejected',
                       rejected_reason = $2,
                       resolved_at     = NOW(),
                       resolved_by     = $3
                 WHERE quarantine_id   = $1
                   AND state           IN ('unclaimed', 'claimed')
                RETURNING quarantine_id
                """,
                quarantine_id,
                cleaned,
                claimer_provider_id,
            )

        if row is None:
            existing = await conn.fetchrow(
                "SELECT state, claimed_by, claim_expires_at "
                "FROM copilot_quarantined_documents WHERE quarantine_id = $1",
                quarantine_id,
            )
            if existing is None:
                raise QuarantineError("not_found", f"unknown quarantine_id {quarantine_id}")
            if existing["state"] not in {"unclaimed", "claimed"}:
                raise QuarantineError(
                    "conflict",
                    f"quarantine row is in terminal state {existing['state']!r}",
                )
            if role == "clinician" and existing["claimed_by"] != claimer_provider_id:
                raise QuarantineError(
                    "claim_violation",
                    "quarantine row is claimed by a different operator",
                )
            raise QuarantineError("claim_expired", "claim has expired; re-claim before rejecting")

    return TransitionResult(
        quarantine_id=str(row["quarantine_id"]),
        state="rejected",
        previous_state="claimed" if role == "clinician" else "unclaimed",
    )


async def invalidate_soft_resolve(
    *,
    pool: Any,
    document_reference_id: str,
    panel_id: str | None,
    parsed_identity: dict[str, Any],
    candidate_matches: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Post-extraction §5.6 hook.

    The §5.6 demographic check (``demographics.check.check_demographics``)
    runs after extraction. When the original resolver ``Pass(source=
    'name_dob_soft')`` produced the patient_id and §5.6 returns
    ``hard_block`` (or even soft_warn that the caller policy promotes),
    the right answer is to UNDO the soft-resolve and route the document
    back to quarantine with reason ``SOFT_RESOLVE_INVALIDATED``. This
    function writes the row; the caller is responsible for nullifying
    any downstream extraction state.
    """
    return await quarantine_document(
        pool=pool,
        document_reference_id=document_reference_id,
        file_batch_id=None,
        panel_id=panel_id,
        parsed_identity=parsed_identity,
        candidate_matches=candidate_matches,
        reason_code="SOFT_RESOLVE_INVALIDATED",
    )


__all__ = [
    "CLAIM_TTL_SECONDS",
    "QUARANTINE_TTL_SECONDS",
    "REJECT_REASON_MAX_CHARS",
    "QuarantineError",
    "QuarantineState",
    "TransitionResult",
    "TransitionRole",
    "claim",
    "invalidate_soft_resolve",
    "list_quarantined",
    "match",
    "quarantine_document",
    "reject",
]
