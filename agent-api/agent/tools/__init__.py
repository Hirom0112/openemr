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
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from auth.fhir_client import fhir_client
from briefing.context_builder import build as build_briefing_context
from briefing.generator import generate_briefing
from briefing.schema import BriefingResponse
from handoff.generator import generate_handoffs
from medication.safety import add_llm_summary, run_safety_checks
from triage.census import build_census
from triage.criteria import extract as extract_criteria
from triage.explainer import explain_census
from triage.rules_engine import rank
from verification.domain_constraints import verify_conversation_answer, verify_safety_summary, verify_triage_entry
from verification.source_attribution import verify_briefing

logger = logging.getLogger(__name__)


# ── Citation helpers ──────────────────────────────────────────────────────────

def _citation_from_briefing(briefing: BriefingResponse, patient_id: str) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for section in briefing.sections:
        for claim in section.claims:
            citations.append({
                "patient_id": patient_id,
                "resource_type": claim.source_resource,
                "resource_id": claim.source_code,
                "effective_datetime": claim.source_dt,
                "value_summary": claim.source_value,
            })
    return citations


def _empty_metadata(tool: str, patient_id: str | None, duration_ms: int, resources: list[str]) -> dict[str, Any]:
    return {
        "tool": tool,
        "patient_id": patient_id,
        "duration_ms": duration_ms,
        "fhir_resources_accessed": resources,
    }


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
    session_id: str | None = session_context.get("session_id")

    cache_key = f"copilot:census:{session_id}" if session_id else None

    entries = await build_census(patient_ids, redis_client=redis_client, cache_key=cache_key)
    annotated = await explain_census(entries, langfuse=langfuse)

    bundles: dict[str, dict] = {}
    for entry in entries:
        try:
            bundles[entry.patient_id] = await fhir_client.get_bundle_for_patient(entry.patient_id)
        except Exception:
            bundles[entry.patient_id] = {"resources": {}}

    verified = [
        verify_triage_entry(e, bundles.get(e["patient_id"], {"resources": {}}))
        for e in annotated
    ]

    citations: list[dict[str, Any]] = [
        {
            "patient_id": e["patient_id"],
            "resource_type": "Observation",
            "resource_id": "",
            "effective_datetime": "",
            "value_summary": e.get("explanation", ""),
        }
        for e in verified
    ]

    duration_ms = int((time.monotonic() - t0) * 1000)
    return {
        "result": {"census": verified, "total": len(verified)},
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

    patient = await fhir_client.get_patient(patient_id)
    bundle = await fhir_client.get_bundle_for_patient(patient_id)

    ctx = build_briefing_context(patient, bundle)
    raw_briefing = await generate_briefing(ctx, langfuse=langfuse)
    verified = verify_briefing(raw_briefing, ctx, strict=True)

    citations = _citation_from_briefing(verified, patient_id)
    duration_ms = int((time.monotonic() - t0) * 1000)
    return {
        "result": verified.model_dump(),
        "citations": citations,
        "metadata": _empty_metadata(
            "get_patient_briefing",
            patient_id,
            duration_ms,
            ["Patient", "Observation", "Condition", "MedicationRequest", "AllergyIntolerance"],
        ),
    }


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
        {
            "patient_id": patient_id,
            "resource_type": "MedicationRequest",
            "resource_id": "",
            "effective_datetime": "",
            "value_summary": f["message"],
        }
        for f in flags_out
    ]

    duration_ms = int((time.monotonic() - t0) * 1000)
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

    handoffs_out = [
        {
            "patient_id": s.patient_id,
            "name": s.name,
            "mrn": s.mrn,
            "triage_level": s.triage_level,
            "illness_severity": s.illness_severity,
            "patient_summary": s.patient_summary,
            "action_list": s.action_list,
            "situation_awareness": s.situation_awareness,
            "contingency_plan": s.contingency_plan,
            "generated_at": s.generated_at,
            **({"error": s.error} if s.error else {}),
        }
        for s in summaries
    ]

    duration_ms = int((time.monotonic() - t0) * 1000)
    return {
        "result": {"handoffs": handoffs_out, "total": len(handoffs_out)},
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
            {
                "patient_id": patient_id,
                "resource_type": "Observation",
                "resource_id": "",
                "effective_datetime": "",
                "value_summary": f"{k}={v}",
            }
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
]
