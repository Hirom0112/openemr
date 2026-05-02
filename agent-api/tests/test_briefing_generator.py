"""Tests for briefing/generator.py — required-sections directive, retry, and schema-error logging.

Covers the recently added behaviors:
  1. Schema-valid first pass returns populated sections without retry.
  2. Empty/missing sections trigger a one-shot retry; final result is the retry payload.
  3. Retry-still-empty falls through and the validated (empty-sections) response is returned.
  4. Schema-validation errors are logged with structured context and a fallback is returned.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from briefing.context_builder import (
    ActiveCondition,
    ActiveMedication,
    BriefingContext,
    Citation,
)
from briefing.generator import generate_briefing
from briefing.schema import BriefingResponse


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.get_event_loop().run_until_complete(coro)


def _citation(code: str = "12345", display: str = "Test", value: str = "v") -> Citation:
    return Citation(
        resource_type="Condition",
        code=code,
        display=display,
        effective_dt="2026-04-30T12:00:00+00:00",
        value=value,
    )


def _ctx_with_data() -> BriefingContext:
    cit = _citation()
    return BriefingContext(
        patient_id="pt-001",
        name="Test Patient",
        dob="1970-01-01",
        mrn="MRN001",
        code_status="Full Code",
        active_conditions=[ActiveCondition(display="HTN", onset="2020", citation=cit)],
        allergies=[],
        active_medications=[
            ActiveMedication(name="Lisinopril", dose="10mg", route="PO", status="active", citation=cit),
        ],
        recent_vitals=[],
        recent_labs=[],
        has_blank_allergy_section=False,
        has_blank_code_status=False,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _populated_tool_input() -> dict[str, Any]:
    return {
        "patient_id": "pt-001",
        "name": "Test Patient",
        "sections": [
            {
                "section": "diagnosis",
                "summary": "Patient has hypertension.",
                "claims": [
                    {
                        "text": "HTN noted on problem list.",
                        "source_resource": "Condition",
                        "source_code": "12345",
                        "source_value": "HTN",
                        "source_dt": "2026-04-30T12:00:00+00:00",
                    }
                ],
            },
            {
                "section": "medications",
                "summary": "Patient takes lisinopril.",
                "claims": [
                    {
                        "text": "Lisinopril 10mg PO daily.",
                        "source_resource": "MedicationRequest",
                        "source_code": "12345",
                        "source_value": "Lisinopril",
                        "source_dt": "2026-04-30T12:00:00+00:00",
                    }
                ],
            },
        ],
        "alerts": [],
        "generated_at": "2026-05-01T00:00:00+00:00",
    }


def _empty_sections_tool_input() -> dict[str, Any]:
    return {
        "patient_id": "pt-001",
        "name": "Test Patient",
        "sections": [],
        "alerts": ["Some alert"],
        "generated_at": "2026-05-01T00:00:00+00:00",
    }


def _malformed_tool_input() -> dict[str, Any]:
    # Missing required `generated_at`; sections claim missing source_dt.
    return {
        "patient_id": "pt-001",
        "name": "Test Patient",
        "sections": [
            {
                "section": "diagnosis",
                "summary": "x",
                "claims": [
                    {
                        "text": "x",
                        "source_resource": "Condition",
                        "source_code": "1",
                        "source_value": "v",
                        # source_dt intentionally omitted
                    }
                ],
            }
        ],
        "alerts": [],
    }


def _llm_response(tool_input: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "produce_briefing"
    block.id = "toolu_test"
    block.input = tool_input

    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


def _patch_client(side_effects: list[MagicMock]) -> Any:
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=side_effects)
    return patch("briefing.generator.anthropic.AsyncAnthropic", return_value=mock_client)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
def test_uses_haiku_4_5_model():
    """Model passed to messages.create must be the Haiku 4.5 alias for fast cold briefings."""
    ctx = _ctx_with_data()
    responses = [_llm_response(_populated_tool_input())]

    with _patch_client(responses) as mock_ctor:
        _run(generate_briefing(ctx))

    mock_client = mock_ctor.return_value
    assert mock_client.messages.create.await_args.kwargs["model"] == "claude-haiku-4-5"


@pytest.mark.hard_failure
def test_returns_populated_sections_on_first_pass():
    """Schema-valid first response with populated sections — no retry occurs."""
    ctx = _ctx_with_data()
    responses = [_llm_response(_populated_tool_input())]

    with _patch_client(responses) as mock_ctor:
        result = _run(generate_briefing(ctx))

    assert isinstance(result, BriefingResponse)
    assert len(result.sections) == 2
    assert {s.section for s in result.sections} == {"diagnosis", "medications"}
    # Exactly one LLM call → no retry.
    mock_client = mock_ctor.return_value
    assert mock_client.messages.create.await_count == 1


@pytest.mark.hard_failure
def test_retries_when_sections_empty_and_returns_retry_result():
    """Empty sections on first call triggers a single retry; populated retry wins."""
    ctx = _ctx_with_data()
    responses = [
        _llm_response(_empty_sections_tool_input()),
        _llm_response(_populated_tool_input()),
    ]

    with _patch_client(responses) as mock_ctor:
        result = _run(generate_briefing(ctx))

    mock_client = mock_ctor.return_value
    assert mock_client.messages.create.await_count == 2

    # The second call must include the retry-instruction user turn.
    second_call = mock_client.messages.create.await_args_list[1]
    messages = second_call.kwargs["messages"]
    assert len(messages) == 3  # original user, assistant, retry user
    assert messages[-1]["role"] == "user"
    assert "omitted the sections array" in messages[-1]["content"]

    # Final result is the populated retry payload.
    assert len(result.sections) == 2


@pytest.mark.hard_failure
def test_retry_still_empty_returns_empty_sections_response():
    """When both attempts return empty sections, the empty validated response is returned.

    Per the current code: after the retry the second tool_block is validated and returned;
    no second-level fallback fires for the empty-sections-only failure mode.
    """
    ctx = _ctx_with_data()
    responses = [
        _llm_response(_empty_sections_tool_input()),
        _llm_response(_empty_sections_tool_input()),
    ]

    with _patch_client(responses) as mock_ctor:
        result = _run(generate_briefing(ctx))

    mock_client = mock_ctor.return_value
    assert mock_client.messages.create.await_count == 2
    assert isinstance(result, BriefingResponse)
    assert result.sections == []
    # The alert from the empty payload survives.
    assert result.alerts == ["Some alert"]


@pytest.mark.hard_failure
def test_schema_validation_error_is_logged_and_fallback_returned(caplog):
    """Malformed tool_use payload → ValidationError logged with patient_id + payload; fallback returned."""
    ctx = _ctx_with_data()
    responses = [_llm_response(_malformed_tool_input())]

    with caplog.at_level("ERROR", logger="briefing.generator"), _patch_client(responses):
        result = _run(generate_briefing(ctx))

    # Fallback briefing — empty sections, alerts derived from ctx flags (none here).
    assert isinstance(result, BriefingResponse)
    assert result.sections == []
    assert result.patient_id == "pt-001"

    # One ERROR record mentioning schema validation, patient_id, and the offending payload.
    schema_records = [
        r for r in caplog.records
        if "failed schema validation" in r.getMessage()
    ]
    assert len(schema_records) == 1
    msg = schema_records[0].getMessage()
    assert "pt-001" in msg
    assert "source_dt" in msg or "generated_at" in msg
