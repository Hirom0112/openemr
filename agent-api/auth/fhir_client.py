"""FHIR R4 client using SMART on FHIR client credentials flow.

Tokens are cached in memory and refreshed automatically before expiry.
All FHIR reads go through get() — callers never touch raw HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

_token_cache: dict[str, Any] = {}
_token_lock = asyncio.Lock()


async def _fetch_token() -> str:
    """Exchange client credentials for an access token."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            settings.resolved_fhir_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.fhir_client_id,
                "client_secret": settings.fhir_client_secret,
                "scope": settings.fhir_scopes,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()

    expires_in = int(payload.get("expires_in", 300))
    return payload["access_token"], time.monotonic() + expires_in - 30  # 30-second buffer


async def get_access_token() -> str:
    """Return a valid access token, refreshing if within 30 s of expiry."""
    async with _token_lock:
        expiry = _token_cache.get("expiry", 0.0)
        if time.monotonic() >= expiry:
            logger.info("Fetching new FHIR access token")
            token, new_expiry = await _fetch_token()
            _token_cache["token"] = token
            _token_cache["expiry"] = new_expiry
        return _token_cache["token"]


class FHIRClient:
    """Thin async FHIR R4 client.  All methods return parsed JSON dicts."""

    def __init__(self) -> None:
        self._base = settings.openemr_base_url.rstrip("/") + "/apis/default/fhir"

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        token = await get_access_token()
        url = f"{self._base}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/fhir+json",
                },
            )
            response.raise_for_status()
            return response.json()

    async def get_patient(self, patient_id: str) -> dict[str, Any]:
        return await self.get(f"Patient/{patient_id}")

    async def search(self, resource: str, params: dict[str, str]) -> dict[str, Any]:
        return await self.get(resource, params=params)

    async def get_bundle_for_patient(self, patient_id: str) -> dict[str, Any]:
        """Fetch a minimal census bundle: vitals, labs, meds, conditions, allergies."""
        resources = [
            ("Observation", {"patient": patient_id, "category": "vital-signs", "_count": "50"}),
            ("Observation", {"patient": patient_id, "category": "laboratory", "_count": "100"}),
            ("MedicationRequest", {"patient": patient_id, "status": "active"}),
            ("Condition", {"patient": patient_id, "clinical-status": "active"}),
            ("AllergyIntolerance", {"patient": patient_id}),
            ("Flag", {"patient": patient_id}),
        ]
        results: dict[str, Any] = {"patient_id": patient_id, "resources": {}}
        for resource, params in resources:
            try:
                data = await self.search(resource, params)
                results["resources"][resource] = data.get("entry", [])
            except httpx.HTTPError as exc:
                logger.warning("FHIR fetch failed", extra={"resource": resource, "patient_id": patient_id, "error": str(exc)})
                results["resources"][resource] = []
        return results


fhir_client = FHIRClient()
