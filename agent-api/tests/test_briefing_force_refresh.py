"""Tests for the force_refresh path on get_patient_briefing.

Covers the Refresh button in the BriefingRenderer that needs to bypass the
30-min Redis briefing cache (and the bundle cache) so the user gets a brand
new generated_at timestamp.

  1. force_refresh=True bypasses the briefing cache (generator runs even
     when a hit is staged in Redis).
  2. force_refresh=True still writes the freshly generated briefing back to
     Redis so subsequent non-forced reads benefit.
  3. force_refresh=True attaches metadata.forced_refresh = True for
     observability.
  4. The /briefing/{pid} HTTP endpoint plumbs force_refresh through to the
     tool when force_refresh is supplied in the POST body.
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
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tools import get_patient_briefing
from briefing.context_builder import BriefingContext
from briefing.schema import BriefingResponse


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


@pytest.mark.hard_failure
def test_force_refresh_skips_cache_and_runs_generator():
    """force_refresh=True bypasses the briefing cache hit and re-generates."""
    cached = _cached_payload()
    redis_client = MagicMock()
    # Briefing cache returns a hit; force_refresh must still skip it.
    redis_client.get = AsyncMock(return_value=json.dumps(cached))
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
            {"patient_id": "pt-001", "force_refresh": True},
            {"redis_client": redis_client},
        ))

    # Generator was awaited despite the cache being warm.
    gen_mock.assert_awaited_once()
    # Patient/bundle were fetched fresh, not pulled from the bundle cache.
    fhir_mock.get_patient.assert_awaited_once()
    fhir_mock.get_bundle_for_patient.assert_awaited_once()
    # Briefing cache get was NOT called for the briefing key (skipped because
    # force_refresh shortcuts the lookup). Bundle cache get is also skipped.
    for call in redis_client.get.await_args_list:
        assert "briefing" not in str(call)
        assert "bundle" not in str(call)

    assert result["metadata"]["cache"] == "miss"
    assert result["metadata"]["forced_refresh"] is True
    # The freshly generated briefing — not the staged "from cache" payload.
    assert result["result"]["generated_at"] == "2026-05-01T00:00:00+00:00"


@pytest.mark.hard_failure
def test_force_refresh_writes_fresh_briefing_to_cache():
    """After force_refresh the new payload is written back to Redis so warm reads benefit."""
    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=None)
    redis_client.setex = AsyncMock()

    fhir_mock = MagicMock()
    fhir_mock.get_patient = AsyncMock(return_value=_patient())
    fhir_mock.get_bundle_for_patient = AsyncMock(return_value=_bundle())

    with patch("agent.tools.fhir_client", fhir_mock), \
            patch("agent.tools.generate_briefing", new=AsyncMock(return_value=_briefing())), \
            patch("agent.tools.build_briefing_context", return_value=_ctx()), \
            patch("agent.tools.verify_briefing", return_value=_briefing()):
        _run(get_patient_briefing(
            {"patient_id": "pt-001", "force_refresh": True},
            {"redis_client": redis_client},
        ))

    briefing_setex = next(
        (call for call in redis_client.setex.await_args_list
         if call.args and call.args[0] == "copilot:briefing:pt-001"),
        None,
    )
    assert briefing_setex is not None
    written = json.loads(briefing_setex.args[2])
    assert written["metadata"]["cache"] == "miss"
    assert written["metadata"]["forced_refresh"] is True


@pytest.mark.hard_failure
def test_default_behavior_uses_cache_hit_path():
    """Without force_refresh the cache hit path is preserved (regression guard)."""
    cached = _cached_payload()
    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=json.dumps(cached))
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

    gen_mock.assert_not_awaited()
    fhir_mock.get_patient.assert_not_awaited()
    assert result["metadata"]["cache"] == "hit"
    assert "forced_refresh" not in result["metadata"]


@pytest.mark.hard_failure
def test_briefing_endpoint_plumbs_force_refresh():
    """POST /briefing/{patient_id} with force_refresh=True forwards the flag to the tool."""
    captured: dict[str, Any] = {}

    async def fake_tool(input: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]:
        captured["input"] = input
        return {
            "result": {
                "patient_id": input["patient_id"],
                "name": "Test Patient",
                "sections": [],
                "alerts": [],
                "generated_at": "2026-05-01T00:00:00+00:00",
            },
            "citations": [],
            "metadata": {},
        }

    with patch("main.get_patient_briefing", side_effect=fake_tool):
        # Importing main at module top would execute startup wiring; defer to here.
        from main import app  # noqa: WPS433
        client = TestClient(app)
        resp = client.post("/briefing/pt-001", json={"force_refresh": True})

    assert resp.status_code == 200, resp.text
    assert captured["input"]["force_refresh"] is True
    assert captured["input"]["patient_id"] == "pt-001"


@pytest.mark.hard_failure
def test_briefing_endpoint_default_force_refresh_false():
    """POST /briefing/{patient_id} without a body defaults force_refresh to False."""
    captured: dict[str, Any] = {}

    async def fake_tool(input: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]:
        captured["input"] = input
        return {
            "result": {
                "patient_id": input["patient_id"],
                "name": "Test Patient",
                "sections": [],
                "alerts": [],
                "generated_at": "2026-05-01T00:00:00+00:00",
            },
            "citations": [],
            "metadata": {},
        }

    with patch("main.get_patient_briefing", side_effect=fake_tool):
        from main import app  # noqa: WPS433
        client = TestClient(app)
        resp = client.post("/briefing/pt-001", json={})

    assert resp.status_code == 200, resp.text
    assert captured["input"]["force_refresh"] is False
