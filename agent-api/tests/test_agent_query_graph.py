"""POST /agent/query is wired to the LangGraph supervisor pipeline.

Verifies:
  (a) /agent/query response is the dispatcher result surfaced through the
      graph's ``finalized.structured_response`` envelope (graph-derived).
  (b) The Stage-3 node rename (extractor -> intake_extractor,
      retriever -> evidence_retriever) didn't break supervisor routing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from graph import compile_graph, make_initial_state
from graph.nodes.supervisor import supervisor
from graph.state import make_initial_state as _mis

pytestmark = pytest.mark.hard_failure


# ── (b) supervisor routing post-rename ──────────────────────────────────────

@pytest.mark.asyncio
async def test_supervisor_returns_intake_extractor_after_rename() -> None:
    state = _mis(
        request_id="rq-rename-1",
        session_id="ss-rename-1",
        provider_id="pv-1",
        file_bytes_ref="ref-x",
    )
    out = await supervisor(state)
    assert out["next_node"] == "intake_extractor"


@pytest.mark.asyncio
async def test_supervisor_returns_evidence_retriever_after_rename() -> None:
    state = _mis(
        request_id="rq-rename-2",
        session_id="ss-rename-2",
        provider_id="pv-1",
        message="follow-up",
    )
    state["extraction"] = {"kind": "lab_report"}
    out = await supervisor(state)
    assert out["next_node"] == "evidence_retriever"


@pytest.mark.asyncio
async def test_compiled_graph_runs_with_renamed_nodes() -> None:
    """End-to-end: a message-only state runs supervisor -> structured ->
    critic -> finalize without raising on the renamed conditional edges."""
    stub = {"narrative": "ok", "data": None, "citations": []}
    with patch(
        "agent.dispatcher.dispatch", new=AsyncMock(return_value=stub)
    ), patch("graph.nodes.structured.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()
    ), patch("graph.nodes.critic.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.finalize.audit_writer.emit", new=AsyncMock()
    ):
        compiled = compile_graph(checkpointer=MemorySaver())
        initial = make_initial_state(
            request_id="rq-e2e",
            session_id="ss-e2e",
            provider_id="pv-1",
            message="hello",
        )
        final = await compiled.ainvoke(
            initial, config={"configurable": {"thread_id": "ss-e2e"}}
        )
    assert final.get("finalized") is not None
    assert final["finalized"]["structured_response"] == stub


# ── (a) /agent/query routes through the graph ──────────────────────────────

@pytest.mark.asyncio
async def test_agent_query_returns_graph_finalized_structured_response() -> None:
    """POST /agent/query result equals the graph's structured_response.

    Calls the endpoint function directly (bypassing the FastAPI lifespan
    hooks which require Redis / writable /data). Verifies the body is the
    dispatcher dict surfaced through ``finalized.structured_response``,
    proving the request flowed: supervisor -> structured -> critic ->
    finalize -> response.
    """
    import main as main_module

    stub = {
        "narrative": "graph-routed reply",
        "data": {"k": 1},
        "citations": [],
        "response_type": "text",
    }

    request_obj = main_module.AgentQueryRequest(
        message="what meds is the patient on",
        session_id="ss-q-1",
        provider_id="pv-1",
        patient_ids=["pat-1"],
    )

    with patch(
        "agent.dispatcher.dispatch", new=AsyncMock(return_value=stub)
    ), patch("graph.nodes.structured.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()
    ), patch("graph.nodes.critic.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.finalize.audit_writer.emit", new=AsyncMock()
    ):
        body = await main_module.agent_query(request_obj)

    # Must be the dispatcher dict surfaced through the graph (not a wrapped
    # envelope) — preserves the legacy /agent/query contract.
    assert body["narrative"] == "graph-routed reply"
    assert body["data"] == {"k": 1}
