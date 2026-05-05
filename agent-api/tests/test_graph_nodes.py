"""Slices 3.2–3.5, 3.8 — node-level tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from graph import compile_graph, make_initial_state
from graph.nodes.extractor import extractor_node
from graph.nodes.finalize import finalize_node, sse_frame
from graph.nodes.retriever import retriever_node
from graph.nodes.structured import structured_node
from graph.nodes.supervisor import supervisor
from graph.state import make_initial_state as _mis

pytestmark = pytest.mark.hard_failure


# ── supervisor ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supervisor_routes_file_to_extractor() -> None:
    state = _mis(
        request_id="r1", session_id="s1", provider_id="p1", file_bytes_ref="x"
    )
    out = await supervisor(state)
    assert out["next_node"] == "extractor"


@pytest.mark.asyncio
async def test_supervisor_routes_question_with_facts_to_retriever() -> None:
    state = _mis(
        request_id="r2", session_id="s2", provider_id="p1", message="hello"
    )
    state["extraction"] = {"kind": "lab_report"}
    out = await supervisor(state)
    assert out["next_node"] == "retriever"


@pytest.mark.asyncio
async def test_supervisor_routes_structured_query_to_dispatcher() -> None:
    state = _mis(
        request_id="r3", session_id="s3", provider_id="p1", message="show vitals"
    )
    out = await supervisor(state)
    assert out["next_node"] == "structured"


@pytest.mark.asyncio
async def test_supervisor_routes_empty_to_finalize() -> None:
    state = _mis(request_id="r4", session_id="s4", provider_id="p1")
    out = await supervisor(state)
    assert out["next_node"] == "finalize"


@pytest.mark.asyncio
async def test_supervisor_emits_audit_row() -> None:
    state = _mis(
        request_id="r5", session_id="s5", provider_id="p1", message="hi"
    )
    with patch("graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()) as mock_emit:
        await supervisor(state)
    assert mock_emit.await_count == 1
    event = mock_emit.await_args.args[0]
    assert event.event_type == "node_handoff"
    assert event.detail_json["from_node"] == "supervisor"
    assert event.detail_json["to_node"] == "structured"


# ── extractor ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extractor_node_no_provider_routes_to_finalize() -> None:
    state = _mis(
        request_id="r6", session_id="s6", provider_id="p1", file_bytes_ref="x"
    )
    with patch("graph.nodes.extractor.audit_writer.emit", new=AsyncMock()):
        out = await extractor_node(state, file_bytes_provider=None)
    assert out["next_node"] == "finalize"
    assert any("extractor" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_extractor_node_happy_path() -> None:
    state = _mis(
        request_id="r7",
        session_id="s7",
        provider_id="p1",
        patient_id="pat-1",
        file_bytes_ref="ref-1",
    )

    class _FakeExtraction:
        def model_dump(self, mode: str = "json") -> dict:
            return {"kind": "lab_report", "values": []}

    async def _provider(_ref: str) -> bytes:
        return b"fake-pdf"

    with patch(
        "graph.nodes.extractor.extract", new=AsyncMock(return_value=_FakeExtraction())
    ), patch("graph.nodes.extractor.audit_writer.emit", new=AsyncMock()):
        out = await extractor_node(state, file_bytes_provider=_provider)

    assert out["extraction"]["kind"] == "lab_report"
    assert out["next_node"] == "demographics"


@pytest.mark.asyncio
async def test_extractor_node_handles_extraction_failed() -> None:
    from extractors.lab import ExtractionFailed

    state = _mis(
        request_id="r8",
        session_id="s8",
        provider_id="p1",
        patient_id="pat-1",
        file_bytes_ref="ref-1",
    )

    async def _provider(_ref: str) -> bytes:
        return b"x"

    with patch(
        "graph.nodes.extractor.extract",
        new=AsyncMock(side_effect=ExtractionFailed("vision call failed")),
    ), patch("graph.nodes.extractor.audit_writer.emit", new=AsyncMock()):
        out = await extractor_node(state, file_bytes_provider=_provider)

    assert out["next_node"] == "finalize"
    assert any("extraction failed" in e for e in out["errors"])


# ── structured ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_structured_node_calls_dispatch() -> None:
    state = _mis(
        request_id="r9", session_id="s9", provider_id="p1", message="show meds"
    )
    fake_response = {"narrative": "ok", "data": {"x": 1}, "citations": []}
    with patch(
        "agent.dispatcher.dispatch",
        new=AsyncMock(return_value=fake_response),
    ) as mock_dispatch, patch(
        "graph.nodes.structured.audit_writer.emit", new=AsyncMock()
    ):
        out = await structured_node(state)
    mock_dispatch.assert_awaited_once()
    kwargs = mock_dispatch.await_args.kwargs
    assert kwargs["message"] == "show meds"
    assert kwargs["session_id"] == "s9"
    assert out["structured_response"] == fake_response


@pytest.mark.asyncio
async def test_structured_node_handles_dispatch_failure() -> None:
    state = _mis(
        request_id="r10", session_id="s10", provider_id="p1", message="boom"
    )
    with patch(
        "agent.dispatcher.dispatch",
        new=AsyncMock(side_effect=RuntimeError("nope")),
    ), patch("graph.nodes.structured.audit_writer.emit", new=AsyncMock()):
        out = await structured_node(state)
    assert out["structured_response"]["narrative"] == "Internal error"
    assert out["structured_response"]["data"] is None
    assert any("structured" in e for e in out["errors"])


# ── retriever ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retriever_stub_returns_empty_snippets() -> None:
    state = _mis(request_id="r11", session_id="s11", provider_id="p1")
    with patch("graph.nodes.retriever.audit_writer.emit", new=AsyncMock()):
        out = await retriever_node(state)
    assert out["retrieval"]["snippets"] == []
    assert out["retrieval"]["fallback_used"] is False


# ── finalize ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finalize_aggregates_state() -> None:
    state = _mis(request_id="r12", session_id="s12", provider_id="p1")
    state["extraction"] = {"kind": "lab_report"}
    state["retrieval"] = {"snippets": [], "fallback_used": False}
    state["critic_decision"] = "pass"
    state["soft_warns"] = []
    state["structured_response"] = {"narrative": "ok", "data": None, "citations": []}
    out = await finalize_node(state)
    assert "finalized" in out
    fin = out["finalized"]
    for key in (
        "extraction",
        "retrieval",
        "critic_decision",
        "soft_warns",
        "structured_response",
        "errors",
    ):
        assert key in fin
    assert fin["critic_decision"] == "pass"


def test_sse_frame_format() -> None:
    out = sse_frame("node_complete", {"node": "x"})
    assert out == 'event: node_complete\ndata: {"node": "x"}\n\n'


# ── full graph ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_graph_runs_with_message() -> None:
    stub = {"narrative": "ok", "data": None, "citations": []}
    with patch(
        "agent.dispatcher.dispatch", new=AsyncMock(return_value=stub)
    ), patch("graph.nodes.structured.audit_writer.emit", new=AsyncMock()), patch(
        "graph.nodes.supervisor.audit_writer.emit", new=AsyncMock()
    ):
        compiled = compile_graph(checkpointer=MemorySaver())
        initial = make_initial_state(
            request_id="req-full",
            session_id="sess-full",
            provider_id="prov-1",
            message="hi",
        )
        config = {"configurable": {"thread_id": "sess-full"}}
        final = await compiled.ainvoke(initial, config=config)
    assert final["critic_decision"] == "pass"
    assert final.get("finalized") is not None
    assert final["finalized"]["structured_response"] == stub
