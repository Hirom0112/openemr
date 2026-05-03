"""Tests for the Redis caching path on get_patient_briefing in agent/tools/__init__.py.

Covers:
  1. Cache hit — cached payload is returned with metadata.cache == "hit" and
     the underlying briefing generator is NOT invoked.
  2. Cache miss — generator runs, payload is written to Redis with the correct
     key + TTL, and metadata.cache == "miss".
  3. Redis errors are non-fatal — a raising Redis client does not break the
     call; the generator runs and the result is returned, with the error logged.
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

from agent.tools import get_patient_briefing
from config import settings
from briefing.context_builder import BriefingContext
from briefing.schema import BriefingResponse


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.get_event_loop().run_until_complete(coro)


def _bundle() -> dict[str, Any]:
    return {"resources": {}}


def _patient() -> dict[str, Any]:
    return {"id": "pt-001", "name": [{"text": "Test Patient"}]}


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


def _briefing() -> BriefingResponse:
    return BriefingResponse(
        patient_id="pt-001",
        name="Test Patient",
        sections=[],
        alerts=[],
        generated_at="2026-05-01T00:00:00+00:00",
    )


def _cached_payload() -> dict[str, Any]:
    return {
        "result": {
            "patient_id": "pt-001",
            "name": "Test Patient",
            "sections": [],
            "alerts": ["from cache"],
            "generated_at": "2026-04-30T00:00:00+00:00",
        },
        "citations": [],
        "metadata": {
            "tool": "get_patient_briefing",
            "patient_id": "pt-001",
            "duration_ms": 42,
            "fhir_resources_accessed": ["Patient"],
        },
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
def test_cache_hit_returns_cached_payload_without_calling_generator():
    """Redis returns a stored payload → generator never runs; metadata.cache == 'hit'."""
    cached = _cached_payload()
    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=json.dumps(cached))
    redis_client.setex = AsyncMock()

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(return_value=_patient())
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    with patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.generate_briefing", new=AsyncMock(return_value=_briefing())) as gen_mock, \
            patch("agent.tools.build_briefing_context", return_value=_ctx()), \
            patch("agent.tools.verify_briefing", return_value=_briefing()):
        result = _run(get_patient_briefing(
            {"patient_id": "pt-001"},
            {"redis_client": redis_client},
        ))

    redis_client.get.assert_awaited_once_with("copilot:briefing:pt-001")
    gen_mock.assert_not_awaited()
    fhir_mock.get_patient.assert_not_awaited()
    fhir_mock.get_bundle_for_patient.assert_not_awaited()
    redis_client.setex.assert_not_awaited()

    assert result["metadata"]["cache"] == "hit"
    assert result["result"]["alerts"] == ["from cache"]
    assert "duration_ms" in result["metadata"]


@pytest.mark.hard_failure
def test_cache_miss_runs_generator_and_writes_to_redis():
    """Redis returns None → generator runs; payload written with correct key + TTL; metadata.cache == 'miss'."""
    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=None)
    redis_client.setex = AsyncMock()

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(return_value=_patient())
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    gen_mock = AsyncMock(return_value=_briefing())

    with patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.generate_briefing", new=gen_mock), \
            patch("agent.tools.build_briefing_context", return_value=_ctx()), \
            patch("agent.tools.verify_briefing", return_value=_briefing()):
        result = _run(get_patient_briefing(
            {"patient_id": "pt-001"},
            {"redis_client": redis_client},
        ))

    # get_patient_briefing now also probes the bundle cache key on a briefing
    # miss (added when the bundle cache was wired into the tool). Expect both
    # the briefing-key probe and the bundle-key probe; the briefing key is the
    # first call.
    assert redis_client.get.await_count == 2
    redis_client.get.assert_any_await("copilot:briefing:pt-001")
    redis_client.get.assert_any_await("copilot:bundle:pt-001")
    gen_mock.assert_awaited_once()
    # setex is called twice on a cold miss: once to write the briefing payload,
    # once to write the bundle payload. Find the briefing write to assert on.
    assert redis_client.setex.await_count == 2
    briefing_setex = next(
        (call for call in redis_client.setex.await_args_list
         if call.args and call.args[0] == "copilot:briefing:pt-001"),
        None,
    )
    assert briefing_setex is not None, "briefing setex not found"
    assert briefing_setex.args[1] == settings.briefing_cache_ttl_seconds
    # Third arg is JSON-serialized payload.
    written = json.loads(briefing_setex.args[2])
    assert written["metadata"]["cache"] == "miss"

    assert result["metadata"]["cache"] == "miss"
    assert result["metadata"]["tool"] == "get_patient_briefing"
    assert result["metadata"]["patient_id"] == "pt-001"


@pytest.mark.hard_failure
def test_redis_error_is_non_fatal(caplog):
    """Redis raising on get() is logged but does not propagate; generator runs and result returned."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    redis_client = MagicMock()
    redis_client.get = AsyncMock(side_effect=RedisConnectionError("redis down"))
    # setex also fails — verify the write-side exception is also non-fatal.
    redis_client.setex = AsyncMock(side_effect=RedisConnectionError("redis down"))

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(return_value=_patient())
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    gen_mock = AsyncMock(return_value=_briefing())

    with caplog.at_level("WARNING", logger="agent.tools"), \
            patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.generate_briefing", new=gen_mock), \
            patch("agent.tools.build_briefing_context", return_value=_ctx()), \
            patch("agent.tools.verify_briefing", return_value=_briefing()):
        result = _run(get_patient_briefing(
            {"patient_id": "pt-001"},
            {"redis_client": redis_client},
        ))

    # Generator still ran despite Redis read failure.
    gen_mock.assert_awaited_once()
    # Result is the freshly generated payload (miss path).
    assert result["metadata"]["cache"] == "miss"
    assert result["metadata"]["patient_id"] == "pt-001"

    # Both read- and write-side cache failures were logged.
    cache_warnings = [
        r for r in caplog.records
        if "Briefing cache" in r.getMessage() and "pt-001" in r.getMessage()
    ]
    assert len(cache_warnings) >= 1
