"""Tool registry — maps tool names to callable implementations.

TOOL_REGISTRY       : five conversational tools registered with the dispatcher.
DIRECT_TOOL_REGISTRY: one direct-call tool (get_triage_rationale) invoked by
                      POST /agent/triage_rationale without going through the
                      dispatcher loop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.tools import (
    generate_handoff,
    get_census_summary,
    get_medication_safety,
    get_patient_briefing,
    get_triage_rationale,
    query_patient_records,
)

# Conversational tools — registered with the dispatcher.
# get_triage_rationale is intentionally excluded: it is a direct-call tool.
TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "get_census_summary": get_census_summary,
    "get_patient_briefing": get_patient_briefing,
    "query_patient_records": query_patient_records,
    "get_medication_safety": get_medication_safety,
    "generate_handoff": generate_handoff,
}

# Direct-call tools — invoked by the React panel without dispatcher involvement.
DIRECT_TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "get_triage_rationale": get_triage_rationale,
}
