"""Verify ``get_census_summary`` schedules briefing warmers in the background.

The fan-out must:
  * call ``warm_briefing_for_patient`` once per surviving census patient,
  * be scheduled via ``asyncio.create_task`` (fire-and-forget — the census
    return must NOT await briefing generation),
  * be bounded so we never warm before ``get_census_summary`` returns.

Together these guarantee that opening the panel does the expensive briefing
work in the background, so a subsequent "Brief X" click hits Redis (~50 ms)
instead of paying the ~26 s cold-LLM cost.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.tools as tools_module
from agent.tools import get_census_summary
from triage.census import CensusBuildResult, CensusEntry

pytestmark = pytest.mark.hard_failure


def _entry(pid: str) -> CensusEntry:
    return CensusEntry(
        patient_id=pid,
        name=f"Patient {pid}",
        mrn="MR-1",
        openemr_pid="1",
        triage_level=2,
        triage_label="moderate",
        triage_description="moderate severity",
        matched_criteria={},
        vitals_summary={},
    )


def test_get_census_summary_fans_out_briefing_warm_without_awaiting() -> None:
    patient_ids = [f"pt-{i:03d}" for i in range(1, 4)]
    entries = [_entry(pid) for pid in patient_ids]

    # Track that each warmer is *scheduled* but not awaited inline.
    started: list[str] = []
    finished: list[str] = []
    call_count = {"n": 0}
    block_holder: dict[str, asyncio.Event] = {}

    async def warm_mock_target(redis_client: Any, pid: str, langfuse: Any | None = None) -> None:
        call_count["n"] += 1
        started.append(pid)
        # Block until the test releases us. This proves the census did NOT
        # await briefing completion — if it had, the call would deadlock.
        await block_holder["block"].wait()
        finished.append(pid)

    async def fake_explain(annotated: Any, langfuse: Any | None = None, redis_client: Any | None = None) -> list[dict[str, Any]]:
        return [
            {
                "patient_id": e.patient_id,
                "triage_level": e.triage_level,
                "triage_label": e.triage_label,
                "matched_criteria": {},
                "explanation": "ok",
            }
            for e in entries
        ]

    async def fake_build_census(*args: Any, **kwargs: Any) -> CensusBuildResult:
        return CensusBuildResult(verified=entries, dropped_ids=[])

    redis_client = MagicMock(name="redis_client")

    fhir_mock = MagicMock()
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value={"resources": {}})

    async def run() -> dict[str, Any]:
        block_holder["block"] = asyncio.Event()
        with patch.object(tools_module, "build_census", side_effect=fake_build_census), \
                patch.object(tools_module, "explain_census", side_effect=fake_explain), \
                patch.object(tools_module, "fhir_client", fhir_mock), \
                patch.object(tools_module, "warm_briefing_for_patient", new=warm_mock_target) as _wm, \
                patch.object(tools_module, "_get_cached_bundle", new=AsyncMock(return_value={"resources": {}})), \
                patch.object(tools_module, "_set_cached_bundle", new=AsyncMock(return_value=None)), \
                patch("agent.tools.verify_triage_entry", side_effect=lambda e, b: e):
            result = await get_census_summary(
                {"provider_id": "prov-1", "patient_ids": patient_ids},
                {"redis_client": redis_client, "session_id": "sess-1", "request_id": "req-1"},
            )

            # Census returned — briefing tasks must NOT have completed yet
            # (fake_warm blocks on `block` until we release it below).
            assert finished == [], \
                "Briefing warmers must NOT have completed before census returns"

            # Yield enough times for the bounded warmers to start. Semaphore
            # caps concurrency at 4, so all 3 patients should be in-flight
            # after a couple of loop iterations.
            for _ in range(10):
                await asyncio.sleep(0)
                if len(started) == len(patient_ids):
                    break

            assert call_count["n"] == len(patient_ids), \
                f"warm_briefing_for_patient should be scheduled once per patient, got {call_count['n']}"
            assert sorted(started) == sorted(patient_ids)
            assert finished == [], "Warmers should still be blocked"

            # Release the warmers and drain background tasks for clean teardown.
            block_holder["block"].set()
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            return result

    result = asyncio.run(run())

    # All warmers ran by the time we drained the loop.
    assert sorted(started) == sorted(patient_ids)
    assert sorted(finished) == sorted(patient_ids)

    # Each warmer received the same redis client we passed in.
    assert result["result"]["total"] == len(patient_ids)


def test_get_census_summary_skips_warm_when_redis_is_none() -> None:
    patient_ids = ["pt-001"]
    entries = [_entry("pt-001")]

    async def fake_explain(annotated: Any, langfuse: Any | None = None, redis_client: Any | None = None) -> list[dict[str, Any]]:
        return [
            {
                "patient_id": "pt-001",
                "triage_level": 2,
                "triage_label": "moderate",
                "matched_criteria": {},
                "explanation": "ok",
            }
        ]

    async def fake_build_census(*args: Any, **kwargs: Any) -> CensusBuildResult:
        return CensusBuildResult(verified=entries, dropped_ids=[])

    fhir_mock = MagicMock()
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value={"resources": {}})

    async def run() -> None:
        with patch.object(tools_module, "build_census", side_effect=fake_build_census), \
                patch.object(tools_module, "explain_census", side_effect=fake_explain), \
                patch.object(tools_module, "fhir_client", fhir_mock), \
                patch.object(tools_module, "warm_briefing_for_patient", new=AsyncMock()) as warm_mock, \
                patch("agent.tools.verify_triage_entry", side_effect=lambda e, b: e):
            await get_census_summary(
                {"provider_id": "prov-1", "patient_ids": patient_ids},
                {"redis_client": None, "session_id": "sess-2"},
            )
            # No redis ⇒ no warmers scheduled (per the docstring contract).
            assert warm_mock.call_count == 0

    asyncio.run(run())
