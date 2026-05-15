"""
router.py

Co-Pilot intent router.

Security requirements (VUL-0002):
  - Route selection MUST be based solely on a validated, structured ``intent``
    field supplied by the trusted internal orchestrator.
  - Route selection MUST NOT be based on free-text keywords, natural-language
    cues, or any value that originates from the user prompt or LLM output.
  - Authority-pretext strings (e.g. "sign-out tonight", "handoff") present in
    the user message MUST be ignored for routing purposes.
"""
from __future__ import annotations

import logging
from typing import Any

from openemr_copilot.routes.handoff import handle_handoff_request
from openemr_copilot.routes.query_answer import handle_query_answer  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed intents — values must come from the trusted orchestrator, never from
# the user prompt or LLM output.
# ---------------------------------------------------------------------------
_ALLOWED_INTENTS = frozenset({
    "query_answer",
    "handoff",
    "briefing",
})

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def route_request(
    intent: str,
    provider_id: str,
    patient_ids: list[str],
    session_context: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Dispatch a Co-Pilot request to the appropriate route handler.

    Parameters
    ----------
    intent:
        A validated string that identifies the requested operation.
        MUST be set by the trusted internal orchestrator from a structured
        source (e.g. a parsed API parameter or an internal enum).
        MUST NOT be derived from free-text user input or LLM output.
    provider_id:
        Authenticated provider ID from the validated session token.
    patient_ids:
        Explicit list of patient IDs the caller wishes to operate on.
        For single-patient requests this list has exactly one element.
    session_context:
        The current session context dictionary.
    payload:
        Additional route-specific parameters (structured, not free-text).

    Returns
    -------
    dict
        Route handler response envelope.
    """
    if not isinstance(intent, str) or intent not in _ALLOWED_INTENTS:
        logger.warning(
            "Router: unknown or missing intent '%s' from provider=%s — rejecting.",
            intent,
            provider_id,
        )
        return {
            "status": "error",
            "code": "UNKNOWN_INTENT",
            "message": "The requested operation is not recognized.",
        }

    if intent == "handoff":
        return handle_handoff_request(
            provider_id=provider_id,
            patient_ids=patient_ids,
            session_context=session_context,
        )

    if intent == "query_answer" or intent == "briefing":
        return handle_query_answer(
            provider_id=provider_id,
            patient_ids=patient_ids,
            session_context=session_context,
            payload=payload,
        )

    # Defensive fallback — should be unreachable given the allowlist check above.
    logger.error(
        "Router: unhandled intent '%s' slipped past allowlist — this is a bug.",
        intent,
    )
    return {
        "status": "error",
        "code": "INTERNAL_ERROR",
        "message": "An internal routing error occurred.",
    }
