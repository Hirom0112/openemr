"""Cross-cutting observability primitives (logging, request_id, tool log helper).

Leaf package: must not import from any sibling business package (agent, triage,
briefing, etc.). Enforced via .importlinter.
"""

from observability.json_logging import (
    JsonLogFormatter,
    RequestIdFilter,
    configure_json_logging,
    request_id_var,
)
from observability.tool_logging import log_tool_outcome

__all__ = [
    "JsonLogFormatter",
    "RequestIdFilter",
    "configure_json_logging",
    "request_id_var",
    "log_tool_outcome",
]
