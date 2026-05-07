"""Unit tests for ``staging.store`` (Phase 9 Slice 9.3)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests._staging_fakes import make_fake_pool

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


# ── Fixture: monkeypatch the audit pool to our in-memory fake ────────────────


@pytest.fixture()
def staging_state(monkeypatch: pytest.MonkeyPatch):
    pool, state = make_fake_pool()

    async def _fake_get_pool() -> Any:
        return pool

    async def _fake_emit(event: Any) -> None:
        state.audit_events.append(
            {
                "event_type": event.event_type,
                "outcome": event.outcome,
                "detail_json": dict(event.detail_json or {}),
            }
        )

    from audit import writer as audit_writer

    monkeypatch.setattr(audit_writer, "get_pool", _fake_get_pool)
    monkeypatch.setattr(audit_writer, "emit", _fake_emit)
    return state


# ── Helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _stage(state, *, target_id: str = "copilot-1-2089-1") -> int:
    from staging import store

    return _run(
        store.stage_pending(
            document_reference_id="copilot:1",
            file_batch_id="00000000-0000-0000-0000-000000000001",
            patient_id="p-1",
            source_format="hl7",
            target_resource_type="Observation",
            target_resource_id=target_id,
            payload={"id": target_id, "resourceType": "Observation"},
        )
    )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_stage_then_approve_then_written(staging_state) -> None:
    from staging import store

    pid = _stage(staging_state)
    approved = _run(store.approve(pid, "alice"))
    assert approved.state == "approved"

    _run(store.mark_written(pid))
    row = _run(store.get_pending(pid))
    assert row is not None and row["state"] == "written"

    types = [e["event_type"] for e in staging_state.audit_events]
    assert "extraction_staged" in types
    assert "extraction_approved" in types
    assert "extraction_written" in types


def test_stage_then_approve_then_failed(staging_state) -> None:
    from staging import store

    pid = _stage(staging_state)
    _run(store.approve(pid, "bob"))
    _run(store.mark_failed(pid, "php_5xx"))
    row = _run(store.get_pending(pid))
    assert row is not None and row["state"] == "failed"
    assert row["write_error"] == "php_5xx"

    fail_evt = next(
        e for e in staging_state.audit_events if e["event_type"] == "extraction_write_failed"
    )
    assert fail_evt["detail_json"]["write_error"] == "php_5xx"


def test_stage_then_reject(staging_state) -> None:
    from staging import store

    pid = _stage(staging_state)
    out = _run(store.reject(pid, "alice", "wrong patient"))
    assert out["state"] == "rejected"

    rej = next(
        e for e in staging_state.audit_events if e["event_type"] == "extraction_rejected"
    )
    assert rej["detail_json"]["reason_chars"] == len("wrong patient")


def test_reject_blocks_approve(staging_state) -> None:
    from staging import store

    pid = _stage(staging_state)
    _run(store.reject(pid, "alice", "no"))
    with pytest.raises(store.StagingError) as ei:
        _run(store.approve(pid, "alice"))
    assert ei.value.code == "conflict"


def test_reject_reason_length_cap(staging_state) -> None:
    from staging import store

    pid = _stage(staging_state)
    too_long = "x" * (store.REJECT_REASON_MAX_CHARS + 1)
    with pytest.raises(store.StagingError) as ei:
        _run(store.reject(pid, "alice", too_long))
    assert ei.value.code == "invalid_reason"


def test_retry_only_from_failed(staging_state) -> None:
    from staging import store

    pid = _stage(staging_state)
    # Pending → retry should fail.
    with pytest.raises(store.StagingError) as ei:
        _run(store.retry(pid))
    assert ei.value.code == "conflict"

    _run(store.approve(pid, "alice"))
    _run(store.mark_failed(pid, "network_timeout"))

    out = _run(store.retry(pid))
    assert out["state"] == "approved"
    assert out["retry_count"] == 1


def test_retry_max_cap(staging_state) -> None:
    from staging import store

    pid = _stage(staging_state)
    for i in range(store.MAX_RETRIES):
        _run(store.approve(pid, "alice")) if i == 0 else None
        # After first approve, walk through fail → retry until cap.
        _run(store.mark_failed(pid, "php_5xx"))
        _run(store.retry(pid))
    # We've now done MAX_RETRIES retries. State is 'approved' with retry_count == MAX.
    _run(store.mark_failed(pid, "php_5xx"))
    with pytest.raises(store.StagingError) as ei:
        _run(store.retry(pid))
    assert ei.value.code == "conflict"


def test_partial_unique_updates_in_place_while_pending(staging_state) -> None:
    """Stage twice with same target_resource_id while pending → updates in place."""
    pid_1 = _stage(staging_state, target_id="copilot-1-x")
    pid_2 = _stage(staging_state, target_id="copilot-1-x")
    assert pid_1 == pid_2


def test_partial_unique_allows_new_row_after_terminal(staging_state) -> None:
    """Stage same target after row went terminal → new row inserted."""
    from staging import store

    pid_1 = _stage(staging_state, target_id="copilot-1-y")
    _run(store.approve(pid_1, "alice"))
    _run(store.mark_written(pid_1))

    # Now stage again — should insert a fresh row (terminal-state row stays).
    pid_2 = _stage(staging_state, target_id="copilot-1-y")
    assert pid_2 != pid_1


def test_list_pending_filters_by_state(staging_state) -> None:
    from staging import store

    pid_a = _stage(staging_state, target_id="copilot-1-a")
    pid_b = _stage(staging_state, target_id="copilot-1-b")
    _run(store.reject(pid_a, "alice", "no"))

    rows = _run(store.list_pending(patient_id="p-1", state="pending"))
    ids = {r["id"] for r in rows}
    assert pid_b in ids and pid_a not in ids

    rows_rej = _run(store.list_pending(patient_id="p-1", state="rejected"))
    assert any(r["id"] == pid_a for r in rows_rej)


def test_get_pending_returns_none_for_missing(staging_state) -> None:
    from staging import store

    assert _run(store.get_pending(99999)) is None
