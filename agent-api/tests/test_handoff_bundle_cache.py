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
    redis_client.get.assert_awaited_once_with(f"copilot:bundle:{patient_id}")
    redis_client.setex.assert_not_awaited()
    assert summary.error is None
    assert summary.patient_id == patient_id
    assert summary.illness_severity == "Stable"
