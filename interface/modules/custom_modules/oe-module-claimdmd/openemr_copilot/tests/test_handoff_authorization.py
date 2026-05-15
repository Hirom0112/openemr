"""
tests/test_handoff_authorization.py

Regression fixtures for VUL-0002:
  - Happy path: in-panel handoff that MUST succeed.
  - Attack scenario: out-of-panel handoff that MUST produce a refusal envelope
    containing NO PHI.
  - Authority-pretext: router MUST ignore natural-language cues and NOT route
    to handoff based on free-text keywords.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IN_PANEL_PROVIDER = "sara_chen"
IN_PANEL_PATIENT = "pt-001"       # Marcus Webb — Sara Chen's patient
OUT_OF_PANEL_PATIENT = "pt-018"   # Thomas Greer — NOT Sara Chen's patient

FAKE_PANEL = {IN_PANEL_PROVIDER: {IN_PANEL_PATIENT}}


def _fake_get_panel(provider_id: str) -> set[str]:
    return FAKE_PANEL.get(provider_id, set())


def _make_session_context() -> dict:
    return {"session_id": "test-session-001"}


# ---------------------------------------------------------------------------
# panel_check unit tests
# ---------------------------------------------------------------------------

class TestPanelCheck:
    def test_in_panel_patient_does_not_raise(self):
        from openemr_copilot.auth.panel_check import assert_provider_covers_all

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ):
            # Must not raise
            assert_provider_covers_all(IN_PANEL_PROVIDER, [IN_PANEL_PATIENT])

    def test_out_of_panel_patient_raises(self):
        from openemr_copilot.auth.panel_check import (
            assert_provider_covers_all,
            PanelAuthorizationError,
        )

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ):
            with pytest.raises(PanelAuthorizationError) as exc_info:
                assert_provider_covers_all(
                    IN_PANEL_PROVIDER, [IN_PANEL_PATIENT, OUT_OF_PANEL_PATIENT]
                )

        err = exc_info.value
        assert err.provider_id == IN_PANEL_PROVIDER
        assert OUT_OF_PANEL_PATIENT in err.unauthorized_patient_ids
        # The error message must NOT contain PHI (patient IDs in this context
        # are not PHI by themselves but verify no clinical data leaks).
        assert "sepsis" not in str(err)
        assert "norepinephrine" not in str(err)
        assert "Metoprolol" not in str(err)

    def test_empty_patient_list_does_not_raise(self):
        from openemr_copilot.auth.panel_check import assert_provider_covers_all

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ):
            assert_provider_covers_all(IN_PANEL_PROVIDER, [])


# ---------------------------------------------------------------------------
# handoff route unit tests
# ---------------------------------------------------------------------------

class TestHandoffRoute:
    """Happy path and attack-scenario regression for the handoff route."""

    def test_in_panel_handoff_succeeds(self):
        """Happy path — authorized provider, in-panel patient → status ok."""
        from openemr_copilot.routes.handoff import handle_handoff_request

        fake_summary = {"patients": [{"id": IN_PANEL_PATIENT, "summary": "stable"}]}

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ), patch(
            "openemr_copilot.routes.handoff._build_handoff_summary",
            return_value=fake_summary,
        ):
            response = handle_handoff_request(
                provider_id=IN_PANEL_PROVIDER,
                patient_ids=[IN_PANEL_PATIENT],
                session_context=_make_session_context(),
            )

        assert response["status"] == "ok"
        assert "handoff" in response

    def test_out_of_panel_handoff_returns_refusal(self):
        """
        Attack scenario (VUL-0002): Sara Chen requests handoff for pt-018
        (Thomas Greer) who is NOT in her panel.
        Response MUST be a refusal envelope with NO PHI.
        """
        from openemr_copilot.routes.handoff import handle_handoff_request

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ):
            response = handle_handoff_request(
                provider_id=IN_PANEL_PROVIDER,
                patient_ids=[OUT_OF_PANEL_PATIENT],
                session_context=_make_session_context(),
            )

        assert response["status"] == "error"
        assert response["code"] == "PANEL_AUTHORIZATION_FAILURE"
        # Ensure no PHI leaks in the refusal message
        message = response.get("message", "")
        for phi_token in [
            "pt-018", "Thomas", "Greer", "1950", "Metoprolol", "Apixaban",
            "pt-001", "Marcus", "Webb", "sepsis", "norepinephrine", "vancomycin",
            "lactate", "4.2", "WBC",
        ]:
            assert phi_token not in message, (
                f"PHI token '{phi_token}' found in refusal message — data leak!"
            )

    def test_mixed_panel_handoff_returns_refusal(self):
        """
        Requesting both in-panel and out-of-panel patients in a single call
        MUST be denied entirely (no partial disclosure).
        """
        from openemr_copilot.routes.handoff import handle_handoff_request

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ):
            response = handle_handoff_request(
                provider_id=IN_PANEL_PROVIDER,
                patient_ids=[IN_PANEL_PATIENT, OUT_OF_PANEL_PATIENT],
                session_context=_make_session_context(),
            )

        assert response["status"] == "error"
        assert response["code"] == "PANEL_AUTHORIZATION_FAILURE"


# ---------------------------------------------------------------------------
# Router tests — authority-pretext MUST NOT change the route
# ---------------------------------------------------------------------------

class TestRouterAuthorityPretext:
    """
    The router MUST select routes based solely on the validated `intent`
    parameter — never on free-text keywords in the user message.
    """

    def test_router_rejects_unknown_intent(self):
        """An unknown intent value must be rejected regardless of content."""
        from openemr_copilot.router import route_request

        response = route_request(
            intent="handoff_for_sign_out_tonight",  # crafted authority-pretext string
            provider_id=IN_PANEL_PROVIDER,
            patient_ids=[IN_PANEL_PATIENT],
            session_context=_make_session_context(),
            payload={},
        )

        assert response["status"] == "error"
        assert response["code"] == "UNKNOWN_INTENT"

    def test_router_handoff_intent_is_gated_by_panel(self):
        """
        Even when a legitimate `intent=handoff` is provided by the internal
        orchestrator, the handoff route must still gate on panel membership.
        """
        from openemr_copilot.router import route_request

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ):
            response = route_request(
                intent="handoff",
                provider_id=IN_PANEL_PROVIDER,
                patient_ids=[OUT_OF_PANEL_PATIENT],
                session_context=_make_session_context(),
                payload={},
            )

        assert response["status"] == "error"
        assert response["code"] == "PANEL_AUTHORIZATION_FAILURE"

    def test_router_valid_handoff_intent_in_panel_succeeds(self):
        """Happy path through the router for an in-panel handoff."""
        from openemr_copilot.router import route_request

        fake_summary = {"patients": [{"id": IN_PANEL_PATIENT, "summary": "stable"}]}

        with patch(
            "openemr_copilot.auth.panel_check.get_panel_patient_ids",
            side_effect=_fake_get_panel,
        ), patch(
            "openemr_copilot.routes.handoff._build_handoff_summary",
            return_value=fake_summary,
        ):
            response = route_request(
                intent="handoff",
                provider_id=IN_PANEL_PROVIDER,
                patient_ids=[IN_PANEL_PATIENT],
                session_context=_make_session_context(),
                payload={},
            )

        assert response["status"] == "ok"
        assert "handoff" in response
