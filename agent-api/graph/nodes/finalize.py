"""Slice 3.8 — Finalize node + SSE-frame helper.

Pure aggregator: packs the W2State pieces into a final response envelope
on ``state["finalized"]``. The SSE wrapping happens in the route handler
(slice 3.9); the finalize concern owns the SSE *format* via :func:`sse_frame`.
"""
from __future__ import annotations

import json as _json
import logging
from typing import Any

from ..state import W2State

logger = logging.getLogger(__name__)


async def finalize_node(state: W2State) -> dict[str, Any]:
    """Aggregate state pieces into a single ``finalized`` envelope."""
    finalized = {
        "extraction": state.get("extraction"),
        "retrieval": state.get("retrieval"),
        "critic_decision": state.get("critic_decision"),
        "soft_warns": state.get("soft_warns") or [],
        "structured_response": state.get("structured_response"),
        "errors": state.get("errors") or [],
    }
    logger.info(
        "graph_finalize_complete",
        extra={
            "has_extraction": bool(state.get("extraction")),
            "critic_decision": state.get("critic_decision"),
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
        },
    )
    return {"finalized": finalized}


def sse_frame(event: str, data: dict) -> str:
    """Format a Server-Sent Event frame matching ``main.py``'s ``_sse_format``."""
    return f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"


__all__ = ["finalize_node", "sse_frame"]
