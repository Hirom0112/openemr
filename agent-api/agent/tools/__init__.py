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
    agent_pid_normalization_total,
    agent_prewarm_runs_total,
)
from observability.tool_logging import CacheState, log_tool_outcome
from auth.fhir_client import fhir_client
from briefing.context_builder import build as build_briefing_context
from briefing.generator import generate_briefing
from briefing.schema import BriefingResponse
from config import settings
from handoff.generator import generate_handoffs
from medication.safety import add_llm_summary, run_safety_checks
from triage.census import _patient_pid, build_census, census_cache_key
from triage.criteria import extract as extract_criteria
from triage.explainer import explain_census
from triage.rules_engine import rank
from verification.domain_constraints import verify_conversation_answer, verify_safety_summary, verify_triage_entry
from verification.source_attribution import extract_citations, verify_briefing

logger = logging.getLogger(__name__)


# ── Patient-id normalization ─────────────────────────────────────────────────
#
# WHY: the LLM emits patient_id in synthetic FHIR-style (``pt-008``) because
# the system prompt advertises that form, but the dispatcher's census-scope
# check and several downstream tools historically expected the OpenEMR pid
# (``"8"``).  When the forms disagreed the first tool call failed, the
# dispatcher's self-correction loop kicked in, and the model spent 3-4 extra
# turns retrying — costing 6-12s of wall-clock per query.  Normalizing once
# at the entry of every single-patient tool collapses that to a single turn.
#
# Accepted forms:
#   ``pt-008`` → ``"8"``    (synthetic FHIR-style, the common misroute)
#   ``pt-2``   → ``"2"``
#   ``"8"``    → ``"8"``    (already canonical)
#   FHIR UUID  → unchanged  (fhir_client._resolve_patient_id handles it)
#   None       → None       (caller errors gracefully)

import re as _re  # local alias — module-level ``re`` import would shadow nothing

_PT_SYNTHETIC = _re.compile(r"^pt-(\d+)$")


def _normalize_patient_id(
    raw: str | None,
    session_context: dict[str, Any],  # noqa: ARG001 — reserved for future cohort lookups
) -> str | None:
    """Normalize a tool-input patient_id to the canonical OpenEMR pid string.

    See the module-level comment for the rationale.  Increments
    ``agent_pid_normalization_total`` so we can monitor which forms arrive in
    production.  Never raises — unknown inputs are returned unchanged so the
    downstream tool surfaces a clean error instead of crashing on the
    normalization path itself.
    """
    if raw is None or raw == "":
        agent_pid_normalization_total.labels(form="unknown").inc()
        return raw  # let the tool's own validation surface the empty input

    m = _PT_SYNTHETIC.match(raw)
    if m is not None:
        agent_pid_normalization_total.labels(form="synthetic_to_pid").inc()
        return str(int(m.group(1)))  # strip the zero-pad: pt-008 → "8"

    if raw.isdigit():
        agent_pid_normalization_total.labels(form="already_pid").inc()
        return raw

    # Anything else (UUIDs, FHIR-style ids) we leave to fhir_client._resolve_patient_id.
    agent_pid_normalization_total.labels(form="uuid_to_pid").inc()
    return raw


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
) -> str | None:
    """Cache the bundle and return its fingerprint (UTC iso timestamp).

    The fingerprint lets downstream caches (briefing) detect when the
    underlying FHIR bundle has been refreshed and invalidate themselves.
    Returns None when no Redis client is available or the write failed.
    """
    if redis_client is None:
        return None
    fingerprint = datetime.now(timezone.utc).isoformat()
    # Stamp into the dict in-place so callers using the same dict downstream
    # see the same fingerprint. Safe — we only add an underscored key.
    bundle["_cached_at"] = fingerprint
    try:
        await redis_client.setex(
            _bundle_cache_key(patient_id),
            settings.bundle_cache_ttl_seconds,
            json.dumps(bundle),
        )
    except Exception as exc:
        logger.warning("Bundle cache write failed", extra={"patient_id": patient_id, "error": str(exc)})
        return None
    return fingerprint


async def _peek_bundle_fingerprint(
    redis_client: aioredis.Redis | None,
    patient_id: str,
) -> str | None:
    """Read the current bundle's cache fingerprint without disturbing metrics.

    Used by the briefing cache to detect whether the underlying bundle has
    turned over since the briefing was last generated. Returns None on miss
    or any error — callers treat None as "cannot validate, regenerate".
    """
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(_bundle_cache_key(patient_id))
    except Exception as exc:
        logger.warning(
            "Bundle fingerprint peek failed patient_id=%s error=%s", patient_id, exc
        )
        return None
    if not raw:
        return None
    try:
        bundle = json.loads(raw)
    except Exception:
        return None
    fp = bundle.get("_cached_at")
    return fp if isinstance(fp, str) else None


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
    *,
    force_refresh: bool = False,
) -> None:
    """Fire-and-forget bundle warmer.

    Skips if the key already exists *unless* ``force_refresh`` is True, in
    which case the FHIR fetch always runs and overwrites the cached entry
    (also bumping its fingerprint so any briefing keyed on the previous
    fingerprint will treat itself as stale on the next read).
    """
    if redis_client is None:
        return
    if not force_refresh and await _redis_exists(redis_client, _bundle_cache_key(patient_id)):
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
    *,
    force_refresh: bool = False,
) -> None:
    """Fire-and-forget briefing warmer.

    Skips if the key already exists *unless* ``force_refresh`` is True, in
    which case ``get_patient_briefing`` is invoked with ``force_refresh=True``
    so both the briefing and its underlying bundle are regenerated.

    Reuses the same path as the dispatcher tool so we never duplicate the
    Anthropic / FHIR call surface.
    """
    if redis_client is None:
        return
    if not force_refresh and await _redis_exists(redis_client, _briefing_cache_key(patient_id)):
        return
    try:
        await get_patient_briefing(
            {"patient_id": patient_id, "force_refresh": force_refresh},
            {"redis_client": redis_client, "langfuse": langfuse},
        )
    except Exception as exc:
        logger.warning("Briefing warm fetch failed", extra={"patient_id": patient_id, "error": str(exc)})


async def warm_medication_safety_for_patient(
    redis_client: aioredis.Redis | None,
    patient_id: str,
    langfuse: Any | None = None,
    *,
    force_refresh: bool = False,
) -> None:
    """Fire-and-forget medication-safety warmer.

    The medication-safety tool itself does not currently maintain its own
    Redis cache — its expensive inputs are the FHIR bundle (which IS cached)
    and a single short LLM summarization call. Running the tool here ensures
    the bundle is freshly populated for the patient and exercises the same
    code path the UI hits, so the first user-facing click pays only the LLM
    cost (~1-2s) instead of the full FHIR fanout (~6-8s).

    When ``force_refresh`` is True the bundle is force-refetched first so the
    safety report reflects current FHIR state rather than whatever the
    bundle cache had from the previous shift.
    """
    if redis_client is None:
        return
    try:
        if force_refresh:
            # Force the underlying bundle to refresh before the safety tool
            # reads it from Redis. The tool itself does no cache check.
            await warm_bundle_for_patient(redis_client, patient_id, force_refresh=True)
        await get_medication_safety(
            {"patient_id": patient_id},
            {"redis_client": redis_client, "langfuse": langfuse},
        )
    except Exception as exc:
        logger.warning(
            "Medication safety warm fetch failed",
            extra={"patient_id": patient_id, "error": str(exc)},
        )


# ── Tool implementations ──────────────────────────────────────────────────────

async def get_census_summary(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    provider_id: str = input["provider_id"]
    patient_ids: list[str] = input["patient_ids"]
    # Surfaced from the UI Refresh button via /triage/census. The dispatcher
    # path leaves this absent (default False) so LLM-driven census calls keep
    # using the warm cache.
    force_refresh: bool = bool(input.get("force_refresh", False))

    redis_client: aioredis.Redis | None = session_context.get("redis_client")
    langfuse = session_context.get("langfuse")

    cache_key = census_cache_key(provider_id, patient_ids)

    try:
        census_result = await build_census(
            patient_ids,
            redis_client=redis_client,
            cache_key=cache_key,
            provider_id=provider_id,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        # Auto-discovery (empty patient_ids) issues a bulk Patient query that
        # currently 500s on OpenEMR's FHIR endpoint due to a
        # SearchFieldOrder type bug. If the dispatcher has already supplied
        # the active session's patient_ids in session_context, fall back to
        # rebuilding the census against those — same idiom as the
        # /agent/prefetch warmer in main.py:_warm().
        session_patient_ids: list[str] = list(session_context.get("patient_ids") or [])
        if not patient_ids and session_patient_ids:
            logger.warning(
                "census_auto_discovery_failed_falling_back_to_session_ids",
                extra={
                    "error": str(exc),
                    "session_patient_count": len(session_patient_ids),
                    "request_id": session_context.get("request_id"),
                },
            )
            agent_prewarm_runs_total.labels(
                outcome="census_auto_discovery_failed_fallback"
            ).inc()
            cache_key = census_cache_key(provider_id, session_patient_ids)
            census_result = await build_census(
                session_patient_ids,
                redis_client=redis_client,
                cache_key=cache_key,
                provider_id=provider_id,
                force_refresh=force_refresh,
            )
            patient_ids = session_patient_ids
        else:
            raise
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

    # Fan out briefing warmers in the background — bounded by a small
    # semaphore so we don't hammer FHIR / Anthropic. Fire-and-forget: the
    # census itself returns immediately so the UI render is not blocked.
    # By the time the clinician clicks "Brief X", the briefing is in Redis.
    _schedule_census_briefing_warm(
        redis_client=redis_client,
        patient_ids=[e["patient_id"] for e in verified],
        session_id=session_context.get("session_id"),
        request_id=session_context.get("request_id"),
        langfuse=langfuse,
    )

    return {
        "result": {
            "census": verified,
            "total": len(verified),
            "requested": len(patient_ids),
            "dropped": len(dropped_ids),
            "dropped_ids": dropped_ids,
            # Frontend uses this for the freshness indicator. On cache hits
            # this is the ORIGINAL build time, not the cache-read time —
            # so 'Census as of HH:MM' matches the briefing's data-as-of line.
            "generated_at": census_result.generated_at,
        },
        "citations": citations,
        "metadata": _empty_metadata("get_census_summary", None, duration_ms, ["Patient", "Observation", "Condition"]),
    }


def _schedule_census_briefing_warm(
    *,
    redis_client: aioredis.Redis | None,
    patient_ids: list[str],
    session_id: str | None,
    request_id: str | None,
    langfuse: Any | None,
) -> None:
    """Schedule background briefing warmers for every census patient.

    Uses ``asyncio.create_task`` so the caller (``get_census_summary``)
    returns immediately. Each per-patient warmer is wrapped in try/except so
    a single failure cannot escape and crash the event loop.
    """
    if redis_client is None:
        logger.debug(
            "census_briefing_warm_skipped_no_redis",
            extra={"session_id": session_id, "request_id": request_id, "patient_count": len(patient_ids)},
        )
        return

    if not patient_ids:
        return

    sem = asyncio.Semaphore(4)

    async def _warm_one(pid: str) -> None:
        try:
            async with sem:
                await warm_briefing_for_patient(redis_client, pid, langfuse=langfuse)
        except Exception as exc:
            logger.warning(
                "census_briefing_warm_failed",
                extra={
                    "session_id": session_id,
                    "request_id": request_id,
                    "patient_id": pid,
                    "error": str(exc),
                },
            )

    for pid in patient_ids:
        asyncio.create_task(_warm_one(pid))

    agent_prewarm_runs_total.labels(outcome="scheduled").inc()
    logger.info(
        "census_briefing_warm_scheduled",
        extra={
            "session_id": session_id,
            "request_id": request_id,
            "patient_count": len(patient_ids),
        },
    )


async def get_patient_briefing(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    patient_id: str = input["patient_id"]
    # Briefing intentionally does NOT normalize: its Redis cache keys
    # (``copilot:briefing:{patient_id}``) are stable on whatever form the
    # caller passed in, and both forms route to the same FHIR data via
    # ``fhir_client._resolve_patient_id``. Normalizing here would split the
    # cache between ``copilot:briefing:pt-008`` and ``copilot:briefing:8``.
    force_refresh: bool = bool(input.get("force_refresh", False))
    langfuse = session_context.get("langfuse")
    redis_client: aioredis.Redis | None = session_context.get("redis_client")

    # Briefings are deterministic given the bundle and are expensive (1-2 LLM calls
    # plus 8 FHIR searches). Serve from Redis when available; first call after a
    # bundle change naturally regenerates because the bundle cache turns over too.
    # When the user explicitly clicks Refresh (force_refresh=True), bypass both
    # caches so they get a brand-new generated_at timestamp.
    if not force_refresh:
        cached = await _get_cached_briefing(redis_client, patient_id)
        if cached is not None:
            # Bundle-fingerprint validation: a briefing's freshness IS its
            # bundle's freshness. If the bundle cache has turned over since
            # this briefing was generated (e.g. census refresh repopulated
            # FHIR data), the cached briefing is stale even if its TTL has
            # not elapsed. Treat as a miss so the caller sees a generated_at
            # that matches the underlying bundle.
            cached_fp = cached.get("metadata", {}).get("bundle_fingerprint")
            current_fp = await _peek_bundle_fingerprint(redis_client, patient_id)
            stale = (current_fp is None) or (cached_fp != current_fp)
            if not stale:
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
            logger.info(
                "briefing_cache_invalidated_bundle_changed",
                extra={
                    "patient_id": patient_id,
                    "cached_fingerprint": cached_fp,
                    "current_fingerprint": current_fp,
                },
            )

    patient = await fhir_client.get_patient(patient_id)

    if force_refresh:
        bundle = await fhir_client.get_bundle_for_patient(patient_id)
        bundle_cache_state: CacheState = "miss"
        bundle_fingerprint = await _set_cached_bundle(redis_client, patient_id, bundle)
    else:
        bundle = await _get_cached_bundle(redis_client, patient_id)
        bundle_cache_state = "hit" if bundle is not None else "miss"
        if bundle is None:
            bundle = await fhir_client.get_bundle_for_patient(patient_id)
            bundle_fingerprint = await _set_cached_bundle(redis_client, patient_id, bundle)
        else:
            # Bundle came from cache — its fingerprint is already stamped.
            fp = bundle.get("_cached_at")
            bundle_fingerprint = fp if isinstance(fp, str) else None

    ctx = build_briefing_context(patient, bundle)
    raw_briefing = await generate_briefing(ctx, langfuse=langfuse)
    verified = verify_briefing(raw_briefing, ctx, strict=True)

    citations = _citation_from_briefing(verified, patient_id)
    duration_ms = int((time.monotonic() - t0) * 1000)
    # WHY: frontend's "Verify in Chart" button needs the numeric OpenEMR PID to
    # build a working set_pid deep-link. The FHIR UUID won't pass demographics.php.
    result_data = verified.model_dump()
    result_data["openemr_pid"] = _patient_pid(patient)
    payload = {
        "result": result_data,
        "citations": citations,
        "metadata": _empty_metadata(
            "get_patient_briefing",
            patient_id,
            duration_ms,
            ["Patient", "Observation", "Condition", "MedicationRequest", "AllergyIntolerance"],
        ),
    }
    payload["metadata"]["cache"] = "miss"
    if force_refresh:
        payload["metadata"]["forced_refresh"] = True
    # Stamp the underlying bundle's fingerprint so future cache reads can
    # detect when the bundle has been refreshed and treat the briefing as
    # stale. See _peek_bundle_fingerprint and the cache-hit branch above.
    if bundle_fingerprint is not None:
        payload["metadata"]["bundle_fingerprint"] = bundle_fingerprint

    await _set_cached_briefing(redis_client, patient_id, payload)

    log_tool_outcome(
        tool_name="get_patient_briefing",
        duration_ms=duration_ms,
        cache="miss",
        session_id=session_context.get("session_id"),
        patient_id=patient_id,
        extra={"bundle_cache": bundle_cache_state},
    )
    return payload


async def query_patient_records(
    input: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    patient_id_raw: str = input["patient_id"]
    patient_id = _normalize_patient_id(patient_id_raw, session_context) or patient_id_raw
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
    from query.router import route as route_query

    handler = ConversationHandler(
        redis_saver=redis_saver,
        sqlite_saver=sqlite_saver,
        redis_client=redis_client,
        langfuse=langfuse,
    )
    effective_session = session_id or f"anon-{patient_id}"

    # Slice the cached bundle for the query's target resource and pass the
    # records to the handler. WHY: the live FHIR fallback in
    # ``query/fhir_search.py`` can't be relied on because (a) it does not
    # resolve numeric PIDs to FHIR UUIDs, so ``patient=8`` returns 0 results,
    # and (b) it forwards the router's ``clinical-status=active`` /
    # ``status=active`` filters which OpenEMR's FHIR search drops on the
    # floor (see auth/fhir_client.get_bundle_for_patient for the same
    # workaround on the briefing path). Reusing the bundle keeps query
    # answers in lockstep with what the briefing surfaces.
    records_override: list[dict[str, Any]] | None = None
    try:
        query_route = await route_query(query, patient_id)
        bundle = await _get_cached_bundle(redis_client, patient_id)
        if bundle is None:
            bundle = await fhir_client.get_bundle_for_patient(patient_id)
            await _set_cached_bundle(redis_client, patient_id, bundle)
        bundle_resources = bundle.get("resources", {})
        entries = bundle_resources.get(query_route.resource, [])
        records_override = [e.get("resource", e) for e in entries]
        logger.debug(
            "query_patient_records bundle slice",
            extra={
                "patient_id": patient_id,
                "resource": query_route.resource,
                "count": len(records_override),
            },
        )
    except Exception as exc:
        logger.warning(
            "query_patient_records bundle slice failed; falling back to live FHIR",
            extra={"patient_id": patient_id, "error": str(exc)},
        )
        records_override = None

    answer_dict = await handler.answer(
        effective_session,
        patient_id,
        query,
        records_override=records_override,
    )

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
    patient_id_raw: str = input["patient_id"]
    patient_id = _normalize_patient_id(patient_id_raw, session_context) or patient_id_raw
    medication_name: str | None = input.get("medication_name")
    langfuse = session_context.get("langfuse")
    redis_client: aioredis.Redis | None = session_context.get("redis_client")

    # Bundle cache mirrors the briefing tool's read-through pattern so this
    # tool no longer triggers a fresh 8-search FHIR fanout per call.
    bundle = await _get_cached_bundle(redis_client, patient_id)
    if bundle is None:
        bundle = await fhir_client.get_bundle_for_patient(patient_id)
        await _set_cached_bundle(redis_client, patient_id, bundle)
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

    # Renderer-friendly string lists — match `MedicationSafetyData` in
    # agent-ui/src/types.ts so the structured renderer has content to display
    # for both the dispatcher path and the direct GET /medication/safety/{id}
    # endpoint.  Derived from the same FHIR resources we just loaded.
    def _med_display(res: dict[str, Any]) -> str:
        cc = res.get("medicationCodeableConcept", {}) or {}
        coding = (cc.get("coding") or [{}])[0]
        return coding.get("display") or cc.get("text") or "Unknown medication"

    def _allergy_display(res: dict[str, Any]) -> str:
        code = res.get("code", {}) or {}
        coding = (code.get("coding") or [{}])[0]
        return coding.get("display") or code.get("text") or "Unknown allergen"

    current_medications = [_med_display(m) for m in meds]
    allergy_list = [_allergy_display(a) for a in allergies]
    # Interactions surface non-allergy safety flags (lab interactions and
    # high-alert medication notices) so the renderer's "Interactions of
    # concern" section conveys the deterministic safety output.
    interactions = [
        f["message"] for f in flags_out if f.get("code") != "ALLERGY_CONFLICT"
    ]

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
            # Renderer fields (string lists) — see MedicationSafetyData in
            # agent-ui/src/types.ts.
            "current_medications": current_medications,
            "allergies": allergy_list,
            "interactions": interactions,
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

    summaries = await generate_handoffs(patient_ids, langfuse=langfuse, redis_client=session_context.get("redis_client"))

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
    patient_id_raw: str = input["patient_id"]
    patient_id = _normalize_patient_id(patient_id_raw, session_context) or patient_id_raw
    redis_client: aioredis.Redis | None = session_context.get("redis_client")

    # Bundle cache mirrors the briefing tool — keeps this direct-call endpoint
    # off the FHIR critical path when the census already warmed the bundle.
    bundle = await _get_cached_bundle(redis_client, patient_id)
    if bundle is None:
        bundle = await fhir_client.get_bundle_for_patient(patient_id)
        await _set_cached_bundle(redis_client, patient_id, bundle)
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
    "warm_medication_safety_for_patient",
]
