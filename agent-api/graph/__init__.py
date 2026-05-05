"""Week-2 LangGraph package — typed state + graph factory.

Public surface: `W2State`, `make_initial_state`, `build_graph`, `compile_graph`.
Per `.importlinter`, this package must not import FastAPI / Starlette
primitives — graph nodes are pure-async functions called by the route
handler.
"""
from __future__ import annotations

from .build import build_graph, compile_graph
from .state import W2State, make_initial_state

__all__ = [
    "W2State",
    "build_graph",
    "compile_graph",
    "make_initial_state",
]
