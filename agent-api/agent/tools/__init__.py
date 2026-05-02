"""Agent tool functions — the six callable implementations behind the dispatcher.

Each function signature:
    async def tool_name(input: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]

Return shape (standardized for all tools):
    {
        "result":   <structured payload>,
        "citations": list[{patient_id, resource_type, resource_id, effective_datetime, value_summary}],
        "metadata": {
            "tool": str,
            "patient_id": str | None,
            "duration_ms": int,
            "fhir_resources_accessed": list[str],
        },
    }

Verification runs inside each tool before returning.
session_context may be sparse ({}) in Phase 2; Phase 4 (dispatcher) populates it fully.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from agent.citation import Citation, CitationList, ClaimClass, citations_for_fhir_resource
from agent.metrics import (
    agent_data_cache_hits_total,
    agent_data_cache_misses_total,
)
from observability.tool_logging import log_tool_outcome
from auth.fhir_client import fhir_client
from briefing.context_builder import build as build_briefing_context
from briefing.generator import generate_briefing
from briefing.schema import BriefingResponse
from config import settings
from handoff.generator import generate_handoffs
from medication.safety import add_llm_summary, run_safety_checks
from triage.census import build_census, census_cache_key
from triage.criteria import extract as extract_criteria
from triage.explainer import explain_census
from triage.rules_engine import rank
from verification.domain_constraints import verify_conversation_answer, verify_safety_summary, verify_triage_entry
from verification.source_attribution import extract_citations, verify_briefing

logger = logging.getLogger(__name__)


# ── Citation helpers ──────────────────────────────────────────────────────────

def _citation_from_briefing(briefing: BriefingResponse, patient_id: str) -> list[dict[str, Any]]:
    """Convert verified briefing claims to structured Citation dicts."""
    result: list[dict[str, Any]] = []
    for section in briefing.sections:
        for claim in section.claims:
            # Map briefing section names to claim classes
            section_to_class: dict[str, ClaimClass] = {
                "labs": "lab_value",
                "vitals": "vital",
                "medications": "medication",
                "conditions": "condition",
                "allergies": "allergy",
                "code_status": "code_status",
                "isolation": "isolation",
            }
            claim_class: ClaimClass = section_to_class.get(section.section, "lab_value")
            citation = Citation(
                patient_id=patient_id,
                resource_type=claim.source_resource,
                resource_id=claim.source_code,
                effective_datetime=getattr(claim, "source_dt", None),
                value_summary=claim.source_value,
                claim_class=claim_class,
            )
            result.append(citation.to_dict())
    return result


def _empty_metadata(tool: str, patient_id: str | None, duration_ms: int, resources: list[str]) -> dict[str, Any]:
    return {
        "tool": tool,
        "patient_id": patient_id,
        "duration_ms": duration_ms,
        "fhir_resources_accessed": resources,
    }


# ── Redis cache helpers (shared by dispatcher tools and prefetch warmer) ─────


def _bundle_cache_key(patient_id: str) -> str:
    return f"copilot:bundle:{patient_id}"


def _briefing_cache_key(patient_id: str) -> str:
    return f"copilot:briefing:{patient_id}"


async def _get_cached_bundle(
    redis_client: aioredis.Redis | None,
    patient_id: str,
) -> dict[str, Any] | None:
    """Return the cached FHIR bundle for ``patient_id`` or None on miss/error.

    Increments hit/miss metrics. Treats Redis exceptions as miss (logged).
    """
    if redis_client is None:
        return None
    key = _bundle_cache_key(patient_id)
    try:
        raw = await redis_client.get(key)
    except Exception as exc:
        logger.warning("Bundle cache read failed", extra={"patient_id": patient_id, "error": str(exc)})
        agent_data_cache_misses_total.labels(cache="bundle").inc()
        return None
    if raw:
        agent_data_cache_hits_total.labels(cache="bundle").inc()
        try:
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Bundle cache decode failed", extra={"patient_id": patient_id, "error": str(exc)})
            return None
    agent_data_cache_misses_total.labels(cache="bundle").inc()
    return None


async def _set_cached_bundle(
    redis_client: aioredis.Redis | None,
    patient_id: str,
    bundle: dict[str, Any],
) -> None:
    if redis_client is None:
        return
    try:
        await redis_client.setex(
            _bundle_cache_key(patient_id),
            settings.bundle_cache_ttl_seconds,
            json.dumps(bundle),
        )
    except Exception as exc:
        logger.warning("Bundle cache write failed", extra={"patient_id": patient_id, "error": str(exc)})


async def _get_cached_briefing(
    redis_client: aioredis.Redis | None,
    patient_id: str,
) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    key = _briefing_cache_key(patient_id)
    try:
        raw = await redis_client.get(key)
    except Exception as exc:
        logger.warning("Briefing cache read failed patient_id=%s error=%s", patient_id, exc)
        agent_data_cache_misses_total.labels(cache="briefing").inc()
        return None
    if raw:
        agent_data_cache_hits_total.labels(cache="briefing").inc()
        try:
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Briefing cache decode failed patient_id=%s error=%s", patient_id, exc)
            return None
    agent_data_cache_misses_total.labels(cache="briefing").inc()
    return None


async def _set_cached_briefing(
    redis_client: aioredis.Redis | None,
    patient_id: str,
    payload: dict[str, Any],
) -> None:
    if redis_client is None:
        return
    try:
        await redis_client.setex(
            _briefing_cache_key(patient_id),
            settings.briefing_cache_ttl_seconds,
            json.dumps(payload),
        )
    except Exception as exc:
        logger.warning("Briefing cache write failed patient_id=%s error=%s", patient_id, exc)


async def _redis_exists(redis_client: aioredis.Redis | None, key: str) -> bool:
    """Return True iff the key exists. Treats Redis errors as False (caller warms)."""
    if redis_client is None:
        return False
    try:
        return bool(await redis_client.exists(key))
    except Exception as exc:
        logger.warning("Redis EXISTS failed", extra={"key": key, "error": str(exc)})
        return False


async def warm_bundle_for_patient(
    redis_client: aioredis.Redis | None,
    patient_id: str,
) -> None:
    """Fire-and-forget bundle warmer. Skips if the key already exists."""
    if redis_client is None:
        return
    if await _redis_exists(redis_client, _bundle_cache_key(patient_id)):
        return
    try:
        bundle = await fhir_client.get_bundle_for_patient(patient_id)
    except Exception as exc:
        logger.warning("Bundle warm fetch failed", extra={"patient_id": patient_id, "error": str(exc)})
        return
    await _set_cached_bundle(redis_client, patient_id, bundle)


async def warm_briefing_for_patient(
    redis_client: aioredis.Redis | None,
    patient_id: str,
    langfuse: Any | None = None,
) -> None:
    """Fire-and-forget briefing warmer. Skips if the key already exists.

    Reuses the same path as the dispatcher tool so we never duplicate the
    Anthropic / FHIR call surface.
    """
    if redis_client is None:
        return
    if await _redis_exists(redis_client, _briefing_cache_key(patient_id)):
        return
    try:
        await get_patient_briefing(
            {"patient_id": patient_id},
            {"redis_client": redis_client, "langfuse": langfuse},
        )
    except Exception as exc:
        logger.warning("Briefing warm fetch failed", extra={"patient_id": patient_id, "error": str(exc)})


# ── Tool implementations ──────────────────────────────────────────────────────

async def get_census_summary(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    provider_id: str = input["provider_id"]
    patient_ids: list[str] = input["patient_ids"]

    redis_client: aioredis.Redis | None = session_context.get("redis_client")
    langfuse = session_context.get("langfuse")

    cache_key = census_cache_key(provider_id, patient_ids)

    census_result = await build_census(
        patient_ids,
        redis_client=redis_client,
        cache_key=cache_key,
        provider_id=provider_id,
    )
    entries = census_result.verified
    dropped_ids = census_result.dropped_ids
    annotated = await explain_census(entries, langfuse=langfuse, redis_client=redis_client)

    bundles: dict[str, dict] = {}
    for entry in entries:
        bundle = await _get_cached_bundle(redis_client, entry.patient_id)
        if bundle is None:
            try:
                bundle = await fhir_client.get_bundle_for_patient(entry.patient_id)
                await _set_cached_bundle(redis_client, entry.patient_id, bundle)
            except Exception:
                bundle = {"resources": {}}

        bundles[entry.patient_id] = bundle

    verified = [
        verify_triage_entry(e, bundles.get(e["patient_id"], {"resources": {}}))
        for e in annotated
    ]

    citations: list[dict[str, Any]] = [
        Citation(
            patient_id=e["patient_id"],
            resource_type="Observation",
            resource_id="",
            effective_datetime=None,
            value_summary=e.get("explanation", ""),
            claim_class="vital",
        ).to_dict()
        for e in verified
    ]

    duration_ms = int((time.monotonic() - t0) * 1000)
    log_tool_outcome(
        tool_name="get_census_summary",
        duration_ms=duration_ms,
        cache="n/a",
        session_id=session_context.get("session_id"),
        extra={"census_size": len(verified)},
    )
    return {
        "result": {
            "census": verified,
            "total": len(verified),
            "requested": len(patient_ids),
            "dropped": len(dropped_ids),
            "dropped_ids": dropped_ids,
        },
        "citations": citations,
        "metadata": _empty_metadata("get_census_summary", None, duration_ms, ["Patient", "Observation", "Condition"]),
    }


async def get_patient_briefing(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    patient_id: str = input["patient_id"]
    langfuse = session_context.get("langfuse")
    redis_client: aioredis.Redis | None = session_context.get("redis_client")

    # Briefings are deterministic given the bundle and are expensive (1-2 LLM calls
    # plus 8 FHIR searches). Serve from Redis when available; first call after a
    # bundle change naturally regenerates because the bundle cache turns over too.
    cached = await _get_cached_briefing(redis_client, patient_id)
    if cached is not None:
        cached.setdefault("metadata", {})["cache"] = "hit"
        duration_ms = int((time.monotonic() - t0) * 1000)
        cached["metadata"]["duration_ms"] = duration_ms
        log_tool_outcome(
            tool_name="get_patient_briefing",
            duration_ms=duration_ms,
            cache="hit",
            session_id=session_context.get("session_id"),
            patient_id=patient_id,
        )
        return cached

    patient = await fhir_client.get_patient(patient_id)
    bundle = await fhir_client.get_bundle_for_patient(patient_id)

    ctx = build_briefing_context(patient, bundle)
    raw_briefing = await generate_briefing(ctx, langfuse=langfuse)
    verified = verify_briefing(raw_briefing, ctx, strict=True)

    citations = _citation_from_briefing(verified, patient_id)
    duration_ms = int((time.monotonic() - t0) * 1000)
    payload = {
        "result": verified.model_dump(),
        "citations": citations,
        "metadata": _empty_metadata(
            "get_patient_briefing",
            patient_id,
            duration_ms,
            ["Patient", "Observation", "Condition", "MedicationRequest", "AllergyIntolerance"],
        ),
    }
    payload["metadata"]["cache"] = "miss"

    await _set_cached_briefing(redis_client, patient_id, payload)

    log_tool_outcome(
        tool_name="get_patient_briefing",
        duration_ms=duration_ms,
        cache="miss",
        session_id=session_context.get("session_id"),
        patient_id=patient_id,
    )
    return payload


async def query_patient_records(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    patient_id: str = input["patient_id"]
    query: str = input["query"]
    session_id: str | None = session_context.get("session_id")

    redis_saver = session_context.get("redis_saver")
    sqlite_saver = session_context.get("sqlite_saver")
    redis_client = session_context.get("redis_client")
    langfuse = session_context.get("langfuse")

    if redis_saver is None or sqlite_saver is None:
        from checkpointer.redis_saver import RedisSaver
        from checkpointer.sqlite_saver import SqliteSaver
        redis_saver = RedisSaver(redis_client) if redis_client else RedisSaver(None)
        sqlite_saver = SqliteSaver()
        await sqlite_saver.init()

    from query.conversation import ConversationHandler
    handler = ConversationHandler(
        redis_saver=redis_saver,
        sqlite_saver=sqlite_saver,
        redis_client=redis_client,
        langfuse=langfuse,
    )
    effective_session = session_id or f"anon-{patient_id}"
    answer_dict = await handler.answer(effective_session, patient_id, query)

    duration_ms = int((time.monotonic() - t0) * 1000)
    log_tool_outcome(
        tool_name="query_patient_records",
        duration_ms=duration_ms,
        cache="n/a",
        session_id=session_id,
        patient_id=patient_id,
    )
    return {
        "result": answer_dict,
        "citations": [],
        "metadata": _empty_metadata("query_patient_records", patient_id, duration_ms, ["Observation", "Condition", "Encounter"]),
    }


async def get_medication_safety(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    patient_id: str = input["patient_id"]
    medication_name: str | None = input.get("medication_name")
    langfuse = session_context.get("langfuse")

    bundle = await fhir_client.get_bundle_for_patient(patient_id)
    resources = bundle.get("resources", {})
    meds = [e.get("resource", e) for e in resources.get("MedicationRequest", [])]
    allergies = [e.get("resource", e) for e in resources.get("AllergyIntolerance", [])]
    observations = [e.get("resource", e) for e in resources.get("Observation", [])]

    report = run_safety_checks(patient_id, meds, allergies, observations)
    report = await add_llm_summary(report, langfuse=langfuse)

    flags_out = [
        {"severity": f.severity, "code": f.code, "message": f.message, "medication": f.medication}
        for f in report.flags
    ]
    if medication_name:
        flags_out = [f for f in flags_out if medication_name.lower() in f["medication"].lower()] or flags_out

    citations: list[dict[str, Any]] = [
        Citation(
            patient_id=patient_id,
            resource_type="MedicationRequest",
            resource_id="",
            effective_datetime=None,
            value_summary=f["message"],
            claim_class="medication",
        ).to_dict()
        for f in flags_out
    ]

    duration_ms = int((time.monotonic() - t0) * 1000)
    log_tool_outcome(
        tool_name="get_medication_safety",
        duration_ms=duration_ms,
        cache="n/a",
        session_id=session_context.get("session_id"),
        patient_id=patient_id,
        extra={"flag_count": len(flags_out)},
    )
    return {
        "result": {
            "patient_id": patient_id,
            "medications_reviewed": report.medications_reviewed,
            "flag_count": len(flags_out),
            "flags": flags_out,
            "summary": report.summary,
        },
        "citations": citations,
        "metadata": _empty_metadata(
            "get_medication_safety",
            patient_id,
            duration_ms,
            ["MedicationRequest", "AllergyIntolerance", "Observation"],
        ),
    }


async def generate_handoff(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    patient_ids: list[str] = input["patient_ids"]
    langfuse = session_context.get("langfuse")

    summaries = await generate_handoffs(patient_ids, langfuse=langfuse)

    # Map I-PASS fields → HandoffPatient shape expected by the frontend renderer:
    #   illness_severity → status
    #   patient_summary  → active_issues (single narrative item)
    #   action_list      → pending_items
    #   situation_awareness + contingency_plan → escalation_triggers
    patients_out = [
        {
            "patient_id": s.patient_id,
            "name": s.name,
            "status": s.illness_severity,
            "active_issues": [s.patient_summary] if s.patient_summary else [],
            "pending_items": s.action_list,
            "escalation_triggers": [
                t for t in [s.situation_awareness, s.contingency_plan] if t
            ],
        }
        for s in summaries
    ]

    duration_ms = int((time.monotonic() - t0) * 1000)
    log_tool_outcome(
        tool_name="generate_handoff",
        duration_ms=duration_ms,
        cache="n/a",
        session_id=session_context.get("session_id"),
        extra={"patient_count": len(patients_out)},
    )
    return {
        "result": {"patients": patients_out, "total": len(patients_out)},
        "citations": [],
        "metadata": _empty_metadata(
            "generate_handoff",
            None,
            duration_ms,
            ["Patient", "Observation", "Condition", "MedicationRequest"],
        ),
    }


async def get_triage_rationale(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    """Direct-call tool — NOT registered with dispatcher.

    Called by React panel click-to-expand via POST /agent/triage_rationale.
    Recomputes triage criteria + rationale for the given patient.
    """
    t0 = time.monotonic()
    patient_id: str = input["patient_id"]

    bundle = await fhir_client.get_bundle_for_patient(patient_id)
    criteria = extract_criteria(bundle)
    triage_result = rank(criteria)

    rationale = {
        "patient_id": patient_id,
        "triage_level": triage_result.level,
        "triage_label": triage_result.label,
        "description": triage_result.description,
        "matched_criteria": triage_result.matched_criteria,
        "thresholds_crossed": [
            f"{k}={v}" for k, v in triage_result.matched_criteria.items()
        ],
    }

    entry = {
        "patient_id": patient_id,
        "explanation": triage_result.description,
        "matched_criteria": triage_result.matched_criteria,
    }
    verified = verify_triage_entry(entry, bundle)
    rationale["verification_warnings"] = verified.get("verification_warnings", [])

    duration_ms = int((time.monotonic() - t0) * 1000)
    return {
        "result": rationale,
        "citations": [
            Citation(
                patient_id=patient_id,
                resource_type="Observation",
                resource_id="",
                effective_datetime=None,
                value_summary=f"{k}={v}",
                claim_class="vital",
            ).to_dict()
            for k, v in triage_result.matched_criteria.items()
        ],
        "metadata": _empty_metadata(
            "get_triage_rationale",
            patient_id,
            duration_ms,
            ["Observation", "Condition"],
        ),
    }


__all__ = [
    "get_census_summary",
    "get_patient_briefing",
    "query_patient_records",
    "get_medication_safety",
    "generate_handoff",
    "get_triage_rationale",
    "warm_bundle_for_patient",
    "warm_briefing_for_patient",
]
