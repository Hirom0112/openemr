"""Test the Redis bundle-cache path on handoff._generate_one.

Asserts that when ``copilot:bundle:{patient_id}`` is pre-populated in the
mock Redis client, ``_generate_one`` does NOT call
``fhir_client.get_bundle_for_patient``. The LLM call is also mocked so no
network access is required.
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

from briefing.context_builder import BriefingContext
from handoff.generator import _generate_one


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.hard_failure
def test_generate_one_uses_cached_bundle_and_skips_fhir() -> None:
    patient_id = "pt-cache-1"
    cached_bundle = {"resources": {"Condition": []}}

    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=json.dumps(cached_bundle).encode("utf-8"))
    redis_client.setex = AsyncMock()

    fake_patient = {"id": patient_id, "name": [{"text": "Cached Patient"}]}

    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {
        "illness_severity": "Stable",
        "patient_summary": "ok",
        "action_list": [],
        "situation_awareness": "",
        "contingency_plan": "",
    }
    fake_response = MagicMock()
    fake_response.content = [tool_block]
    anthropic_client = MagicMock()
    anthropic_client.messages.create = AsyncMock(return_value=fake_response)

    with patch("handoff.generator.fhir_client") as mock_fhir, \
         patch("handoff.generator.build_context") as mock_build_ctx, \
         patch("handoff.generator.extract") as mock_extract, \
         patch("handoff.generator.rank") as mock_rank:
        mock_fhir.get_patient = AsyncMock(return_value=fake_patient)
        mock_fhir.get_bundle_for_patient = AsyncMock(
            side_effect=AssertionError("Should not fetch bundle when cache hit"),
        )
        ctx_obj = BriefingContext(
            patient_id=patient_id,
            name="Cached Patient",
            dob="1970-01-01",
            mrn="MRN-1",
            code_status="",
            active_conditions=[],
            allergies=[],
            active_medications=[],
            recent_vitals=[],
            recent_labs=[],
            has_blank_allergy_section=True,
            has_blank_code_status=True,
            fetched_at="2026-01-01T00:00:00+00:00",
        )
        mock_build_ctx.return_value = ctx_obj
        mock_extract.return_value = {}
        triage_obj = MagicMock()
        triage_obj.level = 3
        triage_obj.label = "Stable"
        mock_rank.return_value = triage_obj

        summary = _run(
            _generate_one(
                patient_id,
                anthropic_client,
                langfuse=None,
                redis_client=redis_client,
            ),
        )

    assert mock_fhir.get_bundle_for_patient.await_count == 0
    # Bundle cache GET fires; handoff cache GET only fires when the bundle
    # carries a ``_cached_at`` fingerprint. This fixture omits it, so we
    # expect a single GET against the bundle key (legacy behavior preserved).
    bundle_calls = [
        c for c in redis_client.get.await_args_list
        if c.args and c.args[0] == f"copilot:bundle:{patient_id}"
    ]
    assert len(bundle_calls) == 1, f"expected 1 bundle GET, got {len(bundle_calls)}"
    redis_client.setex.assert_not_awaited()  # no fingerprint → handoff write is no-op
    assert summary.error is None
    assert summary.patient_id == patient_id
    assert summary.illness_severity == "Stable"


@pytest.mark.hard_failure
def test_generate_one_bypasses_handoff_cache_for_freshness() -> None:
    """Handoff intentionally regenerates per-patient on every shift.

    Even when a cached HandoffSummary exists at
    ``copilot:handoff:{pid}:{fingerprint}``, the generator MUST run the LLM
    to produce a fresh I-PASS. Handoffs are clinical events at shift
    boundaries — a stale cached summary that doesn't reflect the most
    recent vitals/labs/orders is a clinical risk that outweighs the
    latency win of skipping the LLM. (The bundle cache is still in play,
    so the FHIR fetch is fast on warm sessions.)
    """
    patient_id = "pt-cache-2"
    fingerprint = "2026-05-02T10:00:00+00:00"
    cached_bundle = {"resources": {"Condition": []}, "_cached_at": fingerprint}
    cached_handoff_dict = {
        "patient_id": patient_id,
        "name": "Cached Patient",
        "mrn": "MRN-2",
        "triage_level": 3,
        "illness_severity": "Stable",
        "patient_summary": "STALE-from-cache",
        "action_list": ["stale-action"],
        "situation_awareness": "",
        "contingency_plan": "",
        "generated_at": "2026-05-02T10:00:01+00:00",
        "error": None,
    }

    async def _redis_get(key: str) -> bytes | None:
        if key == f"copilot:bundle:{patient_id}":
            return json.dumps(cached_bundle).encode("utf-8")
        if key == f"copilot:handoff:{patient_id}:{fingerprint}":
            return json.dumps(cached_handoff_dict).encode("utf-8")
        return None

    redis_client = MagicMock()
    redis_client.get = AsyncMock(side_effect=_redis_get)
    redis_client.setex = AsyncMock()

    fresh_tool_use = MagicMock()
    fresh_tool_use.type = "tool_use"
    fresh_tool_use.input = {
        "illness_severity": "Watcher",
        "patient_summary": "FRESH from LLM",
        "action_list": ["fresh-action"],
        "situation_awareness": "fresh",
        "contingency_plan": "fresh",
    }
    anthropic_response = MagicMock()
    anthropic_response.content = [fresh_tool_use]
    anthropic_client = MagicMock()
    anthropic_client.messages.create = AsyncMock(return_value=anthropic_response)

    with patch("handoff.generator.fhir_client") as mock_fhir:
        mock_fhir.get_patient = AsyncMock(return_value={"id": patient_id})
        mock_fhir.get_bundle_for_patient = AsyncMock(
            side_effect=AssertionError("bundle cache hit → no FHIR fetch expected"),
        )
        summary = _run(
            _generate_one(
                patient_id,
                anthropic_client,
                langfuse=None,
                redis_client=redis_client,
            ),
        )

    # LLM ran — handoff cache was bypassed.
    assert anthropic_client.messages.create.await_count == 1
    # Returned summary reflects FRESH LLM output, not the stale cache.
    assert summary.patient_summary == "FRESH from LLM"
    assert summary.action_list == ["fresh-action"]
    # No write-back to handoff cache either (we removed both sides).
    assert all(
        not (call.args and str(call.args[0]).startswith(f"copilot:handoff:{patient_id}"))
        for call in redis_client.setex.await_args_list
    )
