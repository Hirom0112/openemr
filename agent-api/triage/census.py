"""Census context builder for UC-1.

Fetches FHIR bundles for all census patients in parallel, derives
TriageCriteria, runs the rules engine, and returns a ranked census list.

Result is cached in Redis so repeated calls within the same session
are served from memory — not from FHIR.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from agent.metrics import (
    agent_census_dropped_patients_total,
    agent_data_cache_hits_total,
    agent_data_cache_misses_total,
)
from auth.fhir_client import fhir_client
from config import settings
from triage.criteria import TriageCriteria, extract
from triage.rules_engine import TriageResult, rank

logger = logging.getLogger(__name__)


def census_cache_key(provider_id: str | None, patient_ids: list[str]) -> str:
    """Build a deterministic Redis key for a census result.

    Shape: ``copilot:census:{provider}:{sha8(sorted(patient_ids))}``.

    The provider segment defaults to ``"unknown"`` when no provider was
    supplied so a missing provider does not silently collide with a real
    one. The patient hash uses the first 8 hex chars of sha1 over the
    comma-joined sorted ID list; an empty list collapses to ``"all"``
    (the auto-discovery sentinel).
    """
    provider_segment = provider_id or "unknown"
    if patient_ids:
        joined = ",".join(sorted(patient_ids))
        digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:8]
    else:
        digest = "all"
    return f"copilot:census:{provider_segment}:{digest}"


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
    # Real labs from the bundle so the explainer LLM cannot hallucinate
    # values to justify the rank. Each entry: {name, loinc, value, unit, interp}.
    abnormal_labs: list[dict[str, Any]] = field(default_factory=list)
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
    'pid' or type code 'MR' whose value is all-digits.

    Returns an empty string when no integer PID can be found.  Crucially,
    we do NOT fall back to the FHIR resource id (a UUID) — the consumer
    of openemr_pid (the chart deep-link in agent-ui) feeds it to
    demographics_full.php?set_pid=, which expects the integer
    patient_data.pid column.  Passing a UUID here loads an empty page.
    The frontend has its own fallback (resolvePatientPid for pt-NNN
    synthetic IDs) and a digits-only guard, so an empty string is the
    safe signal that "we don't know the integer pid".
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
    return ""


async def _build_entry(patient_id: str) -> CensusEntry | None:
    try:
        patient = await fhir_client.get_patient(patient_id)
        bundle = await fhir_client.get_bundle_for_patient(patient_id)
    except Exception as exc:
        # Caller handles None → drop + log + metric. Keep the message
        # consistent so the upstream WARNING already includes the reason.
        logger.warning("Census fetch failed for patient", extra={"patient_id": patient_id, "error": str(exc)})
        return None

    criteria: TriageCriteria = extract(bundle)
    result: TriageResult = rank(criteria)

    # Pull abnormal/critical labs (interpretation H/L/HH/LL/A/AA) so the
    # explainer LLM has real values to cite instead of inventing them.
    abnormal_labs: list[dict[str, Any]] = []
    flagged_codes = {"H", "L", "HH", "LL", "A", "AA"}
    for entry in bundle.get("resources", {}).get("Observation", []):
        obs = entry.get("resource", entry)
        interp_codes = []
        for interp in obs.get("interpretation", []):
            for c in interp.get("coding", []):
                if c.get("code"):
                    interp_codes.append(c["code"])
        if not (set(interp_codes) & flagged_codes):
            continue
        loinc = next(
            (c.get("code") for c in obs.get("code", {}).get("coding", []) if c.get("system") == "http://loinc.org"),
            None,
        )
        display = obs.get("code", {}).get("text") or (
            obs.get("code", {}).get("coding", [{}])[0].get("display", "")
        )
        qty = obs.get("valueQuantity", {})
        value = qty.get("value")
        unit = qty.get("unit", "")
        if value is None:
            continue
        abnormal_labs.append({
            "name": display,
            "loinc": loinc,
            "value": value,
            "unit": unit,
            "interp": interp_codes[0] if interp_codes else "",
        })

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
        abnormal_labs=abnormal_labs,
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
    ``settings.census_cache_ttl_seconds`` and served from cache on subsequent
    calls.
    """
    if redis_client and cache_key:
        try:
            cached = await redis_client.get(cache_key)
        except Exception as exc:
            logger.warning("Census cache read failed", extra={"cache_key": cache_key, "error": str(exc)})
            cached = None
        if cached:
            logger.debug("Census served from cache", extra={"cache_key": cache_key})
            agent_data_cache_hits_total.labels(cache="census").inc()
            raw = json.loads(cached)
            return [CensusEntry(**row) for row in raw]
        agent_data_cache_misses_total.labels(cache="census").inc()

    if not patient_ids:
        logger.info("No patient IDs provided — auto-discovering census from FHIR")
        patient_ids = await fhir_client.get_all_patient_ids(provider_id=provider_id)
        logger.info("Auto-discovered %d patients", len(patient_ids))

    # Sort before fan-out so the resulting list ordering is deterministic
    # regardless of the order FHIR returned the IDs in. The downstream sort
    # by (triage_level, name) is the user-visible ordering, but having a
    # stable input order makes _build_entry concurrency reproducible too.
    patient_ids = sorted(patient_ids)

    tasks = [_build_entry(pid) for pid in patient_ids]
    results = await asyncio.gather(*tasks)

    entries: list[CensusEntry] = []
    for pid, entry in zip(patient_ids, results):
        if entry is None:
            agent_census_dropped_patients_total.inc()
            logger.warning(
                "Patient dropped from census after _build_entry failed",
                extra={"patient_id": pid},
            )
            continue
        entries.append(entry)
    entries.sort(key=lambda e: (e.triage_level, e.name))

    if redis_client and cache_key:
        try:
            await redis_client.setex(
                cache_key,
                settings.census_cache_ttl_seconds,
                json.dumps([asdict(e) for e in entries]),
            )
        except Exception as exc:
            logger.warning("Census cache write failed", extra={"cache_key": cache_key, "error": str(exc)})

    return entries
