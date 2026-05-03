"""UC-5 Parallel Handoff Generation.

Generates I-PASS handoff summaries for multiple patients in parallel.
Each handoff covers: Illness severity, Patient summary, Action list,
Situation awareness, Synthesis by receiver (prompts only).

All handoffs are generated concurrently — the bottleneck is FHIR
fetch latency, not LLM throughput, so parallelism is the right strategy.

Output: list[HandoffSummary] sorted by triage level (most urgent first).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anthropic
import redis.asyncio as aioredis
from langfuse import Langfuse

from agent.metrics import (
    agent_data_cache_hits_total,
    agent_data_cache_misses_total,
)
from agent.response_schemas import PRODUCE_HANDOFF
from auth.fhir_client import fhir_client
from briefing.context_builder import build as build_context
from config import settings
from triage.criteria import extract
from triage.rules_engine import rank

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SYSTEM = """You are generating an I-PASS handoff summary for a patient.
Use the produce_handoff tool to return your response.
Use only data from the patient context provided.
Do not speculate.  Do not make treatment recommendations."""


@dataclass
class HandoffSummary:
    patient_id: str
    name: str
    mrn: str
    triage_level: int
    illness_severity: str
    patient_summary: str
    action_list: list[str]
    situation_awareness: str
    contingency_plan: str
    generated_at: str
    error: str | None = None


async def _get_cached_bundle(
    redis_client: aioredis.Redis | None,
    patient_id: str,
) -> dict[str, Any] | None:
    """Read ``copilot:bundle:{patient_id}`` from Redis. Treats errors as miss.

    Inlined here (not shared with ``agent.tools``) because ``handoff`` must
    remain a leaf module per ``agent-api/.importlinter`` —
    ``no-upward-into-agent-tools``.
    """
    if redis_client is None:
        return None
    key = f"copilot:bundle:{patient_id}"
    try:
        raw = await redis_client.get(key)
    except Exception as exc:
        logger.debug(
            "Handoff bundle cache read failed",
            extra={"cache": "miss", "site": "handoff_bundle", "patient_id": patient_id, "error": str(exc)},
        )
        agent_data_cache_misses_total.labels(cache="bundle").inc()
        return None
    if raw:
        try:
            bundle = json.loads(raw)
        except Exception as exc:
            logger.debug(
                "Handoff bundle cache decode failed",
                extra={"cache": "miss", "site": "handoff_bundle", "patient_id": patient_id, "error": str(exc)},
            )
            agent_data_cache_misses_total.labels(cache="bundle").inc()
            return None
        logger.info(
            "Handoff bundle cache hit",
            extra={"cache": "hit", "site": "handoff_bundle", "patient_id": patient_id},
        )
        agent_data_cache_hits_total.labels(cache="bundle").inc()
        return bundle
    logger.info(
        "Handoff bundle cache miss",
        extra={"cache": "miss", "site": "handoff_bundle", "patient_id": patient_id},
    )
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
            f"copilot:bundle:{patient_id}",
            settings.bundle_cache_ttl_seconds,
            json.dumps(bundle),
        )
    except Exception as exc:
        logger.debug(
            "Handoff bundle cache write failed",
            extra={"site": "handoff_bundle", "patient_id": patient_id, "error": str(exc)},
        )


_HANDOFF_CACHE_TTL_SECONDS = 600  # ~10 min — invalidates anyway when bundle refreshes


def _handoff_cache_key(patient_id: str, fingerprint: str) -> str:
    """Cache key binds the handoff to the bundle fingerprint that generated it.

    When the bundle cache turns over (e.g. census refresh re-fetches FHIR),
    the fingerprint changes and the next handoff lookup misses naturally —
    no separate invalidation pass needed.
    """
    return f"copilot:handoff:{patient_id}:{fingerprint}"


async def _get_cached_handoff(
    redis_client: aioredis.Redis | None,
    patient_id: str,
    fingerprint: str,
) -> HandoffSummary | None:
    if redis_client is None or not fingerprint:
        return None
    key = _handoff_cache_key(patient_id, fingerprint)
    try:
        raw = await redis_client.get(key)
    except Exception as exc:
        logger.warning(
            "Handoff cache read failed",
            extra={"cache": "miss", "site": "handoff", "patient_id": patient_id, "error": str(exc)},
        )
        agent_data_cache_misses_total.labels(cache="handoff").inc()
        return None
    if not raw:
        agent_data_cache_misses_total.labels(cache="handoff").inc()
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:
        logger.warning(
            "Handoff cache decode failed",
            extra={"site": "handoff", "patient_id": patient_id, "error": str(exc)},
        )
        agent_data_cache_misses_total.labels(cache="handoff").inc()
        return None
    agent_data_cache_hits_total.labels(cache="handoff").inc()
    try:
        return HandoffSummary(**data)
    except Exception as exc:
        logger.warning(
            "Handoff cache shape mismatch",
            extra={"site": "handoff", "patient_id": patient_id, "error": str(exc)},
        )
        return None


async def _set_cached_handoff(
    redis_client: aioredis.Redis | None,
    patient_id: str,
    fingerprint: str,
    summary: HandoffSummary,
) -> None:
    if redis_client is None or not fingerprint:
        return
    from dataclasses import asdict
    try:
        await redis_client.setex(
            _handoff_cache_key(patient_id, fingerprint),
            _HANDOFF_CACHE_TTL_SECONDS,
            json.dumps(asdict(summary)),
        )
    except Exception as exc:
        logger.warning(
            "Handoff cache write failed",
            extra={"site": "handoff", "patient_id": patient_id, "error": str(exc)},
        )


async def _generate_one(
    patient_id: str,
    client: anthropic.AsyncAnthropic,
    langfuse: Langfuse | None = None,
    redis_client: aioredis.Redis | None = None,
) -> HandoffSummary:
    from dataclasses import asdict

    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        patient = await fhir_client.get_patient(patient_id)
        bundle = await _get_cached_bundle(redis_client, patient_id)
        if bundle is None:
            bundle = await fhir_client.get_bundle_for_patient(patient_id)
            await _set_cached_bundle(redis_client, patient_id, bundle)
    except Exception as exc:
        logger.warning("FHIR fetch failed for handoff", extra={"patient_id": patient_id, "error": str(exc)})
        return HandoffSummary(
            patient_id=patient_id, name="Unknown", mrn="", triage_level=10,
            illness_severity="Unknown", patient_summary="FHIR data unavailable.",
            action_list=[], situation_awareness="", contingency_plan="",
            generated_at=generated_at, error=str(exc),
        )

    # Per-patient handoff cache keyed on the bundle's fingerprint. Hit means
    # we can skip the LLM call entirely for this patient — the cached I-PASS
    # is still valid because the underlying bundle has not changed.
    fingerprint_raw = bundle.get("_cached_at") if isinstance(bundle, dict) else None
    fingerprint = fingerprint_raw if isinstance(fingerprint_raw, str) else ""
    cached_summary = await _get_cached_handoff(redis_client, patient_id, fingerprint)
    if cached_summary is not None:
        return cached_summary

    ctx = build_context(patient, bundle)
    criteria = extract(bundle)
    triage = rank(criteria)

    prompt = (
        f"Generate an I-PASS handoff for this patient.\n\n"
        f"<patient_data>\n"
        f"PATIENT CONTEXT:\n{json.dumps(asdict(ctx), indent=2)}\n\n"
        f"TRIAGE: Level {triage.level} — {triage.label}\n"
        f"</patient_data>\n\n"
        f"Call the produce_handoff tool with the complete I-PASS summary."
    )

    trace = langfuse.trace(name="handoff-generate", user_id=patient_id) if langfuse else None
    generation = trace.generation(name="handoff-llm", model=_MODEL, input=prompt) if trace else None

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[PRODUCE_HANDOFF],
            tool_choice={"type": "any"},
        )

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            raise ValueError("No tool_use block in handoff response")

        data = tool_block.input
        if generation:
            generation.end(output=data)

        summary = HandoffSummary(
            patient_id=patient_id,
            name=ctx.name,
            mrn=ctx.mrn,
            triage_level=triage.level,
            illness_severity=data.get("illness_severity", "Unknown"),
            patient_summary=data.get("patient_summary", ""),
            action_list=data.get("action_list", []),
            situation_awareness=data.get("situation_awareness", ""),
            contingency_plan=data.get("contingency_plan", ""),
            generated_at=generated_at,
        )
        await _set_cached_handoff(redis_client, patient_id, fingerprint, summary)
        return summary
    except Exception as exc:
        logger.error("Handoff LLM failed", extra={"patient_id": patient_id, "error": str(exc)})
        return HandoffSummary(
            patient_id=patient_id, name=ctx.name, mrn=ctx.mrn,
            triage_level=triage.level, illness_severity="Unknown",
            patient_summary="Handoff generation failed — review chart directly.",
            action_list=[], situation_awareness="", contingency_plan="",
            generated_at=generated_at, error=str(exc),
        )


# Bumped 4 → 8 (2026-05): Anthropic's anthropic-ratelimit-requests-limit
# header reports 1000 RPM on this account, so 8 concurrent handoff LLM calls
# leaves ample headroom even when the briefing/medication paths are also
# firing. Bottleneck moves from per-shift latency to upstream FHIR fanout.
_CONCURRENCY = 8          # max parallel Anthropic calls
_PATIENT_TIMEOUT = 25.0   # seconds per patient before returning error stub


OnPatientComplete = Callable[[HandoffSummary], Awaitable[None]]


async def generate_handoffs(
    patient_ids: list[str],
    langfuse: Langfuse | None = None,
    redis_client: aioredis.Redis | None = None,
    on_patient_complete: OnPatientComplete | None = None,
) -> list[HandoffSummary]:
    """Generate handoff summaries for all patients in parallel.

    When ``on_patient_complete`` is provided, the callback is awaited as soon
    as each per-patient task settles (success or error stub). The callback
    fires in completion order, not census order. The aggregate sorted list
    is still returned for backward compatibility.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _bounded(pid: str) -> HandoffSummary:
        async with sem:
            try:
                summary = await asyncio.wait_for(
                    _generate_one(pid, client, langfuse, redis_client),
                    timeout=_PATIENT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Handoff timed out", extra={"patient_id": pid})
                summary = HandoffSummary(
                    patient_id=pid, name="Unknown", mrn="", triage_level=10,
                    illness_severity="Unknown",
                    patient_summary="Handoff timed out — review chart directly.",
                    action_list=[], situation_awareness="", contingency_plan="",
                    generated_at=datetime.now(timezone.utc).isoformat(),
                    error="timeout",
                )
        if on_patient_complete is not None:
            try:
                await on_patient_complete(summary)
            except Exception as exc:
                logger.warning(
                    "on_patient_complete callback raised",
                    extra={"patient_id": pid, "error": str(exc)},
                )
        return summary

    summaries = await asyncio.gather(*[_bounded(pid) for pid in patient_ids])
    result = list(summaries)
    result.sort(key=lambda s: s.triage_level)
    return result
