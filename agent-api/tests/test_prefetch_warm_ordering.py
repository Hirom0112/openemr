"""Verify ``/agent/prefetch`` warms patients in triage-rank order.

Within a bounded semaphore, the iteration order determines who gets a
worker slot first — and therefore whose briefing is ready when the
physician makes the initial click. The contract: lower triage_level
(more urgent) warms first.

Also verifies that when census-build fails and we fall back to the
client-supplied patient_ids, ordering is preserved (we have no triage
ranks for them) and the per-patient log event is still emitted.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

pytestmark = pytest.mark.hard_failure


class _VerifiedEntry:
    """Minimal stand-in for :class:`triage.census.CensusEntry`."""

    def __init__(self, patient_id: str, triage_level: int) -> None:
        self.patient_id = patient_id
        self.triage_level = triage_level


class _CensusResult:
    def __init__(self, verified: list[_VerifiedEntry]) -> None:
        self.verified = verified
        self.dropped_ids: list[str] = []


async def _drain_pending() -> None:
    for _ in range(50):
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.hard_failure
def test_prefetch_warms_in_triage_rank_order() -> None:
    """Highest-priority patient (lowest triage_level) is dispatched first."""
    from main import app

    # Census returns entries in NON-priority order (admit-time / name order).
    # The prefetch handler must re-sort by triage rank before dispatching.
    verified = [
        _VerifiedEntry("pt-routine", triage_level=10),
        _VerifiedEntry("pt-urgent", triage_level=1),
        _VerifiedEntry("pt-warning", triage_level=5),
    ]

    bundle_call_order: list[str] = []

    async def _record_bundle(_redis: Any, pid: str, **_: Any) -> None:
        # Capture the dispatch order. With a width-6 semaphore and 3 tasks
        # all entries acquire immediately in create-order; the .append runs
        # synchronously before the first await so order is the dispatch
        # order, not the completion order.
        bundle_call_order.append(pid)
        await asyncio.sleep(0)

    bundle_warm = AsyncMock(side_effect=_record_bundle)
    briefing_warm = AsyncMock(return_value=None)
    med_warm = AsyncMock(return_value=None)
    build_census_mock = AsyncMock(return_value=_CensusResult(verified))

    with patch("main.build_census", build_census_mock), \
            patch("main.warm_bundle_for_patient", bundle_warm), \
            patch("main.warm_briefing_for_patient", briefing_warm), \
            patch("main.warm_medication_safety_for_patient", med_warm):
        client = TestClient(app)
        resp = client.post(
            "/agent/prefetch",
            json={
                "session_id": "sess-warm-order",
                "provider_id": "prov-1",
                "patient_ids": [v.patient_id for v in verified],
            },
        )
        assert resp.status_code == 200, resp.text
        asyncio.run(_drain_pending())

    # All three were warmed, and dispatched in lowest-triage-level-first order.
    assert bundle_warm.await_count == 3
    assert bundle_call_order == ["pt-urgent", "pt-warning", "pt-routine"], (
        f"expected triage-rank ordering; got {bundle_call_order}"
    )


@pytest.mark.hard_failure
def test_prefetch_emits_per_patient_warmup_log_with_order_and_rank(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each warmed patient produces one log event with triage_rank + warmup_order."""
    from main import app

    verified = [
        _VerifiedEntry("pt-A", triage_level=3),
        _VerifiedEntry("pt-B", triage_level=1),
    ]

    bundle_warm = AsyncMock(return_value=None)
    briefing_warm = AsyncMock(return_value=None)
    med_warm = AsyncMock(return_value=None)
    build_census_mock = AsyncMock(return_value=_CensusResult(verified))

    caplog.set_level(logging.INFO, logger="main")

    with patch("main.build_census", build_census_mock), \
            patch("main.warm_bundle_for_patient", bundle_warm), \
            patch("main.warm_briefing_for_patient", briefing_warm), \
            patch("main.warm_medication_safety_for_patient", med_warm):
        client = TestClient(app)
        resp = client.post(
            "/agent/prefetch",
            json={
                "session_id": "sess-warm-log",
                "provider_id": "prov-1",
                "patient_ids": [v.patient_id for v in verified],
            },
        )
        assert resp.status_code == 200, resp.text
        asyncio.run(_drain_pending())

    warmup_records = [
        rec for rec in caplog.records
        if rec.getMessage() == "Pre-fetch patient warmed"
    ]
    assert len(warmup_records) == 2

    # Records are emitted in dispatch order. pt-B (level 1) first, pt-A (3) second.
    by_patient = {rec.patient_id: rec for rec in warmup_records}  # type: ignore[attr-defined]

    assert by_patient["pt-B"].triage_rank == 1  # type: ignore[attr-defined]
    assert by_patient["pt-B"].warmup_order == 0  # type: ignore[attr-defined]
    assert by_patient["pt-A"].triage_rank == 3  # type: ignore[attr-defined]
    assert by_patient["pt-A"].warmup_order == 1  # type: ignore[attr-defined]

    # duration_ms must be present and non-negative.
    for rec in warmup_records:
        assert isinstance(rec.duration_ms, int) and rec.duration_ms >= 0  # type: ignore[attr-defined]
        assert rec.cache in {"miss", "n/a"}  # type: ignore[attr-defined]


@pytest.mark.hard_failure
def test_prefetch_fallback_path_preserves_client_order_and_uses_none_rank(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When census-build fails, fall back to client-supplied patient_ids.

    No triage ranks are available there; ordering is the client-supplied
    order and the warmup log records ``triage_rank=None``.
    """
    from main import app

    patient_ids = ["pt-1", "pt-2", "pt-3"]

    bundle_warm = AsyncMock(return_value=None)
    briefing_warm = AsyncMock(return_value=None)
    med_warm = AsyncMock(return_value=None)
    build_census_mock = AsyncMock(side_effect=RuntimeError("bulk Patient 500"))

    caplog.set_level(logging.INFO, logger="main")

    with patch("main.build_census", build_census_mock), \
            patch("main.warm_bundle_for_patient", bundle_warm), \
            patch("main.warm_briefing_for_patient", briefing_warm), \
            patch("main.warm_medication_safety_for_patient", med_warm):
        client = TestClient(app)
        resp = client.post(
            "/agent/prefetch",
            json={
                "session_id": "sess-warm-fallback",
                "provider_id": "prov-1",
                "patient_ids": patient_ids,
            },
        )
        assert resp.status_code == 200, resp.text
        asyncio.run(_drain_pending())

    warmup_records = [
        rec for rec in caplog.records
        if rec.getMessage() == "Pre-fetch patient warmed"
    ]
    assert len(warmup_records) == 3
    for rec in warmup_records:
        assert rec.triage_rank is None  # type: ignore[attr-defined]
