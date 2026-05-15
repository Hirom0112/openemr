"""
routes/handoff.py

Handoff route — returns a structured handoff summary for a validated set of
patients.

Security requirements (VUL-0002):
  1. This route MUST only be invoked by the trusted internal router with an
     explicit ``intent=handoff`` parameter.  It MUST NOT be reachable via
     free-text / LLM keyword matching.
  2. ``assert_provider_covers_all`` MUST be called before any patient data is
     fetched.
  3. On authorization failure a structured refusal envelope is returned that
     contains NO patient identifiers and NO PHI.
"""
from __future__ import annotations

import logging
from typing import Any

from openemr_copilot.auth.panel_check import (
    PanelAuthorizationError,
    assert_provider_covers_all,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def handle_handoff_request(
    provider_id: str,
    patient_ids: list[str],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    """
    Build and return a handoff summary for *patient_ids*.

    Parameters
    ----------
    provider_id:
        The **authenticated** provider ID extracted from the validated session
        token — never from the user prompt.
    patient_ids:
        The explicit list of patient IDs to include.  Must be supplied by the
        internal caller, not derived from free-text LLM output.
    session_context:
        The current session context dictionary (used to retrieve clinical data
        after authorization succeeds).

    Returns
    -------
    dict
        On success: ``{"status": "ok", "handoff": <summary>}``
        On auth failure: ``{"status": "error", "code": "PANEL_AUTHORIZATION_FAILURE",
                           "message": <safe string>}``
    """
    # ------------------------------------------------------------------
    # STEP 1 — Authorization gate (MUST precede any data access)
    # ------------------------------------------------------------------
    try:
        assert_provider_covers_all(
            provider_id=provider_id,
            patient_ids=patient_ids,
        )
    except PanelAuthorizationError as exc:
        # Return a safe refusal envelope.  Do NOT include patient IDs or
        # any clinical data in this response.
        logger.warning(
            "Handoff route: authorization denied for provider=%s patient_count=%d",
            provider_id,
            len(patient_ids),
        )
        return _refusal_envelope(
            "You are not the documented attending or covering provider for one or "
            "more of the requested patients.  The handoff summary cannot be "
            "generated.  Please contact your charge nurse or attending to obtain "
            "appropriate coverage assignment before accessing these records."
        )

    # ------------------------------------------------------------------
    # STEP 2 — Data retrieval (only reached after authorization succeeds)
    # ------------------------------------------------------------------
    try:
        summary = _build_handoff_summary(
            provider_id=provider_id,
            patient_ids=patient_ids,
            session_context=session_context,
        )
    except Exception:  # pragma: no cover
        logger.exception(
            "Handoff route: unexpected error building summary for provider=%s",
            provider_id,
        )
        return _refusal_envelope(
            "An internal error occurred while generating the handoff summary.  "
            "Please try again or contact support."
        )

    return {"status": "ok", "handoff": summary}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _refusal_envelope(message: str) -> dict[str, Any]:
    """Return a structured refusal that contains NO PHI."""
    return {
        "status": "error",
        "code": "PANEL_AUTHORIZATION_FAILURE",
        "message": message,
    }


def _build_handoff_summary(
    provider_id: str,
    patient_ids: list[str],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    """
    Retrieve and structure handoff data for the authorized patient set.

    Replace the stub below with the real data-service calls.
    """
    # Import deferred to allow unit-testing with mocks.
    from openemr_copilot.services.clinical_data_service import (  # type: ignore
        fetch_handoff_data_for_patients,
    )

    raw = fetch_handoff_data_for_patients(
        provider_id=provider_id,
        patient_ids=patient_ids,
        context=session_context,
    )

    return {
        "patients": raw,
        "patient_count": len(patient_ids),
    }
