"""Tests for the Postgres PHI audit writer (``audit.writer``).

Covers:

* :func:`audit.writer.emit` is a no-op when ``settings.audit_db_url`` is empty.
* :func:`audit.writer.emit` NEVER raises — DB failures are swallowed and
  logged as a warning.
* The detail_json validation tripwire rejects
  (a) payloads exceeding the 2 KB string-encoded ceiling, and
  (b) payloads whose JSON form contains a block-listed phrase
  (``Vancomycin``, ``patient``, ``diagnosis``).
* Validation rejection produces a dropped-counter increment instead of
  attempting a DB write.

These tests stub the asyncpg pool — they do not require a live Postgres.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from audit import writer as audit_writer  # noqa: E402
from audit.models import AuditEvent  # noqa: E402


def _reset_pool() -> None:
    """Tear down any cached pool so each test starts fresh."""
    audit_writer._pool = None
    audit_writer._pool_lock = None


@pytest.fixture(autouse=True)
def _isolate_pool() -> Any:
    _reset_pool()
    yield
    _reset_pool()


# ── No-op when audit_db_url is empty ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_is_noop_when_audit_db_url_empty() -> None:
    """An empty DSN MUST short-circuit before any pool init or DB call."""
    with patch.object(audit_writer.settings, "audit_db_url", ""):
        # Patch _get_pool so any accidental call would explode the test.
        with patch.object(audit_writer, "_get_pool", side_effect=AssertionError("pool init not allowed")):
            await audit_writer.emit(AuditEvent(event_type="request", method="GET", path="/x"))


# ── emit() never raises ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_swallows_pool_create_failure() -> None:
    """If pool creation raises, emit() must log a warning and return cleanly."""
    with patch.object(audit_writer.settings, "audit_db_url", "postgresql://x:y@host/db"):
        with patch.object(audit_writer, "_get_pool", AsyncMock(side_effect=RuntimeError("nope"))):
            # MUST NOT RAISE
            await audit_writer.emit(AuditEvent(event_type="request"))


@pytest.mark.asyncio
async def test_emit_swallows_db_execute_failure() -> None:
    """If the INSERT itself raises, emit() must catch it silently."""
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(side_effect=RuntimeError("connection lost"))
    fake_pool = MagicMock()

    class _AcquireCM:
        async def __aenter__(self) -> Any:
            return fake_conn

        async def __aexit__(self, *a: Any) -> None:
            return None

    fake_pool.acquire = MagicMock(return_value=_AcquireCM())

    with patch.object(audit_writer.settings, "audit_db_url", "postgresql://x:y@host/db"):
        with patch.object(audit_writer, "_get_pool", AsyncMock(return_value=fake_pool)):
            # MUST NOT RAISE
            await audit_writer.emit(AuditEvent(event_type="tool_call", tool_name="briefing"))


# ── Validation tripwire ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_rejects_oversized_detail_json() -> None:
    """Payloads exceeding 2 KB string-encoded MUST be dropped pre-write."""
    huge_blob = "x" * 4096  # ~4 KB
    event = AuditEvent(event_type="tool_call", detail_json={"blob": huge_blob})

    fake_pool_get = AsyncMock(side_effect=AssertionError("write attempted on oversized payload"))

    with patch.object(audit_writer.settings, "audit_db_url", "postgresql://x:y@host/db"):
        with patch.object(audit_writer, "_get_pool", fake_pool_get):
            await audit_writer.emit(event)

    fake_pool_get.assert_not_called()


@pytest.mark.parametrize("forbidden_word", ["Vancomycin", "patient", "diagnosis"])
@pytest.mark.asyncio
async def test_emit_rejects_blocklisted_detail_json(forbidden_word: str) -> None:
    """detail_json containing a block-listed phrase MUST be dropped pre-write."""
    event = AuditEvent(
        event_type="tool_call",
        detail_json={"note": f"contains {forbidden_word} which is forbidden"},
    )

    fake_pool_get = AsyncMock(side_effect=AssertionError("write attempted on blocklisted payload"))

    with patch.object(audit_writer.settings, "audit_db_url", "postgresql://x:y@host/db"):
        with patch.object(audit_writer, "_get_pool", fake_pool_get):
            await audit_writer.emit(event)

    fake_pool_get.assert_not_called()


@pytest.mark.asyncio
async def test_emit_accepts_clean_structured_detail() -> None:
    """A typical ``failure_class``-only payload MUST pass validation and INSERT."""
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=None)
    fake_pool = MagicMock()

    class _AcquireCM:
        async def __aenter__(self) -> Any:
            return fake_conn

        async def __aexit__(self, *a: Any) -> None:
            return None

    fake_pool.acquire = MagicMock(return_value=_AcquireCM())

    event = AuditEvent(
        event_type="tool_call",
        tool_name="get_patient_briefing",
        outcome="success",
        duration_ms=42,
        detail_json={"failure_class": None, "ambiguous_name": False},
    )

    with patch.object(audit_writer.settings, "audit_db_url", "postgresql://x:y@host/db"):
        with patch.object(audit_writer, "_get_pool", AsyncMock(return_value=fake_pool)):
            await audit_writer.emit(event)

    fake_conn.execute.assert_awaited_once()


# ── Validator unit (defensive table) ─────────────────────────────────────────


def test_validator_passes_empty_detail() -> None:
    ok, reason = audit_writer._validate_detail({})
    assert ok is True
    assert reason is None


def test_validator_rejects_oversized() -> None:
    ok, reason = audit_writer._validate_detail({"blob": "x" * 4096})
    assert ok is False
    assert reason == "detail_too_large"


@pytest.mark.parametrize("word", ["Vancomycin", "patient", "diagnosis"])
def test_validator_rejects_blocklisted(word: str) -> None:
    ok, reason = audit_writer._validate_detail({"note": word})
    assert ok is False
    assert reason is not None and reason.startswith("detail_blocklist:")
