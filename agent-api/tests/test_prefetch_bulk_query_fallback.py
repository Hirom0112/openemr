"""Verify ``/agent/prefetch`` falls back to per-patient warming when the
FHIR bulk Patient query (``Patient?_count=200&_sort=_id``) errors out.

Without this fallback, a 500 from the bulk Patient search inside
``build_census`` causes ``_warm()`` to bail before scheduling any
``warm_bundle_for_patient`` / ``warm_briefing_for_patient`` calls — every
briefing cache stays cold and the panel pays the full ~26 s cold-LLM cost
for the first click.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

pytestmark = pytest.mark.hard_failure


@pytest.mark.hard_failure
def test_prefetch_falls_back_to_per_patient_warm_when_bulk_query_fails() -> None:
    from main import app
    from agent.metrics import agent_prewarm_runs_total

    patient_ids: list[str] = ["pt-001", "pt-002", "pt-003"]

    bulk_failure = RuntimeError(
        "Server error '500 Internal Server Error' for url "
        "'http://openemr/apis/default/fhir/Patient?_count=200&_sort=_id'"
    )

    bundle_warm = AsyncMock(return_value=None)
    briefing_warm = AsyncMock(return_value=None)
    med_warm = AsyncMock(return_value=None)
    build_census_mock = AsyncMock(side_effect=bulk_failure)

    before = agent_prewarm_runs_total.labels(
        outcome="bulk_query_failed_fallback"
    )._value.get()  # type: ignore[attr-defined]

    with patch("main.build_census", build_census_mock), \
            patch("main.warm_bundle_for_patient", bundle_warm), \
            patch("main.warm_briefing_for_patient", briefing_warm), \
            patch("main.warm_medication_safety_for_patient", med_warm):
        client = TestClient(app)
        resp = client.post(
            "/agent/prefetch",
            json={
                "session_id": "sess-prefetch-fallback",
                "provider_id": "prov-1",
                "patient_ids": patient_ids,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "acknowledged"

        # _warm() runs as a background task — drain pending tasks so the
        # assertions below see the fan-out completing.
        async def _drain() -> None:
            for _ in range(50):
                pending = [
                    t
                    for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()
                ]
                if not pending:
                    return
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_drain())

    # build_census raised once; the fallback should have scheduled the
    # per-patient warmers for every patient_id passed in by the client.
    assert build_census_mock.await_count == 1
    assert bundle_warm.await_count == len(patient_ids)
    assert briefing_warm.await_count == len(patient_ids)

    bundle_pids = sorted(call.args[1] for call in bundle_warm.await_args_list)
    briefing_pids = sorted(call.args[1] for call in briefing_warm.await_args_list)
    assert bundle_pids == sorted(patient_ids)
    assert briefing_pids == sorted(patient_ids)

    after = agent_prewarm_runs_total.labels(
        outcome="bulk_query_failed_fallback"
    )._value.get()  # type: ignore[attr-defined]
    assert after - before == 1, (
        "fallback path must increment "
        "agent_prewarm_runs_total{outcome='bulk_query_failed_fallback'}"
    )


@pytest.mark.hard_failure
def test_prefetch_force_refresh_threads_through_when_gate_enabled() -> None:
    """When the server-side gate is on, force_refresh=true cascades all warmers."""
    from main import app
    import config as config_module

    patient_ids: list[str] = ["pt-001", "pt-002"]

    bundle_warm = AsyncMock(return_value=None)
    briefing_warm = AsyncMock(return_value=None)
    med_warm = AsyncMock(return_value=None)

    class _Verified:
        def __init__(self, pid: str) -> None:
            self.patient_id = pid

    class _CensusResult:
        def __init__(self) -> None:
            self.verified = [_Verified(p) for p in patient_ids]
            self.dropped_ids: list[str] = []

    build_census_mock = AsyncMock(return_value=_CensusResult())

    original_gate = config_module.settings.prefetch_force_refresh_on_login
    config_module.settings.prefetch_force_refresh_on_login = True
    try:
        with patch("main.build_census", build_census_mock), \
                patch("main.warm_bundle_for_patient", bundle_warm), \
                patch("main.warm_briefing_for_patient", briefing_warm), \
                patch("main.warm_medication_safety_for_patient", med_warm):
            client = TestClient(app)
            resp = client.post(
                "/agent/prefetch",
                json={
                    "session_id": "sess-force",
                    "provider_id": "prov-1",
                    "patient_ids": patient_ids,
                    "force_refresh": True,
                },
            )
            assert resp.status_code == 200, resp.text

            async def _drain() -> None:
                for _ in range(50):
                    pending = [
                        t for t in asyncio.all_tasks()
                        if t is not asyncio.current_task() and not t.done()
                    ]
                    if not pending:
                        return
                    await asyncio.gather(*pending, return_exceptions=True)

            asyncio.run(_drain())
    finally:
        config_module.settings.prefetch_force_refresh_on_login = original_gate

    # build_census must have been called with force_refresh=True
    assert build_census_mock.await_count == 1
    assert build_census_mock.await_args.kwargs.get("force_refresh") is True

    # All three per-patient warmers fired with force_refresh=True
    assert bundle_warm.await_count == len(patient_ids)
    assert briefing_warm.await_count == len(patient_ids)
    assert med_warm.await_count == len(patient_ids)
    for call in bundle_warm.await_args_list:
        assert call.kwargs.get("force_refresh") is True
    for call in briefing_warm.await_args_list:
        assert call.kwargs.get("force_refresh") is True
    for call in med_warm.await_args_list:
        assert call.kwargs.get("force_refresh") is True


@pytest.mark.hard_failure
def test_prefetch_force_refresh_downgraded_when_gate_disabled() -> None:
    """When the gate is off, client force_refresh=true is silently ignored."""
    from main import app
    import config as config_module

    patient_ids: list[str] = ["pt-001"]

    bundle_warm = AsyncMock(return_value=None)
    briefing_warm = AsyncMock(return_value=None)
    med_warm = AsyncMock(return_value=None)

    class _Verified:
        def __init__(self, pid: str) -> None:
            self.patient_id = pid

    class _CensusResult:
        def __init__(self) -> None:
            self.verified = [_Verified(p) for p in patient_ids]
            self.dropped_ids: list[str] = []

    build_census_mock = AsyncMock(return_value=_CensusResult())

    original_gate = config_module.settings.prefetch_force_refresh_on_login
    config_module.settings.prefetch_force_refresh_on_login = False
    try:
        with patch("main.build_census", build_census_mock), \
                patch("main.warm_bundle_for_patient", bundle_warm), \
                patch("main.warm_briefing_for_patient", briefing_warm), \
                patch("main.warm_medication_safety_for_patient", med_warm):
            client = TestClient(app)
            resp = client.post(
                "/agent/prefetch",
                json={
                    "session_id": "sess-no-force",
                    "provider_id": "prov-1",
                    "patient_ids": patient_ids,
                    "force_refresh": True,  # client asks
                },
            )
            assert resp.status_code == 200, resp.text

            async def _drain() -> None:
                for _ in range(50):
                    pending = [
                        t for t in asyncio.all_tasks()
                        if t is not asyncio.current_task() and not t.done()
                    ]
                    if not pending:
                        return
                    await asyncio.gather(*pending, return_exceptions=True)

            asyncio.run(_drain())
    finally:
        config_module.settings.prefetch_force_refresh_on_login = original_gate

    # Gate disabled → force_refresh downgraded to False on every call
    assert build_census_mock.await_args.kwargs.get("force_refresh") is False
    for call in bundle_warm.await_args_list:
        assert call.kwargs.get("force_refresh") is False
    for call in briefing_warm.await_args_list:
        assert call.kwargs.get("force_refresh") is False
    for call in med_warm.await_args_list:
        assert call.kwargs.get("force_refresh") is False
    # And medication-safety warmer is now part of the standard fan-out.
    assert med_warm.await_count == len(patient_ids)
