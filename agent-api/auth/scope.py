"""Pre-tool-call patient-scope check.

Prevents the planner from "smuggling" tool calls for patients outside the
authenticated provider's current census. The dispatcher invokes
``check_patient_scope`` immediately before executing any tool_use block; on a
deny it surfaces an ``is_error=True`` tool_result back to the LLM rather than
crashing the loop.

Authoritative scope source: ``session_context["patient_ids"]`` — populated by
the HTTP layer from the inbound request body. If that list is missing or
empty we fail OPEN (allow + warn) during the rollout, then flip to fail-
closed once telemetry confirms the field is reliably populated.

This module is part of the ``auth`` leaf package. Per the import-linter
contract it must not import from any sibling business package.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tools whose ``patient_id`` argument identifies a single patient that must
# fall inside the current provider's census. Tools NOT in this set are
# treated as out-of-scope-of-the-check (e.g. ``get_census_summary`` IS the
# scope source itself; ``generate_handoff`` takes a list and is governed by
# the same upstream filter).
PATIENT_KEYED_TOOLS: frozenset[str] = frozenset(
    {
        "get_patient_briefing",
        "get_medication_safety",
        "query_patient_records",
        "get_triage_rationale",
    }
)


def check_patient_scope(
    tool_name: str,
    tool_input: dict[str, Any],
    session_context: dict[str, Any],
) -> tuple[bool, str | None]:
    """Decide whether ``tool_name`` may be invoked for this ``patient_id``.

    Returns ``(allowed, reason_if_not)``.  ``reason_if_not`` is non-None
    only when ``allowed`` is False and is suitable for surfacing to the LLM
    as the body of an ``is_error=True`` tool_result.

    Decisions:
      * Tool not in ``PATIENT_KEYED_TOOLS`` → allow.
      * No ``patient_id`` in ``tool_input`` → allow (nothing to scope).
      * ``session_context["patient_ids"]`` missing/empty → allow + warn
        (fail-open during rollout).
      * ``patient_id`` IS in census → allow.
      * ``patient_id`` is NOT in census → deny with reason.
    """
    if tool_name not in PATIENT_KEYED_TOOLS:
        return True, None

    requested = tool_input.get("patient_id")
    if requested is None or requested == "":
        return True, None

    raw_ids = session_context.get("patient_ids")
    if not raw_ids:
        # Fail-open: the request did not carry a patient_ids scope. Log so
        # we can spot any caller that is stripping it before the dispatcher
        # sees it. Telemetry-only — no metric here, the structured warn is
        # enough for the rollout window.
        logger.warning(
            "tool_scope_check_fail_open_empty_census",
            extra={
                "tool_name": tool_name,
                "requested_patient_id": requested,
                "session_id": session_context.get("session_id"),
            },
        )
        return True, None

    census_ids = {str(p) for p in raw_ids}
    if str(requested) in census_ids:
        return True, None

    return (
        False,
        f"scope_violation: patient_id {requested} is not on the current provider's census",
    )


__all__ = ["check_patient_scope", "PATIENT_KEYED_TOOLS"]
