"""Single shared helper for per-tool decision-log emission.

Each tool ends with one INFO line carrying tool_name, duration_ms, cache state,
and any tool-specific small ID. Keep this file dependency-free so it can be
imported by ``agent.tools`` without inverting the agent->observability arrow.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

CacheState = Literal["hit", "miss", "n/a"]

_logger = logging.getLogger("agent.tool")


def log_tool_outcome(
    *,
    tool_name: str,
    duration_ms: int,
    cache: CacheState,
    session_id: str | None = None,
    patient_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one INFO log line summarising a tool invocation."""
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "duration_ms": duration_ms,
        "cache": cache,
    }
    if session_id is not None:
        payload["session_id"] = session_id
    if patient_id is not None:
        payload["patient_id"] = patient_id
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)
    _logger.info("tool_outcome", extra=payload)
