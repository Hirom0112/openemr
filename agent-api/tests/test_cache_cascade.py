"""Integration tests for the prefetch cache cascade.

When the prefetch warm path fires with force_refresh=True it must
invalidate the dependent cache chain end-to-end:

    census  ->  per-patient bundle  ->  per-patient briefing
                                   \\->  medication safety reads bundle

These tests exercise the cascade via the same callable surface that
``main.py:_warm()`` uses (``build_census``, ``warm_bundle_for_patient``,
``warm_briefing_for_patient``) with mocked Anthropic + mocked FHIR so they
run in the millisecond regime and never burn tokens.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.tools as tools_module
from agent.tools import (
    _bundle_cache_key,
    _briefing_cache_key,
    get_patient_briefing,
    warm_bundle_for_patient,
    warm_briefing_for_patient,
)
from briefing.context_builder import BriefingContext
from briefing.schema import BriefingResponse
from config import settings
from triage.census import census_cache_key

pytestmark = pytest.mark.hard_failure


# ── Helpers ───────────────────────────────────────────────────────────────────


def _patient(pid: str = "pt-001") -> dict[str, Any]:
    return {"id": pid, "name": [{"text": "Test Patient"}]}


def _bundle(marker: str = "fresh") -> dict[str, Any]:
    # Marker lets us detect whether a freshly-fetched bundle reached the cache.
    return {"resources": {}, "_marker": marker}


def _ctx() -> BriefingContext:
    return BriefingContext(
        patient_id="pt-001",
        name="Test Patient",
        dob="1970-01-01",
        mrn="MRN001",
        code_status="Full Code",
        active_conditions=[],
        allergies=[],
        active_medications=[],
        recent_vitals=[],
        recent_labs=[],
        has_blank_allergy_section=False,
        has_blank_code_status=False,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _briefing(marker: str = "FRESH-BRIEFING") -> BriefingResponse:
    # `marker` is stamped into ``generated_at`` so tests can detect which
    # generator invocation produced the payload that ended up in the cache.
    return BriefingResponse(
        patient_id="pt-001",
        name="Test Patient",
        sections=[],
        alerts=[marker],
        generated_at=marker,
    )


def _stale_briefing_payload(bundle_fingerprint: str) -> dict[str, Any]:
    return {
        "result": {
            "patient_id": "pt-001",
            "name": "Test Patient",
            "sections": [],
            "alerts": ["STALE-BRIEFING"],
            "generated_at": "2026-01-01T00:00:00+00:00",
        },
        "citations": [],
        "metadata": {
            "tool": "get_patient_briefing",
            "patient_id": "pt-001",
            "duration_ms": 1,
            "fhir_resources_accessed": ["Patient"],
            "bundle_fingerprint": bundle_fingerprint,
        },
    }


def _stale_bundle(fingerprint: str) -> dict[str, Any]:
    return {"resources": {}, "_cached_at": fingerprint, "_marker": "stale"}


class FakeRedis:
    """In-memory async Redis double that supports get/setex/exists.

    Calls track invocations so tests can assert on them. Every value is a
    JSON-encoded string, matching how the real client stores them.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(initial or {})
        self.get = AsyncMock(side_effect=self._get)
        self.setex = AsyncMock(side_effect=self._setex)
        self.exists = AsyncMock(side_effect=self._exists)

    async def _get(self, key: str) -> str | None:
        return self.store.get(key)

    async def _setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    async def _exists(self, key: str) -> int:
        return 1 if key in self.store else 0


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_force_refresh_invalidates_dependent_caches() -> None:
    """force_refresh=True cascades: bundle re-fingerprinted -> briefing regenerated."""
    pid = "pt-001"

    stale_fingerprint = "2026-01-01T00:00:00+00:00"
    stale_bundle = _stale_bundle(stale_fingerprint)
    stale_briefing = _stale_briefing_payload(stale_fingerprint)

    redis = FakeRedis({
        _bundle_cache_key(pid): json.dumps(stale_bundle),
        _briefing_cache_key(pid): json.dumps(stale_briefing),
    })

    # FHIR returns a fresh bundle each call. The bundle.warm path should
    # write it back with a NEW fingerprint via _set_cached_bundle.
    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(return_value=_patient(pid))
    fhir_mock.get_bundle_for_patient = AsyncMock(side_effect=lambda _pid: _bundle("fresh-from-fhir"))

    fresh_briefing = _briefing(marker="FRESH-BRIEFING")
    gen_mock = AsyncMock(return_value=fresh_briefing)

    async def run() -> None:
        with patch.object(tools_module, "fhir_client", fhir_mock), \
                patch.object(tools_module, "generate_briefing", new=gen_mock), \
                patch.object(tools_module, "build_briefing_context", return_value=_ctx()), \
                patch.object(tools_module, "verify_briefing", return_value=fresh_briefing):
            # Step 1: bundle warm with force_refresh -> rewrites bundle in
            # Redis with a new _cached_at fingerprint.
            await warm_bundle_for_patient(redis, pid, force_refresh=True)
            # Step 2: briefing warm with force_refresh -> regenerates and
            # rewrites briefing in Redis with metadata.bundle_fingerprint
            # matching the new bundle fingerprint.
            await warm_briefing_for_patient(redis, pid, force_refresh=True)

    asyncio.run(run())

    # New bundle in Redis carries a new fingerprint (not the stale one).
    new_bundle = json.loads(redis.store[_bundle_cache_key(pid)])
    new_fingerprint = new_bundle.get("_cached_at")
    assert isinstance(new_fingerprint, str)
    assert new_fingerprint != stale_fingerprint, "bundle fingerprint must change after force_refresh"
    # Bundle came from FHIR, not the stale Redis copy.
    assert new_bundle.get("_marker") == "fresh-from-fhir"

    # New briefing payload is keyed to the NEW fingerprint and carries the
    # fresh marker (proving it came from the patched generator).
    new_briefing_payload = json.loads(redis.store[_briefing_cache_key(pid)])
    assert new_briefing_payload["metadata"]["bundle_fingerprint"] == new_fingerprint
    assert new_briefing_payload["result"]["alerts"] == ["FRESH-BRIEFING"]
    assert new_briefing_payload["metadata"]["forced_refresh"] is True

    # Generator was actually awaited (not served from stale cache).
    gen_mock.assert_awaited()


def test_force_refresh_disabled_serves_stale() -> None:
    """When the env-gate is OFF a force_refresh request downgrades to a normal warm.

    Bundle stays the original fingerprint, briefing cache hit returns the
    stale payload (NOT regenerated). Asserts the env-gate guard works the
    way the prefetch endpoint computes ``effective_force_refresh``.
    """
    pid = "pt-001"

    stale_fingerprint = "2026-01-01T00:00:00+00:00"
    stale_bundle = _stale_bundle(stale_fingerprint)
    stale_briefing = _stale_briefing_payload(stale_fingerprint)

    redis = FakeRedis({
        _bundle_cache_key(pid): json.dumps(stale_bundle),
        _briefing_cache_key(pid): json.dumps(stale_briefing),
    })

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(return_value=_patient(pid))
    fhir_mock.get_bundle_for_patient = AsyncMock(side_effect=lambda _pid: _bundle("would-be-fresh"))

    gen_mock = AsyncMock(return_value=_briefing(marker="WOULD-BE-FRESH"))

    async def run() -> None:
        # Simulate the gate: client requested True, settings disable it,
        # so effective_force_refresh = False (matches main.py:683).
        with patch.object(settings, "prefetch_force_refresh_on_login", False), \
                patch.object(tools_module, "fhir_client", fhir_mock), \
                patch.object(tools_module, "generate_briefing", new=gen_mock), \
                patch.object(tools_module, "build_briefing_context", return_value=_ctx()), \
                patch.object(tools_module, "verify_briefing", return_value=_briefing()):
            client_force = True
            effective = bool(client_force and settings.prefetch_force_refresh_on_login)
            assert effective is False, "env gate must downgrade force_refresh"

            await warm_bundle_for_patient(redis, pid, force_refresh=effective)
            await warm_briefing_for_patient(redis, pid, force_refresh=effective)

    asyncio.run(run())

    # Bundle untouched: same fingerprint, EXISTS short-circuit prevented FHIR call.
    bundle_after = json.loads(redis.store[_bundle_cache_key(pid)])
    assert bundle_after.get("_cached_at") == stale_fingerprint
    assert bundle_after.get("_marker") == "stale"
    fhir_mock.get_bundle_for_patient.assert_not_awaited()

    # Briefing untouched: EXISTS check on briefing key short-circuits warm.
    briefing_after = json.loads(redis.store[_briefing_cache_key(pid)])
    assert briefing_after["result"]["alerts"] == ["STALE-BRIEFING"]
    gen_mock.assert_not_awaited()


def test_briefing_cache_auto_invalidates_on_bundle_fingerprint_change() -> None:
    """Bundle _cached_at change without touching briefing cache -> regenerate.

    Pins the freshness fix from commit 13ad10842: a cached briefing whose
    metadata.bundle_fingerprint != the bundle's current _cached_at is
    treated as a miss, even if its TTL has not elapsed.
    """
    pid = "pt-001"

    # Briefing cache contains a stale payload keyed to fingerprint "A".
    # Bundle cache has been independently updated to fingerprint "B".
    stale_briefing = _stale_briefing_payload("A")
    bundle_at_b = {"resources": {}, "_cached_at": "B"}

    redis = FakeRedis({
        _briefing_cache_key(pid): json.dumps(stale_briefing),
        _bundle_cache_key(pid): json.dumps(bundle_at_b),
    })

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(return_value=_patient(pid))
    # When briefing regenerates it pulls the bundle straight from cache
    # (bundle_cache_state == "hit" path). FHIR call happens only as a
    # safety net if cache returns None — assert it does NOT happen here.
    fhir_mock.get_bundle_for_patient = AsyncMock(side_effect=lambda _pid: _bundle("should-not-fetch"))

    fresh = _briefing(marker="REGENERATED")
    gen_mock = AsyncMock(return_value=fresh)

    async def run() -> dict[str, Any]:
        with patch.object(tools_module, "fhir_client", fhir_mock), \
                patch.object(tools_module, "generate_briefing", new=gen_mock), \
                patch.object(tools_module, "build_briefing_context", return_value=_ctx()), \
                patch.object(tools_module, "verify_briefing", return_value=fresh):
            return await get_patient_briefing(
                {"patient_id": pid},
                {"redis_client": redis},
            )

    result = asyncio.run(run())

    # Cache MISS path was taken because bundle fingerprint changed.
    assert result["metadata"]["cache"] == "miss"
    gen_mock.assert_awaited_once()
    # Returned payload is the fresh one, not the staged stale alerts.
    assert result["result"]["alerts"] == ["REGENERATED"]
    # FHIR bundle fetch was NOT made — bundle came from Redis cache hit.
    fhir_mock.get_bundle_for_patient.assert_not_awaited()
    # Newly written briefing in Redis is keyed to fingerprint "B" now.
    new_briefing = json.loads(redis.store[_briefing_cache_key(pid)])
    assert new_briefing["metadata"]["bundle_fingerprint"] == "B"
