"""Slice 3.1 — LangGraph skeleton smoke tests.

Verifies:
  * W2State factory returns the expected required keys.
  * The graph compiles against `MemorySaver`.
  * End-to-end execution routes message-only inputs through the worker stub.
  * Empty inputs route straight to finalize and skip the worker.
  * File-only inputs reach the worker stub.
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from graph import W2State, build_graph, compile_graph, make_initial_state

pytestmark = pytest.mark.hard_failure


def test_w2state_initial_shape() -> None:
    state: W2State = make_initial_state(
        request_id="r", session_id="s", provider_id="p"
    )
    # Required keys present.
    for key in (
        "request_id",
        "patient_id",
        "session_id",
        "provider_id",
        "message",
        "file_bytes_ref",
        "doc_type_hint",
        "errors",
    ):
        assert key in state, f"missing required key {key!r}"
    assert state["request_id"] == "r"
    assert state["session_id"] == "s"
    assert state["provider_id"] == "p"
    assert state["patient_id"] is None
    assert state["message"] is None
    assert state["file_bytes_ref"] is None
    assert state["doc_type_hint"] is None
    assert state["errors"] == []


def test_graph_compiles() -> None:
    compiled = compile_graph(checkpointer=MemorySaver())
    assert compiled is not None
    # Sanity: the uncompiled builder also returns a usable StateGraph.
    assert build_graph() is not None


@pytest.mark.asyncio
async def test_graph_runs_end_to_end_with_message() -> None:
    compiled = compile_graph(checkpointer=MemorySaver())
    initial = make_initial_state(
        request_id="req-msg",
        session_id="sess-msg",
        provider_id="prov-1",
        message="hello",
    )
    config = {"configurable": {"thread_id": "sess-msg"}}
    final = await compiled.ainvoke(initial, config=config)
    assert final["critic_decision"] == "pass"
    assert final["structured_response"]["narrative"] == "stub"
    assert final["structured_response"]["citations"] == []


@pytest.mark.asyncio
async def test_graph_routes_to_finalize_when_empty() -> None:
    compiled = compile_graph(checkpointer=MemorySaver())
    initial = make_initial_state(
        request_id="req-empty",
        session_id="sess-empty",
        provider_id="prov-1",
    )
    config = {"configurable": {"thread_id": "sess-empty"}}
    final = await compiled.ainvoke(initial, config=config)
    # supervisor went straight to finalize → worker_stub never ran.
    assert "structured_response" not in final or final.get("structured_response") in (None, {})
    assert final.get("next_node") == "finalize_stub"


@pytest.mark.asyncio
async def test_supervisor_routes_file_to_worker() -> None:
    compiled = compile_graph(checkpointer=MemorySaver())
    initial = make_initial_state(
        request_id="req-file",
        session_id="sess-file",
        provider_id="prov-1",
        file_bytes_ref="ref-1",
    )
    config = {"configurable": {"thread_id": "sess-file"}}
    final = await compiled.ainvoke(initial, config=config)
    assert final.get("structured_response") is not None
    assert final["structured_response"]["narrative"] == "stub"
    assert final["critic_decision"] == "pass"
