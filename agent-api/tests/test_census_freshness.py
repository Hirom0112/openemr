"""Tests for the census freshness/refresh path.

Covers the bug where the census header showed wall-clock time while the
briefing pulled from the same data showed a 17-minute-older "Data as of"
timestamp because of the 15-minute census cache.

  1. A cold (cache-miss) census stamps generated_at to current UTC time.
  2. A warm (cache-hit) census returns the ORIGINAL generated_at — not the
     cache-read time. (Without this, the field would be meaningless.)
  3. force_refresh=True bypasses the cache READ and produces a fresh
     generated_at.
  4. After a force_refresh the cache is rewritten with the fresh value, so
     the next non-forced read returns the new time.
  5. The /triage/census endpoint plumbs force_refresh through to the tool.
  6. Legacy cached payloads (bare list of entry dicts, pre-generated_at)
     still deserialise — generated_at falls back to None so the UI shows
     "Census · Refresh" without a time instead of crashing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx  # noqa: F401  — keeps test isolation surface symmetric with siblings
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from triage import census as census_module
from triage.census import CensusBuildResult, build_census, census_cache_key

pytestmark = pytest.mark.hard_failure


def _run(coro: Any) -> Any:  # noqa: ANN401
    # Use a fresh loop per call. asyncio.get_event_loop() returns a closed
    # loop in some sibling tests' wake (test_briefing_force_refresh closes
    # the loop via TestClient teardown), which raises RuntimeError on the
    # next run_until_complete. Constructing a new loop each time isolates
    # this module from the suite-wide loop lifecycle.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # Drain any pending tasks (briefing warmer fan-out scheduled via
        # asyncio.create_task) before closing so we don't leak warnings.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def _patient(pid: str) -> dict[str, Any]:
    return {
        "id": pid,
        "name": [{"given": ["Test"], "family": pid}],
        "identifier": [{"system": "http://x/pid", "value": "1"}],
    }


def _bundle() -> dict[str, Any]:
    return {"resources": {"Observation": [], "Encounter": []}}


def _fhir_mock() -> MagicMock:
    fhir = MagicMock()
    fhir.get_patient = AsyncMock(side_effect=lambda pid: _patient(pid))
    fhir.get_bundle_for_patient = AsyncMock(return_value=_bundle())
    return fhir


def test_cold_census_stamps_generated_at() -> None:
    """A cache-miss build returns a non-empty ISO timestamp."""
    fhir = _fhir_mock()
    with patch.object(census_module, "fhir_client", fhir), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")):
        result = _run(build_census(["pt-001"]))

    assert isinstance(result, CensusBuildResult)
    assert result.generated_at is not None
    assert "T" in result.generated_at  # ISO-8601


def test_warm_census_preserves_original_generated_at() -> None:
    """A cache HIT returns the ORIGINAL generated_at, not the read time.

    Without this, the freshness indicator is meaningless — it would always
    show the current time even on a 15-min-old cached payload.
    """
    cache_key = census_cache_key("system", ["pt-001"])

    storage: dict[str, str] = {}

    async def fake_get(key: str) -> str | None:
        return storage.get(key)

    async def fake_setex(key: str, _ttl: int, value: str) -> None:
        storage[key] = value

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=fake_get)
    redis.setex = AsyncMock(side_effect=fake_setex)

    fhir = _fhir_mock()
    with patch.object(census_module, "fhir_client", fhir), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")):
        first = _run(build_census(["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system"))
        second = _run(build_census(["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system"))

    assert first.generated_at is not None
    assert second.generated_at == first.generated_at, (
        "warm-read generated_at must equal the original write time, not the cache-read time"
    )
    # Verify it really came from the cache (FHIR was only hit once).
    assert fhir.get_patient.await_count == 1


def test_force_refresh_bypasses_cache_and_produces_fresh_generated_at() -> None:
    """force_refresh=True skips the cache READ; the new generated_at differs."""
    cache_key = census_cache_key("system", ["pt-001"])
    storage: dict[str, str] = {}

    async def fake_get(key: str) -> str | None:
        return storage.get(key)

    async def fake_setex(key: str, _ttl: int, value: str) -> None:
        storage[key] = value

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=fake_get)
    redis.setex = AsyncMock(side_effect=fake_setex)

    fhir = _fhir_mock()
    with patch.object(census_module, "fhir_client", fhir), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")):
        first = _run(build_census(["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system"))
        # Tiny delay so the freshly generated timestamp is provably distinct.
        _run(asyncio.sleep(0.01))
        forced = _run(build_census(
            ["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system",
            force_refresh=True,
        ))

    assert first.generated_at is not None
    assert forced.generated_at is not None
    assert forced.generated_at != first.generated_at, "force_refresh must produce a NEW generated_at"
    # FHIR was hit twice — once cold, once forced. Cache was bypassed for
    # the second call.
    assert fhir.get_patient.await_count == 2


def test_force_refresh_writes_fresh_value_back_to_cache() -> None:
    """After force_refresh the cache holds the new generated_at."""
    cache_key = census_cache_key("system", ["pt-001"])
    storage: dict[str, str] = {}

    async def fake_get(key: str) -> str | None:
        return storage.get(key)

    async def fake_setex(key: str, _ttl: int, value: str) -> None:
        storage[key] = value

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=fake_get)
    redis.setex = AsyncMock(side_effect=fake_setex)

    fhir = _fhir_mock()
    with patch.object(census_module, "fhir_client", fhir), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")):
        _run(build_census(["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system"))
        _run(asyncio.sleep(0.01))
        forced = _run(build_census(
            ["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system",
            force_refresh=True,
        ))
        # Subsequent non-forced read must see the FRESH value.
        warm = _run(build_census(["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system"))

    assert warm.generated_at == forced.generated_at, (
        "after force_refresh, the cache must hold the new generated_at"
    )


def test_legacy_cached_payload_shape_still_deserialises() -> None:
    """A bare-list cached payload (pre-generated_at) is handled with generated_at=None.

    Backward-compat guard: a deploy that lands while warm caches contain the
    OLD shape must not crash. The UI then shows "Census · Refresh" without
    a timestamp.
    """
    cache_key = census_cache_key("system", ["pt-001"])
    # Pre-generated_at shape: a JSON list of entry dicts, not a wrapper.
    legacy_entry = {
        "patient_id": "pt-001",
        "name": "Test pt-001",
        "mrn": "1",
        "openemr_pid": "1",
        "triage_level": 11,
        "triage_label": "Routine",
        "triage_description": "",
        "matched_criteria": {},
        "vitals_summary": {},
        "abnormal_labs": [],
        "admit_date": None,
        "days_since_admit": None,
    }
    storage = {cache_key: json.dumps([legacy_entry])}

    async def fake_get(key: str) -> str | None:
        return storage.get(key)

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=fake_get)
    redis.setex = AsyncMock()

    with patch.object(census_module, "fhir_client", _fhir_mock()), \
            patch.object(census_module, "get_access_token", new=AsyncMock(return_value="tok")):
        result = _run(build_census(["pt-001"], redis_client=redis, cache_key=cache_key, provider_id="system"))

    assert len(result.verified) == 1
    assert result.generated_at is None  # legacy payload had no timestamp


def test_triage_census_endpoint_plumbs_force_refresh() -> None:
    """POST /triage/census with force_refresh=True forwards the flag to the tool."""
    captured: dict[str, Any] = {}

    async def fake_tool(input: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]:
        captured["input"] = input
        return {
            "result": {
                "census": [],
                "total": 0,
                "requested": 1,
                "dropped": 0,
                "dropped_ids": [],
                "generated_at": "2026-05-02T01:30:00+00:00",
            },
            "citations": [],
            "metadata": {},
        }

    with patch("main.get_census_summary", side_effect=fake_tool):
        from main import app  # noqa: WPS433
        client = TestClient(app)
        resp = client.post("/triage/census", json={"patient_ids": ["pt-001"], "force_refresh": True})

    assert resp.status_code == 200, resp.text
    assert captured["input"]["force_refresh"] is True
    assert captured["input"]["patient_ids"] == ["pt-001"]
    body = resp.json()
    assert body["generated_at"] == "2026-05-02T01:30:00+00:00"


def test_triage_census_endpoint_default_force_refresh_false() -> None:
    """Default behaviour: omitting force_refresh defaults to False."""
    captured: dict[str, Any] = {}

    async def fake_tool(input: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]:
        captured["input"] = input
        return {
            "result": {
                "census": [], "total": 0, "requested": 1, "dropped": 0, "dropped_ids": [],
                "generated_at": "2026-05-02T01:30:00+00:00",
            },
            "citations": [],
            "metadata": {},
        }

    with patch("main.get_census_summary", side_effect=fake_tool):
        from main import app  # noqa: WPS433
        client = TestClient(app)
        resp = client.post("/triage/census", json={"patient_ids": ["pt-001"]})

    assert resp.status_code == 200, resp.text
    assert captured["input"]["force_refresh"] is False
