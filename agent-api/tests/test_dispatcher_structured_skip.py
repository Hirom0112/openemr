"""Tests for the structured-response framing-skip shortcut in dispatcher.dispatch.

Verifies that for every structured-renderer response type (census, briefing,
medication_safety, handoff, query_answer) the dispatcher makes exactly ONE
Anthropic call (the planner) and skips the second/framing call when the
user's intent matches the response type.  Free-text ``text`` still incurs
two Anthropic calls.  ``query_answer`` falls back to two calls when the
user's intent does not look like a direct records lookup.

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
    "query_answer": "what was the last potassium",
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
    if response_type == "query_answer":
        return {"found": True, "answer": "Last potassium 4.2 mEq/L on 2026-04-30."}
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
async def test_query_answer_falls_through_when_intent_mismatches() -> None:
    """When the model dispatches query_patient_records but the user's message
    doesn't read like a direct records lookup, the structured-skip gate must
    NOT fire and the dispatcher must run the framing turn (so the LLM can
    re-frame or chain).
    """
    tool_name = _TOOL_FOR_RESPONSE_TYPE["query_answer"]
    payload = {"found": True, "answer": "potassium was 4.2"}

    fake_create = AsyncMock(side_effect=[
        _planner_response(tool_name),
        _framing_response("Potassium was 4.2 mEq/L."),
    ])
    fake_tool = AsyncMock(return_value={"result": payload, "citations": []})

    # "do the thing" matches none of the query_answer intent hints, so the
    # framing turn must still run.
    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="do the thing",
            session_id="sess-test-qa-mismatch",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 2, (
        "query_answer with intent-mismatch must still make planner + framing; "
        f"got {fake_create.await_count}."
    )
    assert result["type"] == "query_answer"
    assert result["narrative"] == "Potassium was 4.2 mEq/L."


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_query_answer_skip_enriches_history_with_patient_context() -> None:
    """When query_answer skips framing, the placeholder narrative persisted to
    history must include the patient name + answer snippet so follow-up turns
    ("what about her potassium?") can resolve the pronoun.
    """
    tool_name = _TOOL_FOR_RESPONSE_TYPE["query_answer"]
    payload = {
        "found": True,
        "answer": "Last potassium was 4.2 mEq/L on 2026-04-30.",
        "name": "Sara Chen",
        "patient_id": "pt-007",
    }

    fake_create = AsyncMock(side_effect=[
        _planner_response(tool_name, {"patient_id": "pt-007"}),
    ])
    fake_tool = AsyncMock(return_value={"result": payload, "citations": []})

    saved: list[tuple[str, Any]] = []

    async def _capture_save(session_id: str, ctx: dict[str, Any], role: str, content: Any) -> None:
        saved.append((role, content))

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.object(dispatcher, "_save_turn", _capture_save), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="what was the last potassium",
            session_id="sess-test-qa-history",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 1
    assert result["type"] == "query_answer"

    # Pull out the assistant text block persisted by the structured-skip path.
    assistant_texts = [
        block.get("text")
        for role, content in saved
        if role == "assistant" and isinstance(content, list)
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert assistant_texts, "structured-skip must persist an assistant text block"
    placeholder = assistant_texts[-1]
    assert isinstance(placeholder, str) and placeholder
    # Must carry the patient name so pronoun resolution has context.
    assert "Sara Chen" in placeholder, placeholder
    # Must carry a fragment of the answer so the LLM can recall the prior fact.
    assert "potassium" in placeholder.lower(), placeholder
    assert "4.2" in placeholder, placeholder

    # The returned envelope's narrative must match the persisted placeholder.
    assert result["narrative"] == placeholder


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_census_chains_to_query_answer_when_intent_mismatches_census() -> None:
    """Census fired as a resolution step before query_patient_records: the
    intent-mismatch gate on census must NOT fire (so the loop continues),
    and the final response_type must be query_answer.
    """
    census_payload = {"patients": [{"patient_id": "pt-007", "name": "Sara Chen"}]}
    qa_payload = {"found": True, "answer": "Potassium 4.2 mEq/L."}

    fake_create = AsyncMock(side_effect=[
        _planner_response("get_census_summary", {"patient_ids": []}),
        _planner_response("query_patient_records", {"patient_id": "pt-007", "question": "potassium"}),
    ])
    fake_census = AsyncMock(return_value={"result": census_payload, "citations": []})
    fake_qa = AsyncMock(return_value={"result": qa_payload, "citations": []})

    async def _fake_load(session_id: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "earlier turn"}]

    with patch.object(dispatcher, "_load_history", _fake_load), \
         patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "query_patient_records": fake_qa,
             },
             clear=False,
         ):
        result = await dispatch(
            message="what was Sara Chen's last potassium",
            session_id="sess-chain-qa",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    # Two planner calls: dispatcher did NOT skip after the census resolution.
    assert fake_create.await_count == 2
    assert fake_census.await_count == 1
    assert fake_qa.await_count == 1
    assert result["type"] == "query_answer"
    assert result["data"] == qa_payload


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

    # Seed prior history so the deterministic briefing fast path bows out
    # (cold-start gate) and the LLM dispatch loop runs.  This test still
    # protects the resolution-step → final-answer chaining behavior in the
    # planner; the fast path is exercised separately in
    # ``tests/test_dispatcher_fast_path.py``.
    async def _fake_load(session_id: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "earlier turn"}]

    with patch.object(dispatcher, "_load_history", _fake_load), \
         patch.object(dispatcher._anthropic.messages, "create", fake_create), \
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

    # Query answer matches (per latency-bench keyword set)
    assert _user_intent_matches_response("what was the last potassium", "query_answer")
    assert _user_intent_matches_response("creatinine trend over the last week", "query_answer")
    assert _user_intent_matches_response("echo show any wall motion abnormality", "query_answer")
    assert _user_intent_matches_response("what is the current sodium", "query_answer")
    # Mismatch: pure briefing intent should not match query_answer.
    assert not _user_intent_matches_response("brief Marcus Webb", "query_answer")

    # Negative cases — intent does NOT match the structured response_type.
    # "Brief Marcus" does not match census even though census might fire as a
    # resolution step.
    assert not _user_intent_matches_response("Brief Marcus Webb", "census")
    assert not _user_intent_matches_response("show me the census", "briefing")
    assert not _user_intent_matches_response("what was the potassium", "census")
    assert not _user_intent_matches_response("what was the potassium", "briefing")
    # Unknown response_type → False.
    assert not _user_intent_matches_response("anything", "unknown_type")
