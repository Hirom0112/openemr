"""Extended FHIR search for UC-3 — wider time window than census prefetch.

The census builder fetches recent data (50 vitals, 100 labs).
UC-3 queries may ask about historical trends, prior admissions, or
medication history going back months.  This module handles those
extended searches with configurable windows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from auth.fhir_client import fhir_client
from query.router import QueryRoute

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_DAYS = 90
_EXTENDED_WINDOW_DAYS = 365


def _date_from(days_back: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    return cutoff.strftime("%Y-%m-%d")


async def search_for_patient(
    patient_id: str,
    route: QueryRoute,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """Execute a FHIR search and return a flat list of resource dicts."""
    window_days = _EXTENDED_WINDOW_DAYS if extended else _DEFAULT_WINDOW_DAYS

    params: dict[str, str] = {
        "patient": patient_id,
        "_count": "200" if extended else "50",
        **route.params,
    }

    # Apply date filter for time-windowed resources
    if route.resource in ("Observation", "MedicationRequest", "Procedure", "DiagnosticReport"):
        date_field = "date" if route.resource == "Observation" else "authoredon" if route.resource == "MedicationRequest" else "performed"
        params[f"{date_field}"] = f"ge{_date_from(window_days)}"

    try:
        bundle = await fhir_client.search(route.resource, params)
        entries = bundle.get("entry", [])
        resources = [e.get("resource", e) for e in entries]
        logger.debug(
            "FHIR search complete",
            extra={"resource": route.resource, "count": len(resources), "extended": extended},
        )
        return resources
    except Exception as exc:
        logger.warning(
            "FHIR extended search failed",
            extra={"resource": route.resource, "patient_id": patient_id, "error": str(exc)},
        )
        return []
