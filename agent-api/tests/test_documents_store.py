"""Tests for ``documents.store`` (W2 §4.3 / §4.4).

These tests exercise the stub-row INSERT…ON CONFLICT DO NOTHING dance
against a real Postgres — the concurrency semantics under test are
inherently DB-side (transaction isolation, unique-constraint races) and
cannot be faithfully simulated with mocks.

If no Postgres is reachable (CI lane without the service container, or a
local dev box without docker), the suite cleanly skips.  Configure the
target via ``COPILOT_TEST_PG_DSN``; default is the standard local docker
postgres DSN.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Postgres availability gate ──────────────────────────────────────────────
#
# We probe at import time so the entire module skips with a single clean
# reason instead of erroring inside fixtures.  asyncpg is the only hard
# dependency; if it's missing the audit module would already be broken.

_DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
_TEST_DSN = os.environ.get("COPILOT_TEST_PG_DSN", _DEFAULT_DSN)


def _probe_postgres(dsn: str) -> str | None:
    """Return ``None`` if reachable, else a human-readable skip reason."""
    try:
        import asyncpg  # type: ignore[import-not-found]
    except Exception as exc:
        return f"asyncpg unavailable: {exc}"

    async def _try() -> None:
        conn = await asyncpg.connect(dsn, timeout=2.0)
        await conn.close()

    try:
        asyncio.run(_try())
    except Exception as exc:
        return f"postgres unavailable at {dsn}: {type(exc).__name__}"
    return None


_SKIP_REASON = _probe_postgres(_TEST_DSN)
pytestmark.append(
    pytest.mark.skipif(
        _SKIP_REASON is not None,
        reason=_SKIP_REASON or "postgres unavailable",
    )
)


# Imports that touch the audit pool must come AFTER the pytestmark gate so
# the module imports cleanly during collection on hostless CI lanes.
from audit import writer as audit_writer  # noqa: E402

if _SKIP_REASON is None:  # pragma: no branch — covered by the gate above
    from documents import store  # noqa: E402
    from documents.store import (  # noqa: E402
        ClaimResult,
        claim_or_get,
        complete,
        compute_sha256,
        fail,
    )


# ── Fixtures ────────────────────────────────────────────────────────────────

_SCHEMA_PATH = Path(__file__).parent.parent / "audit" / "schema.sql"


def _reset_pool() -> None:
    audit_writer._pool = None
    audit_writer._pool_lock = None


@pytest.fixture(autouse=True)
async def _pg_setup() -> Any:
    """Apply schema, point the audit pool at the test DSN, isolate per-test.

    The audit writer's ``settings.audit_db_url`` is monkey-patched to the
    test DSN for the lifetime of the test; the shared pool is torn down
    on entry and exit so each test starts from a clean asyncpg state.
    """
    import asyncpg  # type: ignore[import-not-found]

    _reset_pool()
    audit_writer.settings.audit_db_url = _TEST_DSN  # type: ignore[attr-defined]

    # Apply schema (idempotent) + truncate just our table for isolation.
    schema_sql = _SCHEMA_PATH.read_text()
    conn = await asyncpg.connect(_TEST_DSN, timeout=5.0)
    try:
        await conn.execute(schema_sql)
        await conn.execute("TRUNCATE TABLE copilot_doc_extractions RESTART IDENTITY")
    finally:
        await conn.close()

    yield

    # Drop the asyncpg pool so the next test rebuilds it clean.
    await audit_writer.close_pool()
    _reset_pool()


# ── Test cases ──────────────────────────────────────────────────────────────


async def test_concurrent_claim_returns_one_winner() -> None:
    """Five concurrent claim_or_get calls → exactly one ``owns_claim=True``."""
    doc_ref = "DocumentReference/race-1"
    sha = compute_sha256(b"race-pdf-bytes")

    results = await asyncio.gather(
        *(
            claim_or_get(
                document_reference_id=doc_ref,
                content_sha256=sha,
                patient_id="Patient/42",
            )
            for _ in range(5)
        )
    )

    winners = [r for r in results if r.owns_claim]
    losers = [r for r in results if not r.owns_claim]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert len(losers) == 4
    assert all(r.cached_payload is None for r in losers)
    # All five point at the same row.
    assert {r.extraction_id for r in results} == {winners[0].extraction_id}


async def test_reingest_returns_cached() -> None:
    """Completed extraction → second claim returns cached payload, no rerun."""
    doc_ref = "DocumentReference/cache-1"
    pdf_bytes = b"identical-pdf-content"
    sha = compute_sha256(pdf_bytes)

    first = await claim_or_get(
        document_reference_id=doc_ref,
        content_sha256=sha,
        patient_id="Patient/7",
    )
    assert first.owns_claim is True

    payload = {
        "kind": "lab_report",
        "fields": [{"name": "WBC", "value": "7.2", "bbox": [10, 20, 30, 40]}],
        "ocr_confidence": {"min": 0.91, "max": 0.99},
    }
    await complete(
        extraction_id=first.extraction_id,
        kind="lab_report",
        payload=payload,
        classifier_confidence=0.95,
        ocr_confidence_range=(0.91, 0.99),
    )

    # Second claim with identical args — must be a cache hit.  We do NOT
    # involve any Anthropic stub here; the contract is "if cached_payload
    # is set, the caller skips the graph entirely", so the absence of any
    # Anthropic reference in this test IS the assertion.
    second = await claim_or_get(
        document_reference_id=doc_ref,
        content_sha256=sha,
        patient_id="Patient/7",
    )
    assert second.owns_claim is False
    assert second.extraction_id == first.extraction_id
    assert second.cached_payload == payload


async def test_failed_eligible_for_retry_after_window() -> None:
    """Failed row whose retry_after has elapsed → next claim_or_get wins."""
    import asyncpg  # type: ignore[import-not-found]

    doc_ref = "DocumentReference/retry-1"
    sha = compute_sha256(b"flaky-pdf")

    first = await claim_or_get(
        document_reference_id=doc_ref,
        content_sha256=sha,
        patient_id="Patient/9",
    )
    assert first.owns_claim is True

    await fail(extraction_id=first.extraction_id, error="vision_call_timeout")

    # Fast-forward retry_after into the past.
    conn = await asyncpg.connect(_TEST_DSN, timeout=5.0)
    try:
        await conn.execute(
            "UPDATE copilot_doc_extractions SET retry_after = NOW() - INTERVAL '1 second' WHERE extraction_id = $1",
            first.extraction_id,
        )
        row_before = await conn.fetchrow(
            "SELECT status, retry_count FROM copilot_doc_extractions WHERE extraction_id = $1",
            first.extraction_id,
        )
    finally:
        await conn.close()

    assert row_before["status"] == "failed"
    assert row_before["retry_count"] == 1

    second = await claim_or_get(
        document_reference_id=doc_ref,
        content_sha256=sha,
        patient_id="Patient/9",
    )
    assert second.owns_claim is True
    assert second.extraction_id == first.extraction_id
    assert second.cached_payload is None

    # Confirm the row is back in 'processing' with retry_count preserved.
    conn = await asyncpg.connect(_TEST_DSN, timeout=5.0)
    try:
        row_after = await conn.fetchrow(
            "SELECT status, retry_count FROM copilot_doc_extractions WHERE extraction_id = $1",
            first.extraction_id,
        )
    finally:
        await conn.close()
    assert row_after["status"] == "processing"
    assert row_after["retry_count"] == 1


async def test_permanently_failed_after_max_retries() -> None:
    """Four consecutive fail() calls → status='permanently_failed', no claim."""
    import asyncpg  # type: ignore[import-not-found]

    doc_ref = "DocumentReference/dead-1"
    sha = compute_sha256(b"unrecoverable-pdf")

    claim = await claim_or_get(
        document_reference_id=doc_ref,
        content_sha256=sha,
        patient_id="Patient/13",
    )
    assert claim.owns_claim is True

    for _ in range(4):
        await fail(extraction_id=claim.extraction_id, error="vision_unavailable")

    conn = await asyncpg.connect(_TEST_DSN, timeout=5.0)
    try:
        row = await conn.fetchrow(
            "SELECT status, retry_count, retry_after FROM copilot_doc_extractions WHERE extraction_id = $1",
            claim.extraction_id,
        )
    finally:
        await conn.close()

    assert row["status"] == "permanently_failed"
    assert row["retry_count"] == 4
    assert row["retry_after"] is None

    # Subsequent claim_or_get yields no claim, no payload.
    rerun = await claim_or_get(
        document_reference_id=doc_ref,
        content_sha256=sha,
        patient_id="Patient/13",
    )
    assert isinstance(rerun, ClaimResult)
    assert rerun.owns_claim is False
    assert rerun.cached_payload is None
    assert rerun.extraction_id == claim.extraction_id


# ── Sanity unit tests that don't need Postgres (still gated by skip) ────────


def test_compute_sha256_is_lowercase_hex() -> None:
    digest = compute_sha256(b"hello")
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert digest == digest.lower()


def test_backoff_schedule_matches_spec() -> None:
    # 1m, 5m, 15m, 60m per W2 §4.6
    assert store._BACKOFF_SECONDS == (60, 300, 900, 3600)
    assert store._MAX_RETRIES == 4
