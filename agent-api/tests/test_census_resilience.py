"""Resilience tests for triage.census.build_census.

Covers the Sara-Chen-panel-oscillation bug:
  1. Transient 401s on individual patients are retried, with the token
     cache invalidated before the second attempt.
  2. The pre-fanout token refresh is invoked exactly once per call.
  3. The fan-out semaphore caps concurrent _build_entry tasks at 4.
  4. Patients that fail both attempts are surfaced via dropped_ids
     (so the response shape exposes the loss instead of hiding it).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from triage import census as census_module
from triage.census import CensusBuildResult, build_census

pytestmark = pytest.mark.hard_failure


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.get_event_loop().run_until_complete(coro)


def _patient(pid: str) -> dict[str, Any]:
    return {
        "id": pid,
        "name": [{"given": ["Test"], "family": pid}],
        "identifier": [{"system": "http://x/pid", "value": "1"}],
    }


def _bundle() -> dict[str, Any]:
    return {"resources": {"Observation": [], "Encounter": []}}


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://fhir/Patient/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"status {status}", request=request, response=response)


def test_transient_401s_are_retried_and_dropped_ids_populated() -> None:
    patient_ids = [f"pt-{i:03d}" for i in range(1, 6)]
    failing = {"pt-002", "pt-004"}

    get_patient_calls: dict[str, int] = {pid: 0 for pid in patient_ids}

    async def fake_get_patient(pid: str) -> dict[str, Any]:
        get_patient_calls[pid] += 1
        if pid in failing:
            raise _http_error(401)
        return _patient(pid)

    async def fake_get_bundle(pid: str) -> dict[str, Any]:
        return _bundle()

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(side_effect=fake_get_patient)
    fhir_mock.get_bundle_for_patient = AsyncMock(side_effect=fake_get_bundle)

    with patch.object(census_module, "fhir_client", fhir_mock), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")), \
            patch.object(census_module, "invalidate_token_cache") as invalidate_mock:
        result = _run(build_census(patient_ids))

    assert isinstance(result, CensusBuildResult)
    assert len(result.verified) == 3
    assert sorted(result.dropped_ids) == ["pt-002", "pt-004"]

    # Each failing patient should have been attempted twice (1 retry).
    assert get_patient_calls["pt-002"] == 2
    assert get_patient_calls["pt-004"] == 2
    # On 401 the token cache must be invalidated before each retry.
    assert invalidate_mock.call_count == 2


def test_token_is_prefetched_once_before_fanout() -> None:
    patient_ids = ["pt-001", "pt-002", "pt-003"]

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(side_effect=lambda pid: _patient(pid))
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    token_mock = AsyncMock(return_value="tok")
    with patch.object(census_module, "fhir_client", fhir_mock), \
            patch.object(census_module, "get_access_token", new=token_mock):
        result = _run(build_census(patient_ids))

    assert token_mock.call_count == 1
    assert len(result.verified) == 3
    assert result.dropped_ids == []


def test_semaphore_caps_inflight_at_four() -> None:
    patient_ids = [f"pt-{i:03d}" for i in range(1, 11)]

    inflight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_get_patient(pid: str) -> dict[str, Any]:
        nonlocal inflight, peak
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.02)
            return _patient(pid)
        finally:
            async with lock:
                inflight -= 1

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(side_effect=fake_get_patient)
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    with patch.object(census_module, "fhir_client", fhir_mock), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")):
        result = _run(build_census(patient_ids))

    assert peak <= census_module._CENSUS_FANOUT_CONCURRENCY == 4
    assert len(result.verified) == 10


def test_non_retryable_status_is_not_retried() -> None:
    patient_ids = ["pt-001", "pt-002"]
    calls: dict[str, int] = {pid: 0 for pid in patient_ids}

    async def fake_get_patient(pid: str) -> dict[str, Any]:
        calls[pid] += 1
        if pid == "pt-002":
            raise _http_error(404)
        return _patient(pid)

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(side_effect=fake_get_patient)
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    with patch.object(census_module, "fhir_client", fhir_mock), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")):
        result = _run(build_census(patient_ids))

    assert calls["pt-002"] == 1
    assert result.dropped_ids == ["pt-002"]
    assert len(result.verified) == 1
