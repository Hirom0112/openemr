"""Watchdog tests for the Slice 9.3 staging subsystem."""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any

import pytest

from tests._staging_fakes import make_fake_pool

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def state(monkeypatch: pytest.MonkeyPatch):
    pool, st = make_fake_pool()

    async def _fake_get_pool() -> Any:
        return pool

    async def _fake_emit(event: Any) -> None:
        st.audit_events.append(
            {
                "event_type": event.event_type,
                "outcome": event.outcome,
                "detail_json": dict(event.detail_json or {}),
            }
        )

    from audit import writer as audit_writer

    monkeypatch.setattr(audit_writer, "get_pool", _fake_get_pool)
    monkeypatch.setattr(audit_writer, "emit", _fake_emit)
    return st


def _stage_and_approve(*, target_id: str = "copilot-1-w") -> int:
    from staging import store

    pid = _run(
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
    _run(store.approve(pid, "alice"))
    return pid


def test_stuck_approved_reaper_picks_up_old_rows(
    state, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _stage_and_approve()
    # Time-travel: backdate decided_at by 10 min so the reaper picks it up.
    state.rows[pid]["decided_at"] = _dt.datetime.now(
        _dt.timezone.utc
    ) - _dt.timedelta(minutes=10)

    from observations import writer as obs_writer

    async def _ok(row: dict[str, Any]):
        return ("written", None)

    monkeypatch.setattr(obs_writer, "_perform_write", _ok)

    from staging import watchdog

    counts = _run(watchdog.run_stuck_approved_reaper_once())
    assert counts["picked_up"] == 1
    assert counts["written"] == 1
    assert state.rows[pid]["state"] == "written"


def test_stuck_approved_reaper_records_orphan_failure(
    state, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _stage_and_approve(target_id="copilot-1-orphan")
    state.rows[pid]["decided_at"] = _dt.datetime.now(
        _dt.timezone.utc
    ) - _dt.timedelta(minutes=10)

    from observations import writer as obs_writer

    async def _bad(row: dict[str, Any]):
        return ("failed", "network_timeout")

    monkeypatch.setattr(obs_writer, "_perform_write", _bad)

    from staging import watchdog

    counts = _run(watchdog.run_stuck_approved_reaper_once())
    assert counts["failed"] == 1
    assert state.rows[pid]["state"] == "failed"
    assert state.rows[pid]["write_error"].startswith("watchdog_recovered_orphan:")


def test_stuck_approved_reaper_skips_fresh_rows(
    state, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recently-approved rows (under the 5-minute TTL) should not be touched."""
    pid = _stage_and_approve(target_id="copilot-1-fresh")

    from observations import writer as obs_writer

    async def _ok(row: dict[str, Any]):
        return ("written", None)

    monkeypatch.setattr(obs_writer, "_perform_write", _ok)

    from staging import watchdog

    counts = _run(watchdog.run_stuck_approved_reaper_once())
    assert counts["picked_up"] == 0
    assert state.rows[pid]["state"] == "approved"


def test_stale_pending_notifier_alerts_after_7d(state) -> None:
    from staging import store, watchdog

    pid = _run(
        store.stage_pending(
            document_reference_id="copilot:1",
            file_batch_id="00000000-0000-0000-0000-000000000002",
            patient_id="p-1",
            source_format="docx",
            target_resource_type="Observation",
            target_resource_id="copilot-1-stale-soft",
            payload={"id": "copilot-1-stale-soft", "resourceType": "Observation"},
        )
    )
    state.rows[pid]["staged_at"] = _dt.datetime.now(
        _dt.timezone.utc
    ) - _dt.timedelta(days=10)

    counts = _run(watchdog.run_stale_pending_notifier_once())
    assert counts["alerted"] == 1
    assert counts["auto_rejected"] == 0
    assert state.rows[pid]["state"] == "pending"
    alert_evt = next(
        e
        for e in state.audit_events
        if e["event_type"] == "extraction_staged" and e["detail_json"].get("stale")
    )
    assert alert_evt["detail_json"]["watchdog_age_days"] >= 7


def test_stale_pending_notifier_auto_rejects_after_30d(state) -> None:
    from staging import store, watchdog

    pid = _run(
        store.stage_pending(
            document_reference_id="copilot:1",
            file_batch_id="00000000-0000-0000-0000-000000000003",
            patient_id="p-1",
            source_format="docx",
            target_resource_type="Observation",
            target_resource_id="copilot-1-stale-hard",
            payload={"id": "copilot-1-stale-hard", "resourceType": "Observation"},
        )
    )
    state.rows[pid]["staged_at"] = _dt.datetime.now(
        _dt.timezone.utc
    ) - _dt.timedelta(days=45)

    counts = _run(watchdog.run_stale_pending_notifier_once())
    assert counts["auto_rejected"] == 1
    assert state.rows[pid]["state"] == "rejected"
    assert "auto-rejected" in (state.rows[pid]["write_error"] or "")


def test_watchdog_start_stop_idempotent(state) -> None:
    """``start_watchdog`` is idempotent and ``stop_watchdog`` always returns."""
    from staging import watchdog

    async def _go():
        watchdog.start_watchdog()
        watchdog.start_watchdog()  # second call no-ops
        await watchdog.stop_watchdog()
        # Restartable.
        watchdog.start_watchdog()
        await watchdog.stop_watchdog()

    asyncio.get_event_loop().run_until_complete(_go())
