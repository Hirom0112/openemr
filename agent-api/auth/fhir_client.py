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
        self._id_cache: dict[str, str] = {}

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
        """Translate synthetic or numeric PIDs to FHIR UUIDs via identifier search.

        pt-001 → PID 1, "1" (plain numeric) → PID 1 → GET /Patient?identifier=1 → UUID.
        Already-UUID / FHIR-ID strings pass through unchanged.
        """
        cached = self._id_cache.get(patient_id)
        if cached is not None:
            return cached

        m = re.match(r'^pt-(\d+)$', patient_id)
        if m:
            pid_num = str(int(m.group(1)))
        elif patient_id.isdigit():
            pid_num = patient_id
        else:
            self._id_cache[patient_id] = patient_id
            return patient_id  # already a FHIR UUID

        result = await self.search("Patient", {"identifier": pid_num})
        entries = result.get("entry", [])
        if not entries:
            raise ValueError(f"No FHIR Patient found for identifier {pid_num} (from {patient_id})")
        fhir_id: str = entries[0]["resource"]["id"]
        logger.info(
            "Resolved patient ID",
            extra={"pt_id": patient_id, "pid": pid_num, "fhir_id": fhir_id},
        )
        self._id_cache[patient_id] = fhir_id
        return fhir_id

    async def get_all_patient_ids(self, count: int = 200, provider_id: str | None = None) -> list[str]:
        """Return FHIR UUIDs for patients (census auto-discovery).

        If ``provider_id`` is supplied, only patients with an in-progress
        Encounter participated in by that Practitioner are returned.
        """
        if provider_id:
            result = await self.search("Encounter", {
                "participant.individual": f"Practitioner/{provider_id}",
                "status": "in-progress",
                "_count": str(count),
            })
            seen: set[str] = set()
            patient_ids: list[str] = []
            for e in result.get("entry", []):
                ref = e.get("resource", {}).get("subject", {}).get("reference", "")
                pid = ref.split("/")[-1] if "/" in ref else ref
                if pid and pid not in seen:
                    seen.add(pid)
                    patient_ids.append(pid)
            return patient_ids
        result = await self.search("Patient", {"_count": str(count)})
        return [e["resource"]["id"] for e in result.get("entry", [])]

    async def get_patient(self, patient_id: str) -> dict[str, Any]:
        fhir_id = await self._resolve_patient_id(patient_id)
        return await self.get(f"Patient/{fhir_id}")

    async def search(self, resource: str, params: dict[str, str]) -> dict[str, Any]:
        return await self.get(resource, params=params)

    async def _safe_search(self, resource: str, params: dict[str, str], patient_id: str) -> dict[str, Any]:
        """Search with error handling; returns empty bundle on failure."""
        try:
            return await self.search(resource, params)
        except httpx.HTTPError as exc:
            logger.warning(
                "FHIR fetch failed",
                extra={"resource": resource, "params": params, "patient_id": patient_id, "error": str(exc)},
            )
            return {}

    async def get_bundle_for_patient(self, patient_id: str) -> dict[str, Any]:
        """Fetch a minimal census bundle: vitals, labs, meds, conditions, allergies.

        All 8 FHIR searches run concurrently via asyncio.gather() to minimise
        wall-clock latency. Observation results are deduplicated by resource id
        before being stored so the lab search cannot overwrite vitals entries.
        SBP/DBP (8480-6/8462-4) are stored as hasMember observations in OpenEMR
        and are NOT returned by category=vital-signs, so they are fetched explicitly.
        """
        fhir_id = await self._resolve_patient_id(patient_id)

        observation_params = [
            {"patient": fhir_id, "category": "vital-signs", "_count": "50"},
            {"patient": fhir_id, "code": "8480-6", "_count": "10"},   # Systolic BP
            {"patient": fhir_id, "code": "8462-4", "_count": "10"},   # Diastolic BP
            {"patient": fhir_id, "category": "laboratory", "_count": "100"},
            {"patient": fhir_id, "code": "81638-3", "_count": "5"},   # Code status — not in vital-signs or lab category
        ]
        other_resources = [
            ("MedicationRequest", {"patient": fhir_id, "status": "active"}),
            ("Condition", {"patient": fhir_id, "clinical-status": "active"}),
            ("AllergyIntolerance", {"patient": fhir_id}),
            ("Flag", {"patient": fhir_id}),
            ("Encounter", {"patient": fhir_id, "status": "in-progress", "_count": "3"}),
        ]

        # Run all searches concurrently.
        obs_coros = [self._safe_search("Observation", p, patient_id) for p in observation_params]
        other_coros = [self._safe_search(r, p, patient_id) for r, p in other_resources]
        all_results = await asyncio.gather(*obs_coros, *other_coros)

        obs_results = all_results[:len(obs_coros)]
        other_results = all_results[len(obs_coros):]

        # Merge Observation entries, deduplicating by resource id.
        obs_entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for data in obs_results:
            for entry in data.get("entry", []):
                rid = entry.get("resource", {}).get("id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    obs_entries.append(entry)

        results: dict[str, Any] = {"patient_id": patient_id, "resources": {}}
        results["resources"]["Observation"] = obs_entries
        for (resource, _), data in zip(other_resources, other_results):
            results["resources"][resource] = data.get("entry", [])

        return results


fhir_client = FHIRClient()
