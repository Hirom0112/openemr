"""Tests that the dispatcher surfaces patient identity on response.metadata.

The UI uses these fields to:
  * render the "Verify in Chart ↗" button on free-text responses whose
    ``response.data`` carries no ``patient_id`` (text, query_answer,
    medication_safety) — see ``chartPatientIdForResponse`` in
    ``agent-ui/src/components/ChatSurface.tsx``.
  * render a prominent patient banner above the answer so the physician
    can confirm identity at a glance, especially after pronoun resolution
    ("can i give her tylenol?" → answer should clearly show the patient).

Contract:
  * ``metadata.patient_id`` is set when the most recent SUCCESSFUL tool
    call had a ``patient_id`` in its input.
  * ``metadata.patient_name`` is set when the most recent successful tool
    result has a top-level ``name`` (or ``patient_name``) string.
  * Failed tool calls do NOT establish patient context.
  * The fast-path response also populates these fields.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher  # noqa: E402
from agent.dispatcher import dispatch  # noqa: E402


def _planner_response(
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    tool_use_id: str = "tu_1",
) -> SimpleNamespace:
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input or {},
        id=tool_use_id,
    )
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="tool_use", content=[tool_use], usage=usage)


def _framing_response(text: str = "Done.") -> SimpleNamespace:
    text_block = SimpleNamespace(text=text)
    text_block.type = "text"
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="end_turn", content=[text_block], usage=usage)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_metadata_carries_patient_id_from_successful_tool_call() -> None:
    """A successful tool call with ``patient_id`` in input surfaces it on metadata."""
    tool_name = "query_patient_records"
    fake_create = AsyncMock(
        side_effect=[
            _planner_response(tool_name, {"patient_id": "pt-007"}),
            _framing_response("Tylenol is safe."),
        ]
    )
    fake_tool = AsyncMock(return_value={"result": {"answer": "ok"}, "citations": []})

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="can i give her tylenol?",
            session_id="sess-meta-pid",
            session_context={"provider_id": "prov-1", "patient_ids": ["pt-007"]},
        )

    assert result["metadata"].get("patient_id") == "pt-007"


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_metadata_carries_patient_name_from_tool_result_name_field() -> None:
    """A successful tool result with a top-level ``name`` surfaces patient_name."""
    tool_name = "get_patient_briefing"
    fake_create = AsyncMock(
        side_effect=[
            _planner_response(tool_name, {"patient_id": "pt-007"}),
        ]
    )
    fake_tool = AsyncMock(
        return_value={
            "result": {
                "patient_id": "pt-007",
                "name": "YVONNE CASTILLO",
                "sections": [],
                "alerts": [],
            },
            "citations": [],
        }
    )

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="brief Yvonne",
            session_id="sess-meta-name",
            session_context={"provider_id": "prov-1", "patient_ids": ["pt-007"]},
        )

    assert result["metadata"].get("patient_id") == "pt-007"
    assert result["metadata"].get("patient_name") == "YVONNE CASTILLO"


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_metadata_reflects_last_successful_tool_call_when_chained() -> None:
    """When two successful tool calls run, metadata reflects the LAST one."""
    tool_name = "query_patient_records"
    fake_create = AsyncMock(
        side_effect=[
            _planner_response(tool_name, {"patient_id": "pt-001"}, tool_use_id="tu_a"),
            _planner_response(tool_name, {"patient_id": "pt-002"}, tool_use_id="tu_b"),
            _framing_response("Final."),
        ]
    )

    call_seq = {"n": 0}

    async def _tool(tool_input: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]:
        call_seq["n"] += 1
        if call_seq["n"] == 1:
            return {"result": {"answer": "first", "name": "Marcus Webb"}, "citations": []}
        return {"result": {"answer": "second", "name": "Sara Chen"}, "citations": []}

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: _tool}, clear=False):
        result = await dispatch(
            message="follow-up",
            session_id="sess-meta-last",
            session_context={
                "provider_id": "prov-1",
                "patient_ids": ["pt-001", "pt-002"],
            },
        )

    assert result["metadata"].get("patient_id") == "pt-002"
    assert result["metadata"].get("patient_name") == "Sara Chen"


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_failed_tool_call_does_not_pollute_metadata() -> None:
    """A FAILED tool call must NOT establish patient_id / patient_name."""
    tool_name = "query_patient_records"
    fake_create = AsyncMock(
        side_effect=[
            _planner_response(tool_name, {"patient_id": "pt-009"}),
            _framing_response("I couldn't fetch that."),
        ]
    )
    fake_tool = AsyncMock(side_effect=RuntimeError("FHIR upstream 502"))

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="what about Yvonne?",
            session_id="sess-meta-failed",
            session_context={"provider_id": "prov-1", "patient_ids": ["pt-009"]},
        )

    md = result["metadata"]
    assert "patient_id" not in md
    assert "patient_name" not in md


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_fast_path_dispatch_populates_metadata_patient_id() -> None:
    """The deterministic briefing fast path also surfaces patient identity."""
    fake_create = AsyncMock()  # never called on fast path
    fake_census = AsyncMock(
        return_value={
            "result": {
                "census": [{"patient_id": "pt-001", "name": "Marcus Webb"}],
                "total": 1,
                "requested": 0,
                "dropped": 0,
                "dropped_ids": [],
            },
            "citations": [],
            "metadata": {},
        }
    )
    fake_briefing = AsyncMock(
        return_value={
            "result": {
                "patient_id": "pt-001",
                "name": "Marcus Webb",
                "sections": [],
                "alerts": [],
            },
            "citations": [{"patient_id": "pt-001", "resource_type": "Patient"}],
            "metadata": {"tool": "get_patient_briefing"},
        }
    )

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
            message="brief Marcus Webb",
            session_id="sess-fp-meta",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 0, "fast path must not call Anthropic"
    md = result["metadata"]
    assert md.get("fast_path") is True
    assert md.get("patient_id") == "pt-001"
    assert md.get("patient_name") == "Marcus Webb"
