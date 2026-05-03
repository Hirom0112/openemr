"""Tests for the Redis output cache on get_medication_safety.

Mirrors test_briefing_cache.py. Covers:
  1. Cache hit returns cached payload without invoking the LLM/safety pipeline.
  2. Cache miss runs the pipeline and writes the payload with the correct key + TTL.
  3. Bundle fingerprint mismatch invalidates the cache (regenerates).
  4. force_refresh=True bypasses the cache read and writes a fresh entry.
  5. Cache write failure is non-fatal (tool returns successfully, WARNING logged).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tools import get_medication_safety
from config import settings
from medication.safety import MedicationSafetyReport


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run(coro: Any) -> Any:  # noqa: ANN401
    # Use a fresh loop per call instead of asyncio.get_event_loop() — the
    # latter raises ``RuntimeError: There is no current event loop`` once an
    # earlier test in the suite has closed/torn down the implicit main-thread
    # loop (a known Python 3.9 gotcha that bites test_briefing_cache.py too).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_BUNDLE_FINGERPRINT = "2026-04-30T00:00:00+00:00"


def _bundle(fingerprint: str = _BUNDLE_FINGERPRINT) -> dict[str, Any]:
    return {
        "resources": {
            "MedicationRequest": [],
            "AllergyIntolerance": [],
            "Observation": [],
        },
        "_cached_at": fingerprint,
    }


def _safety_report() -> MedicationSafetyReport:
    return MedicationSafetyReport(
        patient_id="8",
        flags=[],
        summary="No medication safety flags detected for the active medication list.",
        medications_reviewed=0,
        generated_at=_BUNDLE_FINGERPRINT,
    )


def _cached_payload(bundle_fingerprint: str | None = _BUNDLE_FINGERPRINT) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tool": "get_medication_safety",
        "patient_id": "8",
        "duration_ms": 13,
        "fhir_resources_accessed": ["MedicationRequest", "AllergyIntolerance", "Observation"],
        "cache": "miss",
    }
    if bundle_fingerprint is not None:
        metadata["bundle_fingerprint"] = bundle_fingerprint
    return {
        "result": {
            "patient_id": "8",
            "medications_reviewed": 0,
            "flag_count": 0,
            "flags": [],
            "summary": "from cache",
            "current_medications": [],
            "allergies": [],
            "interactions": [],
            "generated_at": "2026-04-30T00:00:00+00:00",
        },
        "citations": [],
        "metadata": metadata,
    }


def _make_redis(get_side_effect: Any) -> MagicMock:
    redis_client = MagicMock()
    redis_client.get = AsyncMock(side_effect=get_side_effect)
    redis_client.setex = AsyncMock()
    return redis_client


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
def test_cache_hit_returns_cached_payload_without_running_pipeline():
    """Redis returns stored payload + matching bundle fingerprint → no LLM call."""
    cached = _cached_payload()
    bundle_payload = _bundle()

    async def fake_get(key: str) -> str | None:
        if key == "copilot:medication_safety:8":
            return json.dumps(cached)
        if key == "copilot:bundle:8":
            return json.dumps(bundle_payload)
        return None

    redis_client = _make_redis(fake_get)

    fhir_mock = MagicMock()
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    safety_mock = MagicMock(return_value=_safety_report())
    add_summary_mock = AsyncMock(return_value=_safety_report())

    with patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.run_safety_checks", new=safety_mock), \
            patch("agent.tools.add_llm_summary", new=add_summary_mock):
        result = _run(get_medication_safety(
            {"patient_id": "8"},
            {"redis_client": redis_client},
        ))

    redis_client.get.assert_any_await("copilot:medication_safety:8")
    redis_client.get.assert_any_await("copilot:bundle:8")
    safety_mock.assert_not_called()
    add_summary_mock.assert_not_awaited()
    fhir_mock.get_bundle_for_patient.assert_not_awaited()
    redis_client.setex.assert_not_awaited()

    assert result["metadata"]["cache"] == "hit"
    assert result["result"]["summary"] == "from cache"
    assert "duration_ms" in result["metadata"]


@pytest.mark.hard_failure
def test_cache_miss_runs_pipeline_and_writes_to_redis():
    """Redis returns None → pipeline runs; payload written with correct key + TTL."""
    redis_client = _make_redis(AsyncMock(return_value=None))

    fhir_mock = MagicMock()
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    safety_mock = MagicMock(return_value=_safety_report())
    add_summary_mock = AsyncMock(return_value=_safety_report())

    with patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.run_safety_checks", new=safety_mock), \
            patch("agent.tools.add_llm_summary", new=add_summary_mock):
        result = _run(get_medication_safety(
            {"patient_id": "8"},
            {"redis_client": redis_client},
        ))

    safety_mock.assert_called_once()
    add_summary_mock.assert_awaited_once()

    # Find the medication-safety setex (a bundle setex may also fire on
    # bundle cache miss).
    safety_setex = next(
        (call for call in redis_client.setex.await_args_list
         if call.args and call.args[0] == "copilot:medication_safety:8"),
        None,
    )
    assert safety_setex is not None, "medication_safety setex not found"
    assert safety_setex.args[1] == settings.medication_safety_cache_ttl_seconds
    written = json.loads(safety_setex.args[2])
    assert written["metadata"]["cache"] == "miss"
    # On a cold miss the bundle is re-stamped by _set_cached_bundle, so the
    # fingerprint reflects "now()" — assert it's present and non-empty rather
    # than equal to a fixed value.
    assert isinstance(written["metadata"].get("bundle_fingerprint"), str)
    assert written["metadata"]["bundle_fingerprint"]

    assert result["metadata"]["cache"] == "miss"
    assert result["metadata"]["tool"] == "get_medication_safety"


@pytest.mark.hard_failure
def test_cached_safety_invalidated_when_bundle_fingerprint_changes():
    """Bundle refresh (new _cached_at) makes the cached safety report stale → regenerate."""
    cached = _cached_payload(bundle_fingerprint="2026-04-01T00:00:00+00:00")
    bundle_payload = _bundle(fingerprint="2026-05-01T12:00:00+00:00")

    async def fake_get(key: str) -> str | None:
        if key == "copilot:medication_safety:8":
            return json.dumps(cached)
        if key == "copilot:bundle:8":
            return json.dumps(bundle_payload)
        return None

    redis_client = _make_redis(fake_get)

    fhir_mock = MagicMock()
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    safety_mock = MagicMock(return_value=_safety_report())
    add_summary_mock = AsyncMock(return_value=_safety_report())

    with patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.run_safety_checks", new=safety_mock), \
            patch("agent.tools.add_llm_summary", new=add_summary_mock):
        result = _run(get_medication_safety(
            {"patient_id": "8"},
            {"redis_client": redis_client},
        ))

    safety_mock.assert_called_once()
    add_summary_mock.assert_awaited_once()
    assert result["metadata"]["cache"] == "miss"
    assert result["result"]["summary"] != "from cache"


@pytest.mark.hard_failure
def test_force_refresh_bypasses_cache_and_writes_fresh():
    """force_refresh=True skips the cache read even on a hit and regenerates."""
    cached = _cached_payload()
    bundle_payload = _bundle()

    async def fake_get(key: str) -> str | None:
        if key == "copilot:medication_safety:8":
            return json.dumps(cached)
        if key == "copilot:bundle:8":
            return json.dumps(bundle_payload)
        return None

    redis_client = _make_redis(fake_get)

    fhir_mock = MagicMock()
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    safety_mock = MagicMock(return_value=_safety_report())
    add_summary_mock = AsyncMock(return_value=_safety_report())

    with patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.run_safety_checks", new=safety_mock), \
            patch("agent.tools.add_llm_summary", new=add_summary_mock):
        result = _run(get_medication_safety(
            {"patient_id": "8", "force_refresh": True},
            {"redis_client": redis_client},
        ))

    # force_refresh path should not even attempt to read the safety cache key.
    safety_get_calls = [
        c for c in redis_client.get.await_args_list
        if c.args and c.args[0] == "copilot:medication_safety:8"
    ]
    assert safety_get_calls == [], "force_refresh must skip safety cache read"

    fhir_mock.get_bundle_for_patient.assert_awaited_once()
    safety_mock.assert_called_once()

    safety_setex = next(
        (call for call in redis_client.setex.await_args_list
         if call.args and call.args[0] == "copilot:medication_safety:8"),
        None,
    )
    assert safety_setex is not None, "force_refresh must still write to cache"
    assert result["metadata"]["cache"] == "miss"
    assert result["metadata"].get("forced_refresh") is True


@pytest.mark.hard_failure
def test_cache_write_failure_is_non_fatal(caplog):
    """Redis raising on setex is logged but does not propagate."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=None)
    redis_client.setex = AsyncMock(side_effect=RedisConnectionError("redis down"))

    fhir_mock = MagicMock()
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    safety_mock = MagicMock(return_value=_safety_report())
    add_summary_mock = AsyncMock(return_value=_safety_report())

    with caplog.at_level("WARNING", logger="agent.tools"), \
            patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.run_safety_checks", new=safety_mock), \
            patch("agent.tools.add_llm_summary", new=add_summary_mock):
        result = _run(get_medication_safety(
            {"patient_id": "8"},
            {"redis_client": redis_client},
        ))

    # Tool still returned a fresh payload despite Redis write failure.
    assert result["metadata"]["cache"] == "miss"
    assert result["metadata"]["patient_id"] == "8"

    cache_warnings = [
        r for r in caplog.records
        if "Medication safety cache write failed" in r.getMessage()
    ]
    assert len(cache_warnings) >= 1
