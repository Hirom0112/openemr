"""LangGraph factory for the Week-2 pipeline.

Topology (post-slice 3.9)
-------------------------

    supervisor --> {intake_extractor | structured | evidence_retriever | finalize}
    intake_extractor --> demographics --> critic --> finalize --> END
    structured --> critic --> finalize --> END
    evidence_retriever --> critic --> finalize --> END

Provider injection
------------------
LangGraph nodes are unary functions of state, so the route handler injects
its per-request providers via ``functools.partial``:

* ``file_bytes_provider`` resolves an opaque ``file_bytes_ref`` to PDF
  bytes (extractor node).
* ``fhir_patient_provider`` resolves ``patient_id`` to a FHIR Patient JSON
  (demographics node).

Both are optional — when omitted the nodes degrade to in-process defaults
(extractor: skip + route to finalize; demographics: fall back to the
production :data:`auth.fhir_client.fhir_client`).

Checkpointer
------------
LangGraph 0.2.x expects a ``BaseCheckpointSaver`` from
``langgraph.checkpoint.base``. The repo's ``checkpointer.RedisSaver`` is a
thin custom hash-based store with a different surface (append/load/clear),
so it does not satisfy that interface out of the box. For tests, pass
``langgraph.checkpoint.memory.MemorySaver()``.

TODO(slice-3.x): write ``graph.checkpointer_adapter.LangGraphRedisSaver``
that wraps ``checkpointer.RedisSaver``.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, Optional

from langgraph.graph import END, StateGraph

from config import settings

from .nodes.critic import critic_node
from .nodes.demographics import demographics_node
from .nodes.extractor import extractor_node
from .nodes.finalize import finalize_node
from .nodes.retriever import retriever_node
from .nodes.structured import structured_node
from .nodes.supervisor import supervisor
from .state import W2State

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

_SUPERVISOR_ROUTES: dict[str, str] = {
    "intake_extractor": "intake_extractor",
    "structured": "structured",
    "evidence_retriever": "evidence_retriever",
    "finalize": "finalize",
}


def _route_from_supervisor(state: W2State) -> str:
    """Map supervisor's ``next_node`` field to a graph node key."""
    requested = state.get("next_node") or "finalize"
    return _SUPERVISOR_ROUTES.get(requested, "finalize")


def build_graph(
    *,
    file_bytes_provider: Callable[[str], Awaitable[bytes]] | None = None,
    fhir_patient_provider: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    page_bytes_provider: Optional[
        Callable[[dict[str, Any]], Awaitable[Optional[bytes]]]
    ] = None,
) -> StateGraph:
    """Build (but do not compile) the W2 graph.

    Provider callbacks are bound onto the relevant nodes via
    ``functools.partial`` so LangGraph's unary-function contract is
    preserved.
    """
    graph: StateGraph = StateGraph(W2State)

    # Bind injected providers onto their nodes — LangGraph nodes are
    # ``async def fn(state) -> dict``; partial keeps that signature.
    bound_extractor = functools.partial(
        extractor_node, file_bytes_provider=file_bytes_provider
    )
    bound_demographics = functools.partial(
        demographics_node, fhir_patient_provider=fhir_patient_provider
    )

    graph.add_node("supervisor", supervisor)
    graph.add_node("intake_extractor", bound_extractor)
    graph.add_node("structured", structured_node)
    graph.add_node("evidence_retriever", retriever_node)
    graph.add_node("demographics", bound_demographics)
    graph.add_node("critic", critic_node)
    graph.add_node("finalize", finalize_node)

    # ── Wave 2C — optional citation_verifier node ───────────────────────────
    # Inserted before the critic on the document path so the critic still
    # runs over the verified citations. Off-by-default behaviour: when
    # ``settings.verify_citations == "off"`` we skip the wiring entirely;
    # the node is not even registered, so the graph topology is unchanged.
    verify_mode = (settings.verify_citations or "off").lower()
    verifier_enabled = verify_mode in ("sample", "all")
    if verifier_enabled:
        from agent.citation_verifier import citation_verifier_node

        bound_verifier = functools.partial(
            citation_verifier_node, page_bytes_provider=page_bytes_provider
        )
        graph.add_node("citation_verifier", bound_verifier)

    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route_from_supervisor,
        {
            "intake_extractor": "intake_extractor",
            "structured": "structured",
            "evidence_retriever": "evidence_retriever",
            "finalize": "finalize",
        },
    )
    if verifier_enabled:
        # demographics → citation_verifier → critic. The verifier is a
        # post-extraction pass; demographics has already finished by the
        # time we run, and the critic reads the (possibly mutated)
        # extraction afterwards.
        graph.add_edge("intake_extractor", "demographics")
        graph.add_edge("demographics", "citation_verifier")
        graph.add_edge("citation_verifier", "critic")
    else:
        graph.add_edge("intake_extractor", "demographics")
        graph.add_edge("demographics", "critic")
    graph.add_edge("structured", "critic")
    graph.add_edge("evidence_retriever", "critic")
    graph.add_edge("critic", "finalize")
    graph.add_edge("finalize", END)

    return graph


def compile_graph(
    *,
    checkpointer: Any | None = None,
    file_bytes_provider: Callable[[str], Awaitable[bytes]] | None = None,
    fhir_patient_provider: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    page_bytes_provider: Optional[
        Callable[[dict[str, Any]], Awaitable[Optional[bytes]]]
    ] = None,
) -> Any:
    """Build and compile the graph with optional injected providers."""
    graph = build_graph(
        file_bytes_provider=file_bytes_provider,
        fhir_patient_provider=fhir_patient_provider,
        page_bytes_provider=page_bytes_provider,
    )
    return graph.compile(checkpointer=checkpointer)


__all__ = ["build_graph", "compile_graph"]
