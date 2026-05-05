"""LangGraph factory for the Week-2 pipeline.

Topology (post-slice 3.2–3.8)
-----------------------------

    supervisor --> {extractor | structured | retriever | finalize}
    extractor --> demographics_stub --> critic_stub --> finalize --> END
    structured --> critic_stub --> finalize --> END
    retriever --> critic_stub --> finalize --> END

Stubs that remain in this file
------------------------------

* ``demographics_stub`` — owned by the parallel agent (slice 3.7); kept as
  a passthrough until that lands.
* ``critic_stub`` — owned by the parallel agent (slice 3.6); MUST remain
  untouched in this slice. It returns ``critic_decision="pass"``.

Checkpointer
------------
LangGraph 0.2.x expects a `BaseCheckpointSaver` from
`langgraph.checkpoint.base`. The repo's `checkpointer.RedisSaver` is a
thin custom hash-based store with a different surface (append/load/clear),
so it does not satisfy that interface out of the box. For tests, pass
`langgraph.checkpoint.memory.MemorySaver()`.

TODO(slice-3.x): write `graph.checkpointer_adapter.LangGraphRedisSaver`
that wraps `checkpointer.RedisSaver`.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from .nodes.extractor import extractor_node
from .nodes.finalize import finalize_node
from .nodes.retriever import retriever_node
from .nodes.structured import structured_node
from .nodes.supervisor import supervisor
from .state import W2State

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stubs owned by parallel agents — DO NOT REPLACE in slices 3.2–3.5/3.8.
# ---------------------------------------------------------------------------

async def demographics_stub(state: W2State) -> dict[str, Any]:
    """Placeholder for Slice 3.7 (wrong-patient detection).

    Owned by the parallel agent; this passthrough keeps the topology
    connected until that node lands.
    """
    return {}


async def critic_stub(state: W2State) -> dict[str, Any]:
    """Passthrough critic that always passes.

    Owned by the parallel agent (Slice 3.6). Do not modify in this slice.
    """
    return {"critic_decision": "pass"}


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

_SUPERVISOR_ROUTES: dict[str, str] = {
    "extractor": "extractor",
    "structured": "structured",
    "retriever": "retriever",
    "finalize": "finalize",
}


def _route_from_supervisor(state: W2State) -> str:
    """Map supervisor's ``next_node`` field to a graph node key."""
    requested = state.get("next_node") or "finalize"
    return _SUPERVISOR_ROUTES.get(requested, "finalize")


def build_graph() -> StateGraph:
    """Build (but do not compile) the W2 graph."""
    graph: StateGraph = StateGraph(W2State)

    graph.add_node("supervisor", supervisor)
    graph.add_node("extractor", extractor_node)
    graph.add_node("structured", structured_node)
    graph.add_node("retriever", retriever_node)
    graph.add_node("demographics_stub", demographics_stub)
    graph.add_node("critic_stub", critic_stub)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route_from_supervisor,
        {
            "extractor": "extractor",
            "structured": "structured",
            "retriever": "retriever",
            "finalize": "finalize",
        },
    )
    graph.add_edge("extractor", "demographics_stub")
    graph.add_edge("demographics_stub", "critic_stub")
    graph.add_edge("structured", "critic_stub")
    graph.add_edge("retriever", "critic_stub")
    graph.add_edge("critic_stub", "finalize")
    graph.add_edge("finalize", END)

    return graph


def compile_graph(*, checkpointer: Any | None = None) -> Any:
    """Build and compile the graph against the supplied checkpointer."""
    graph = build_graph()
    return graph.compile(checkpointer=checkpointer)


__all__ = ["build_graph", "compile_graph"]
