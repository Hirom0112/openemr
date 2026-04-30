"""FHIR R4 client for OpenEMR.

OpenEMR on Railway does not support the SMART client_credentials backend
services flow (returns "assertion type is not supported").  The working flow
is a password grant with user_role=users.  Tokens are cached in memory and
refreshed automatically before expiry.  All FHIR reads go through get().

Patient ID mapping: the agent uses 'pt-NNN' synthetic identifiers. OpenEMR
stores patients with numeric PIDs (1, 2, 3…) as their FHIR identifier value.
_resolve_patient_id() translates 'pt-001' → FHIR UUID via GET /Patient?identifier=1.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

_token_cache: dict[str, Any] = {}
_token_lock = asyncio.Lock()


async def _fetch_token() -> tuple[str, float]:
    """Obtain an access token via password grant with FHIR scopes."""
    token_url = settings.resolved_fhir_token_url
    logger.info("FHIR token URL: %s", token_url)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "password",
                    "client_id": settings.fhir_client_id,
                    "client_secret": settings.fhir_client_secret,
                    "username": settings.fhir_username,
                    "password": settings.fhir_password,
                    "user_role": settings.fhir_user_role,
                    "scope": settings.fhir_scopes,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as conn_exc:
        logger.error("FHIR token HTTP request failed (connection error): %s", conn_exc)
        raise

    if response.status_code != 200:
        logger.error(
            "FHIR token request failed",
            extra={"status": response.status_code, "body": response.text[:300]},
        )
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as json_exc:
        logger.error(
            "FHIR token response is not valid JSON",
            extra={"status": response.status_code, "body": response.text[:300], "error": str(json_exc)},
        )
        raise RuntimeError(f"FHIR token response is not valid JSON: {response.text[:200]}") from json_exc

    if "access_token" not in payload:
        raise RuntimeError(f"No access_token in FHIR token response: {payload}")

    expires_in = int(payload.get("expires_in", 300))
    return payload["access_token"], time.monotonic() + expires_in - 30


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
            logger.info(
                "FHIR GET response",
                extra={"url": url, "status": response.status_code, "body_len": len(response.text), "body_preview": response.text[:200]},
            )
            if response.status_code != 200:
                logger.error(
                    "FHIR GET failed",
                    extra={"url": url, "status": response.status_code, "body": response.text[:500]},
                )
            response.raise_for_status()
            try:
                return response.json()
            except Exception as json_exc:
                logger.error(
                    "FHIR GET response not JSON",
                    extra={"url": url, "status": response.status_code, "body": response.text[:500], "error": str(json_exc)},
                )
                raise

    async def _resolve_patient_id(self, patient_id: str) -> str:
        """Translate 'pt-NNN' synthetic IDs to FHIR UUIDs via identifier search.

        pt-001 → OpenEMR PID 1 → GET /Patient?identifier=1 → UUID.
        Already-UUID IDs pass through unchanged.
        """
        m = re.match(r'^pt-(\d+)$', patient_id)
        if not m:
            return patient_id
        pid_num = str(int(m.group(1)))  # "pt-001" → "1"
        result = await self.search("Patient", {"identifier": pid_num})
        entries = result.get("entry", [])
        if not entries:
            raise ValueError(f"No FHIR Patient found for identifier {pid_num} (from {patient_id})")
        fhir_id: str = entries[0]["resource"]["id"]
        logger.info(
            "Resolved patient ID",
            extra={"pt_id": patient_id, "pid": pid_num, "fhir_id": fhir_id},
        )
        return fhir_id

    async def get_patient(self, patient_id: str) -> dict[str, Any]:
        fhir_id = await self._resolve_patient_id(patient_id)
        return await self.get(f"Patient/{fhir_id}")

    async def search(self, resource: str, params: dict[str, str]) -> dict[str, Any]:
        return await self.get(resource, params=params)

    async def get_bundle_for_patient(self, patient_id: str) -> dict[str, Any]:
        """Fetch a minimal census bundle: vitals, labs, meds, conditions, allergies."""
        fhir_id = await self._resolve_patient_id(patient_id)
        resources = [
            ("Observation", {"patient": fhir_id, "category": "vital-signs", "_count": "50"}),
            ("Observation", {"patient": fhir_id, "category": "laboratory", "_count": "100"}),
            ("MedicationRequest", {"patient": fhir_id, "status": "active"}),
            ("Condition", {"patient": fhir_id, "clinical-status": "active"}),
            ("AllergyIntolerance", {"patient": fhir_id}),
            ("Flag", {"patient": fhir_id}),
        ]
        results: dict[str, Any] = {"patient_id": patient_id, "resources": {}}
        for resource, params in resources:
            try:
                data = await self.search(resource, params)
                results["resources"][resource] = data.get("entry", [])
            except httpx.HTTPError as exc:
                logger.warning(
                    "FHIR fetch failed",
                    extra={"resource": resource, "patient_id": patient_id, "error": str(exc)},
                )
                results["resources"][resource] = []
        return results


fhir_client = FHIRClient()
