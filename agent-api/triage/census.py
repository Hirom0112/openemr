"""Census context builder for UC-1.

Fetches FHIR bundles for all census patients in parallel, derives
TriageCriteria, runs the rules engine, and returns a ranked census list.

Result is cached in Redis so repeated calls within the same session
are served from memory — not from FHIR.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from auth.fhir_client import fhir_client
from triage.criteria import TriageCriteria, extract
from triage.rules_engine import TriageResult, rank

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 5 minutes — stale after one re-rounding cycle


@dataclass
class CensusEntry:
    patient_id: str
    name: str
    mrn: str
    openemr_pid: str
    triage_level: int
    triage_label: str
    triage_description: str
    matched_criteria: dict[str, Any]
    vitals_summary: dict[str, float]
    admit_date: str | None = None
    days_since_admit: int | None = None


def _patient_name(patient_resource: dict) -> str:
    names = patient_resource.get("name", [])
    if not names:
        return "Unknown"
    n = names[0]
    given = " ".join(n.get("given", []))
    family = n.get("family", "")
    return f"{given} {family}".strip()


def _patient_mrn(patient_resource: dict) -> str:
    for ident in patient_resource.get("identifier", []):
        if ident.get("type", {}).get("coding", [{}])[0].get("code") == "MR":
            return ident.get("value", "")
    return patient_resource.get("id", "")


def _patient_pid(patient_resource: dict) -> str:
    """Extract the numeric OpenEMR PID from FHIR Patient identifiers.

    OpenEMR stores the integer PID as an identifier with system ending in
    'pid' or type code 'MR' whose value is all-digits.  Falls back to the
    FHIR resource id (UUID) so the field is always non-empty.
    """
    for ident in patient_resource.get("identifier", []):
        system: str = ident.get("system", "")
        value: str = ident.get("value", "")
        # OpenEMR emits system="http://.../pid" for the integer PID identifier
        if "pid" in system and value.isdigit():
            return value
        # Some OpenEMR versions use type code MR with a numeric value
        code = ident.get("type", {}).get("coding", [{}])[0].get("code", "")
        if code == "MR" and value.isdigit():
            return value
    return patient_resource.get("id", "")


async def _build_entry(patient_id: str) -> CensusEntry | None:
    try:
        patient = await fhir_client.get_patient(patient_id)
        bundle = await fhir_client.get_bundle_for_patient(patient_id)
    except Exception as exc:
        logger.warning("Census fetch failed for patient", extra={"patient_id": patient_id, "error": str(exc)})
        return None

    criteria: TriageCriteria = extract(bundle)
    result: TriageResult = rank(criteria)

    # Extract admit date from the first in-progress Encounter
    admit_date: str | None = None
    days_since_admit: int | None = None
    for enc_entry in bundle.get("resources", {}).get("Encounter", []):
        enc = enc_entry.get("resource", enc_entry)
        start = enc.get("period", {}).get("start")
        if start:
            admit_date = start
            try:
                admit_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - admit_dt
                days_since_admit = max(0, delta.days)
            except (ValueError, AttributeError):
                pass
            break

    return CensusEntry(
        patient_id=patient_id,
        name=_patient_name(patient),
        mrn=_patient_mrn(patient),
        openemr_pid=_patient_pid(patient),
        triage_level=result.level,
        triage_label=result.label,
        triage_description=result.description,
        matched_criteria=result.matched_criteria,
        vitals_summary=criteria.latest_vitals,
        admit_date=admit_date,
        days_since_admit=days_since_admit,
    )


async def build_census(
    patient_ids: list[str],
    redis_client: aioredis.Redis | None = None,
    cache_key: str | None = None,
    provider_id: str | None = None,
) -> list[CensusEntry]:
    """Return a census ranked by triage level (1 = most urgent).

    If patient_ids is empty, all patients in the system are fetched from FHIR
    (auto-discovery for morning census when no panel is pre-loaded).

    If redis_client and cache_key are provided, results are cached for
    _CACHE_TTL seconds and served from cache on subsequent calls.
    """
    if redis_client and cache_key:
        cached = await redis_client.get(cache_key)
        if cached:
            logger.debug("Census served from cache", extra={"cache_key": cache_key})
            raw = json.loads(cached)
            return [CensusEntry(**row) for row in raw]

    if not patient_ids:
        logger.info("No patient IDs provided — auto-discovering census from FHIR")
        patient_ids = await fhir_client.get_all_patient_ids(provider_id=provider_id)
        logger.info("Auto-discovered %d patients", len(patient_ids))

    tasks = [_build_entry(pid) for pid in patient_ids]
    results = await asyncio.gather(*tasks)

    entries: list[CensusEntry] = [e for e in results if e is not None]
    entries.sort(key=lambda e: (e.triage_level, e.name))

    if redis_client and cache_key:
        await redis_client.setex(cache_key, _CACHE_TTL, json.dumps([asdict(e) for e in entries]))

    return entries
