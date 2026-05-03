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
from agent.dispatcher import (
    _STRUCTURED_RESPONSE_TYPES,
    _user_intent_matches_response,
    dispatch,
)


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


# User messages whose intent matches each structured response_type — used by
# the framing-skip tests since the gate now requires intent-match to fire.
_INTENT_MATCHING_MESSAGE: dict[str, str] = {
    "census": "show me the census",
    "briefing": "brief Marcus Webb",
    "medication_safety": "any allergies for him",
    "handoff": "generate the sign-out for my list",
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
            message=_INTENT_MATCHING_MESSAGE[response_type],
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


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_structured_response_chains_when_intent_mismatches() -> None:
    """When the model calls a structured tool as a *resolution step* (intent
    mismatch), the dispatcher must NOT skip framing — it must loop so the
    model can chain to the actually-requested tool.

    Scenario: physician asks "Brief Marcus Webb". Model first calls
    get_census_summary (to resolve the name to an ID).  Census's
    response_type is structured, but the user's intent is "briefing", so
    the gate must NOT fire.  The dispatcher loops; the model then calls
    get_patient_briefing on the resolved ID.  Final envelope must be
    type=briefing, NOT census.
    """
    census_payload = {"patients": [{"patient_id": "pt-001", "name": "Marcus Webb"}]}
    briefing_payload = {"patient_id": "pt-001", "sections": []}

    fake_create = AsyncMock(side_effect=[
        _planner_response("get_census_summary", {"patient_ids": []}),
        _planner_response("get_patient_briefing", {"patient_id": "pt-001"}),
    ])
    fake_census = AsyncMock(return_value={"result": census_payload, "citations": []})
    fake_briefing = AsyncMock(return_value={"result": briefing_payload, "citations": []})

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="Brief Marcus Webb",
            session_id="sess-chain",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    # Two planner calls — the dispatcher did NOT structured-skip after census.
    assert fake_create.await_count == 2, (
        f"Expected two planner calls (census resolution → briefing chain); "
        f"got {fake_create.await_count}.  The intent-mismatch gate regressed."
    )
    # Census tool ran (resolution), briefing tool ran (final answer).
    assert fake_census.await_count == 1
    assert fake_briefing.await_count == 1
    # Final response is the briefing — what the user actually asked for —
    # NOT the census resolution step.  The dispatcher's capture logic
    # overrides final_data when a later structured tool's inferred type
    # matches the user's intent.
    assert result["type"] == "briefing"
    assert result["data"] == briefing_payload


@pytest.mark.hard_failure
def test_intent_match_helper() -> None:
    """Direct unit test of _user_intent_matches_response."""
    # Census matches
    assert _user_intent_matches_response("show me the census", "census")
    assert _user_intent_matches_response("morning rounds please", "census")
    assert _user_intent_matches_response("triage the list", "census")

    # Briefing matches
    assert _user_intent_matches_response("Brief Marcus Webb", "briefing")
    assert _user_intent_matches_response("Pre-encounter briefing for Delia", "briefing")
    assert _user_intent_matches_response("Tell me about the patient in bed 502", "briefing")
    assert _user_intent_matches_response("what happened overnight", "briefing")

    # Medication safety matches
    assert _user_intent_matches_response("any allergies for him", "medication_safety")
    assert _user_intent_matches_response("check medication safety", "medication_safety")
    assert _user_intent_matches_response("any concerns with his meds", "medication_safety")

    # Handoff matches
    assert _user_intent_matches_response("generate the sign-out", "handoff")
    assert _user_intent_matches_response("end of rounds handoff", "handoff")

    # Negative cases — intent does NOT match the structured response_type.
    # "Brief Marcus" does not match census even though census might fire as a
    # resolution step.
    assert not _user_intent_matches_response("Brief Marcus Webb", "census")
    assert not _user_intent_matches_response("show me the census", "briefing")
    assert not _user_intent_matches_response("what was the potassium", "census")
    assert not _user_intent_matches_response("what was the potassium", "briefing")
    # Unknown response_type → False.
    assert not _user_intent_matches_response("anything", "unknown_type")
