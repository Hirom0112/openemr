"""LangGraph factory for the Week-2 pipeline (Slice 3.1 skeleton).

This slice stands up the topology and proves end-to-end execution. All
nodes are stubs; later slices replace each one with real workers/critic/
finalize logic.

Topology
--------
    supervisor_stub --(state["next_node"])--> worker_stub | finalize_stub
    worker_stub --> critic_stub --> finalize_stub --> END

Checkpointer
------------
LangGraph 0.2.x expects a `BaseCheckpointSaver` from
`langgraph.checkpoint.base`. The repo's `checkpointer.RedisSaver` is a
thin custom hash-based store with a different surface (append/load/clear),
so it does not satisfy that interface out of the box. For the skeleton we
let callers pass any LangGraph-compatible saver (typically `MemorySaver`
in tests, the eventual real adapter in production). Slice 3.x will add a
proper `RedisSaver -> BaseCheckpointSaver` adapter.

TODO(slice-3.x): write `graph.checkpointer_adapter.LangGraphRedisSaver`
that wraps `checkpointer.RedisSaver` and implements `aget_tuple`, `alist`,
`aput`, `aput_writes` against the existing copilot:checkpoint:* keys.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from .state import W2State

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stub nodes
# ---------------------------------------------------------------------------

async def supervisor_stub(state: W2State) -> dict[str, Any]:
    """Decide the next node based on whether we have a file or a message."""
    has_file = bool(state.get("file_bytes_ref"))
    has_message = bool((state.get("message") or "").strip())
    next_node = "worker_stub" if (has_file or has_message) else "finalize_stub"

    logger.info(
        "graph_supervisor_routed",
        extra={
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
            "has_file": has_file,
            "has_message": has_message,
            "next_node": next_node,
        },
    )
    return {"next_node": next_node}


async def worker_stub(state: W2State) -> dict[str, Any]:
    """Passthrough worker that emits a stub structured response."""
    return {
        "structured_response": {
            "narrative": "stub",
            "data": None,
            "citations": [],
        }
    }


async def critic_stub(state: W2State) -> dict[str, Any]:
    """Passthrough critic that always passes."""
    return {"critic_decision": "pass"}


async def finalize_stub(state: W2State) -> dict[str, Any]:
    """Terminal node — logs shape only, never the structured payload."""
    sr = state.get("structured_response")
    logger.info(
        "graph_finalize_complete",
        extra={
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
            "has_structured_response": sr is not None,
            "critic_decision": state.get("critic_decision"),
        },
    )
    return {}


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

def _route_from_supervisor(state: W2State) -> str:
    """Map supervisor's `next_node` field to a successor key."""
    return state.get("next_node") or "finalize_stub"


def build_graph() -> StateGraph:
    """Build (but do not compile) the W2 graph with stub nodes wired in.

    Returns the uncompiled `StateGraph` so callers can either compile it
    themselves with a custom checkpointer or pass it through `compile_graph`.
    """
    graph: StateGraph = StateGraph(W2State)

    graph.add_node("supervisor_stub", supervisor_stub)
    graph.add_node("worker_stub", worker_stub)
    graph.add_node("critic_stub", critic_stub)
    graph.add_node("finalize_stub", finalize_stub)

    graph.set_entry_point("supervisor_stub")
    graph.add_conditional_edges(
        "supervisor_stub",
        _route_from_supervisor,
        {
            "worker_stub": "worker_stub",
            "finalize_stub": "finalize_stub",
        },
    )
    graph.add_edge("worker_stub", "critic_stub")
    graph.add_edge("critic_stub", "finalize_stub")
    graph.add_edge("finalize_stub", END)

    return graph


def compile_graph(*, checkpointer: Any | None = None) -> Any:
    """Build and compile the graph against the supplied checkpointer.

    The caller owns the checkpointer lifecycle. For tests, pass
    `langgraph.checkpoint.memory.MemorySaver()`. For production, pass the
    real LangGraph-compatible saver once Slice 3.x lands the adapter.
    """
    graph = build_graph()
    return graph.compile(checkpointer=checkpointer)


__all__ = ["build_graph", "compile_graph"]
