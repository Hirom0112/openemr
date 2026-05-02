"""Tests for the structured-response framing-skip shortcut in dispatcher.dispatch.

Verifies that for every structured-renderer response type (census, briefing,
medication_safety, handoff) the dispatcher makes exactly ONE Anthropic call
(the planner) and skips the second/framing call.  Free-text response types
(text, query_answer) still incur two Anthropic calls.

This protects the ~1.5–2 s per-turn latency win documented in
agent/dispatcher.py:_STRUCTURED_RESPONSE_TYPES.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher
from agent.dispatcher import _STRUCTURED_RESPONSE_TYPES, dispatch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _planner_response(tool_name: str, tool_input: dict[str, Any] | None = None) -> SimpleNamespace:
    """Build a fake Anthropic response that issues a single tool_use block."""
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input or {},
        id="tu_test_1",
    )
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[tool_use],
        usage=usage,
    )


def _framing_response(text: str = "Done.") -> SimpleNamespace:
    """Build a fake Anthropic response that ends the turn with a text block."""
    text_block = SimpleNamespace(text=text)
    # `b.type` is checked for tool_use detection; assign a non-tool_use type.
    text_block.type = "text"
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[text_block],
        usage=usage,
    )


_TOOL_FOR_RESPONSE_TYPE: dict[str, str] = {
    "census": "get_census_summary",
    "briefing": "get_patient_briefing",
    "medication_safety": "get_medication_safety",
    "handoff": "generate_handoff",
    "query_answer": "query_patient_records",
}


def _structured_payload(response_type: str) -> dict[str, Any]:
    """Minimal payload sufficient for _structured_skip_narrative."""
    if response_type == "handoff":
        return {"total": 2, "patients": [{"id": "p1"}, {"id": "p2"}]}
    if response_type == "census":
        return {"patients": [{"id": "p1"}]}
    if response_type == "briefing":
        return {"patient_id": "p1", "sections": []}
    if response_type == "medication_safety":
        return {"patient_id": "p1", "interactions": []}
    return {"answer": "ok"}


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("response_type", sorted(_STRUCTURED_RESPONSE_TYPES))
@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_structured_response_skips_framing_call(response_type: str) -> None:
    """For each structured renderer type, dispatch makes exactly ONE Anthropic call."""
    tool_name = _TOOL_FOR_RESPONSE_TYPE[response_type]
    payload = _structured_payload(response_type)

    fake_create = AsyncMock(side_effect=[_planner_response(tool_name)])
    fake_tool = AsyncMock(return_value={"result": payload, "citations": []})

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="please run it",
            session_id="sess-test",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 1, (
        f"{response_type}: expected ONE Anthropic call (planner only), "
        f"got {fake_create.await_count}.  The framing-skip shortcut regressed."
    )
    assert result["type"] == response_type
    assert result["data"] == payload
    # Narrative is a non-empty placeholder so verification + history stay well-formed.
    assert isinstance(result["narrative"], str)
    assert result["narrative"] != ""


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_query_answer_still_makes_framing_call() -> None:
    """Free-text query_answer must NOT use the shortcut — needs the framing pass."""
    tool_name = _TOOL_FOR_RESPONSE_TYPE["query_answer"]
    payload = {"answer": "potassium was 4.2"}

    fake_create = AsyncMock(side_effect=[
        _planner_response(tool_name),
        _framing_response("Potassium was 4.2 mEq/L."),
    ])
    fake_tool = AsyncMock(return_value={"result": payload, "citations": []})

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="what was the potassium?",
            session_id="sess-test-qa",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 2, (
        "query_answer must continue to make TWO Anthropic calls (planner + framing); "
        f"got {fake_create.await_count}."
    )
    assert result["type"] == "query_answer"
    assert result["narrative"] == "Potassium was 4.2 mEq/L."
