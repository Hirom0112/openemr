"""
auth/panel_check.py

Provides panel-membership authorization helpers for the Co-Pilot.

All authorization decisions are made against the authoritative EHR panel
store — never against values supplied by the user or the LLM prompt.
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


class PanelAuthorizationError(PermissionError):
    """Raised when the requesting provider is not authorized for one or more patients."""

    def __init__(self, provider_id: str, unauthorized_patient_ids: list[str]) -> None:
        # Store metadata WITHOUT PHI in the message so the string is safe to log.
        self.provider_id = provider_id
        self.unauthorized_patient_ids = unauthorized_patient_ids
        super().__init__(
            f"Provider '{provider_id}' is not the documented attending or covering "
            f"provider for {len(unauthorized_patient_ids)} patient(s). "
            "Access denied (HIPAA minimum-necessary, 164.502(b))."
        )


def get_panel_patient_ids(provider_id: str) -> set[str]:
    """
    Return the set of patient IDs for which *provider_id* is the documented
    attending or covering provider.

    This function must query the authoritative EHR panel store.
    Replace the placeholder import below with the actual panel-service client.
    """
    try:
        # Import is deferred so that the auth module can be unit-tested with a mock.
        from openemr_copilot.services.panel_service import fetch_panel_for_provider  # type: ignore
        return set(fetch_panel_for_provider(provider_id))
    except ImportError:
        # Should only happen in isolated unit-test environments where the service
        # layer is mocked at the call site.
        raise


def is_patient_in_panel(provider_id: str, patient_id: str) -> bool:
    """
    Return True if *patient_id* is in *provider_id*'s panel.
    """
    return patient_id in get_panel_patient_ids(provider_id)


def assert_provider_covers_all(
    provider_id: str,
    patient_ids: Iterable[str],
) -> None:
    """
    Raise :class:`PanelAuthorizationError` if the requesting provider is **not**
    the documented attending or covering provider for every patient in
    *patient_ids*.

    This check MUST be called before any patient data is fetched or returned
    by any bulk route.

    :param provider_id: The authenticated provider's ID from the session.
    :param patient_ids: The patient IDs that will be included in the response.
    :raises PanelAuthorizationError: If any patient is outside the provider's panel.
    """
    requested = set(patient_ids)
    if not requested:
        # Empty request — nothing to authorize.
        return

    panel = get_panel_patient_ids(provider_id)
    unauthorized = sorted(requested - panel)

    if unauthorized:
        # Log the violation WITHOUT including patient identifiers or PHI.
        logger.warning(
            "Panel authorization failure: provider=%s unauthorized_count=%d",
            provider_id,
            len(unauthorized),
        )
        raise PanelAuthorizationError(
            provider_id=provider_id,
            unauthorized_patient_ids=unauthorized,
        )
