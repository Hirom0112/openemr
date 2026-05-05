from __future__ import annotations

"""Clinical Co-Pilot — agent-api entry point.

Exposes:
  GET  /health                       — liveness / readiness
  GET  /fhir/patient/{id}            — FHIR proxy for validation
  POST /triage/census                — UC-1: ranked triage list (legacy)
  POST /briefing/{patient_id}        — UC-2: pre-encounter briefing (legacy)
  POST /session/{id}/query           — UC-3: multi-turn targeted record query (legacy)
  GET  /medication/safety/{id}       — UC-4: medication safety surface (legacy)
  POST /handoff/generate             — UC-5: parallel handoff generation (legacy)
  POST /handoff/generate/stream      — UC-5: per-patient SSE streaming handoff
  POST /agent/query                  — dispatcher: all use cases via tool_use loop
  POST /agent/triage_rationale/{id}  — direct-call triage rationale (click-to-expand)
  POST /session/{id}/message         — raw conversation turn (checkpointer)
  GET  /session/{id}/history         — stored conversation turns
"""

import asyncio
import logging
import time
import uuid
from typing import Any

import redis.asyncio as aioredis
from dataclasses import asdict
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langfuse import Langfuse
from prometheus_client import Counter, Histogram, make_asgi_app
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from pydantic import ConfigDict as _PydanticConfig
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# PHP sends numeric PIDs and provider IDs as JSON numbers.
# _CoerceModel tells Pydantic v2 to coerce numbers to str instead of 422-ing.
class _CoerceModel(BaseModel):
    model_config = _PydanticConfig(coerce_numbers_to_str=True)

from agent.dispatcher import dispatch
from agent.metrics import (
    agent_client_timing_seconds,
    agent_dispatch_latency_seconds,
    agent_prewarm_duration_seconds,
    agent_prewarm_runs_total,
    agent_prompt_cache_hits_total,
    agent_prompt_cache_misses_total,
    agent_tool_calls_total,
    agent_tool_misroute_total,
)
from agent.tools import (
    generate_handoff,
    get_census_summary,
    get_medication_safety,
    get_patient_briefing,
    get_triage_rationale,
    query_patient_records,
    warm_briefing_for_patient,
    warm_bundle_for_patient,
    warm_medication_safety_for_patient,
)
from audit import writer as audit_writer
from audit.middleware import audit_middleware
from audit.models import AuditEvent
from auth import request_principal_var
from auth.fhir_client import (
    fhir_client,
    get_access_token,
    invalidate_token_cache,
)
from auth.jwt_middleware import jwt_middleware
from briefing.schema import BriefingResponse
from checkpointer.redis_saver import RedisSaver
from checkpointer.sqlite_saver import SqliteSaver
from config import settings
from observability.json_logging import configure_json_logging, request_id_var
from triage.census import build_census, census_cache_key

configure_json_logging(settings.log_level)
logger = logging.getLogger(__name__)

# ── Legacy per-endpoint metrics (kept for backward compatibility) ─────────────

TRIAGE_LEVEL_COUNTER = Counter(
    "agent_triage_level_total",
    "Number of patients triaged at each priority level",
    ["level"],
)

BRIEFING_DURATION = Histogram(
    "agent_briefing_duration_seconds",
    "End-to-end time to generate a verified patient briefing",
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0),
)

app = FastAPI(title="Clinical Co-Pilot API", version="0.1.0")

# CORS: lock to a single configured browser origin. Empty falls back to "*"
# with a startup warning so local dev keeps working without env churn, but
# any real deployment must set OPENEMR_ORIGIN.
if settings.openemr_origin:
    _cors_origins = [settings.openemr_origin]
else:
    logger.warning(
        "CORS origin not configured (OPENEMR_ORIGIN empty) — falling back to '*'. "
        "Do not run this in production."
    )
    _cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

class RequestIdMiddleware(BaseHTTPMiddleware):
    """Per-request request_id: read X-Request-ID or mint uuid4, propagate via ContextVar."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        token = request_id_var.set(rid)
        try:
            response: Response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


# ── Middleware stack ─────────────────────────────────────────────────────────
# Starlette runs the LAST-REGISTERED middleware OUTERMOST. Registration
# order below is therefore intentional and from INNER (runs near handler)
# to OUTER (runs first on the request).
#
#   CORSMiddleware                       — already registered above (innermost)
#   audit_middleware                     — needs request_id + principal in scope
#   _audit_principal_stash               — copies principal ContextVar → request.state
#   jwt_middleware                       — verifies JWT, sets principal ContextVar
#   RequestIdMiddleware                  — sets request_id ContextVar (outermost)
#
# When a request arrives: RequestId → JWT → stash → audit → CORS → handler.
# This guarantees both ContextVars are bound during audit's call_next, AND
# the principal stash has run before audit reads request.state.

# The ``audit`` package is a leaf in .importlinter and cannot import from
# ``auth``; the stash bridges that boundary by copying ``request_principal_var``
# onto ``request.state.audit_principal`` here in main.py.

app.middleware("http")(audit_middleware)


@app.middleware("http")
async def _audit_principal_stash(request: Request, call_next: Any) -> Response:
    """Copy the verified JWT principal onto request.state for the audit middleware."""
    try:
        principal = request_principal_var.get()
    except LookupError:
        principal = None
    if principal is not None:
        request.state.audit_principal = principal
    return await call_next(request)


# JWT verification + provider-id scope check. Registered as an HTTP middleware
# (not Depends) so it runs once per request and can short-circuit with 401/403
# before any route handler executes. Bypasses entirely when COPILOT_JWT_SECRET
# is empty — see auth/jwt_middleware.py for the bypass-list and scope rules.
app.middleware("http")(jwt_middleware)
app.add_middleware(RequestIdMiddleware)

Instrumentator().instrument(app).expose(app)

# Expose prometheus_client metrics at /metrics (in addition to the
# fastapi-instrumentator default at /metrics already above — the mount
# adds the full prometheus_client registry including our custom counters).
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

_redis: aioredis.Redis | None = None
_redis_saver: RedisSaver | None = None
_sqlite_saver: SqliteSaver | None = None
_langfuse: Langfuse | None = None


@app.on_event("startup")
async def warn_legacy_endpoints() -> None:
    if settings.legacy_endpoints_enabled:
        logger.warning(
            "Legacy per-use-case endpoints are ACTIVE. "
            "These will be removed after Phase 13 cutover is confirmed. "
            "Set LEGACY_ENDPOINTS_ENABLED=false to disable."
        )


@app.on_event("startup")
async def startup() -> None:
    global _redis, _redis_saver, _sqlite_saver, _langfuse

    _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    _redis_saver = RedisSaver(_redis)

    _sqlite_saver = SqliteSaver()
    await _sqlite_saver.init()

    _langfuse = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    logger.info("agent-api started", extra={"redis_url": settings.redis_url, "openemr": settings.openemr_base_url})


@app.on_event("shutdown")
async def shutdown() -> None:
    if _redis:
        await _redis.aclose()
    if _langfuse:
        _langfuse.flush()
    await audit_writer.close_pool()


# ── PHI audit destruction-record API ─────────────────────────────────────────
#
# Compliance-driven destruction of audit / PHI records is performed out of
# band by DB tooling (DROP PARTITION, manual purge scripts, etc).  This
# endpoint NEVER deletes anything itself — it records that destruction
# happened in the immutable ``copilot_audit_destructions`` table so the
# §9.7 retention policy has a tamper-evident receipt trail.

class DestructionRecordRequest(BaseModel):
    target_session: str | None = None
    target_patient: str | None = None
    target_window_start: Any | None = None  # datetime (ISO-8601) or None
    target_window_end: Any | None = None
    rows_affected: int = 0
    reason: str


class DestructionRecordResponse(BaseModel):
    destruction_id: int
    ts: str


@app.post("/audit/destruction-record", response_model=DestructionRecordResponse)
async def post_destruction_record(body: DestructionRecordRequest) -> DestructionRecordResponse:
    """Record (don't perform) a compliance-driven PHI destruction.

    Auth required (enforced upstream by ``jwt_middleware``).  ``requested_by``
    is taken from the verified JWT's ``sub`` — the body cannot override it.
    """
    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason must not be empty")

    principal = request_principal_var.get()
    if principal is None:
        # In bypass mode (empty JWT secret) we still need a stable
        # ``requested_by`` value so the receipt is queryable.  Use a
        # sentinel rather than NULL so consumers can grep for it.
        requested_by = "unauthenticated"
    else:
        requested_by = str(principal.get("provider_id") or "unknown")

    # Parse optional ISO-8601 datetimes for the destruction window.
    import datetime as _dt
    def _parse(v: Any) -> _dt.datetime | None:
        if v is None or v == "":
            return None
        if isinstance(v, _dt.datetime):
            return v
        try:
            return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid datetime") from exc

    rid = request_id_var.get()
    try:
        destruction_id, ts = await audit_writer.record_destruction(
            requested_by=requested_by,
            request_id=rid,
            target_session=body.target_session,
            target_patient=body.target_patient,
            target_window_start=_parse(body.target_window_start),
            target_window_end=_parse(body.target_window_end),
            rows_affected=int(body.rows_affected),
            reason=body.reason.strip(),
        )
    except RuntimeError as exc:
        # Surfaces "audit_db_url not configured" or pool-create failure
        # so operators see why the receipt could not be written.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("destruction_record_failed", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail="destruction record write failed") from exc

    # Best-effort: also emit a row to copilot_audit_events so the
    # destruction event itself is queryable in the same timeline.
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type="destruction",
                request_id=rid,
                provider_id=requested_by,
                session_id=body.target_session,
                patient_id=body.target_patient,
                outcome="success",
                detail_json={
                    "destruction_id": destruction_id,
                    "rows_affected": int(body.rows_affected),
                },
            )
        )
    except Exception:
        pass

    return DestructionRecordResponse(destruction_id=destruction_id, ts=ts.isoformat())


def _session_ctx(session_id: str | None = None) -> dict:
    return {
        "redis_client": _redis,
        "redis_saver": _redis_saver,
        "sqlite_saver": _sqlite_saver,
        "langfuse": _langfuse,
        "session_id": session_id,
    }


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    redis_ok = False
    if _redis:
        try:
            await _redis.ping()
            redis_ok = True
        except Exception:
            pass
    return {"status": "ok", "redis": redis_ok}


# ── FHIR validation proxy ─────────────────────────────────────────────────────

@app.get("/fhir/patient/{patient_id}")
async def get_patient(patient_id: str) -> dict:
    try:
        return await fhir_client.get_patient(patient_id)
    except Exception as exc:
        logger.error("FHIR patient fetch failed patient_id=%s error=%s", patient_id, exc)
        raise HTTPException(status_code=502, detail="FHIR upstream error") from exc


# ── Diagnostic FHIR probe (gated by COPILOT_DIAG env) ────────────────────────

@app.get("/diag/fhir")
async def diag_fhir() -> dict:
    """One-shot FHIR connectivity probe. Gated by COPILOT_DIAG env flag.

    Forces a fresh token, performs GET /Patient?identifier=1, and returns
    the resolved URL, status, body snippet, and token metadata. Never logs
    or returns the bearer token itself.
    """
    if not settings.copilot_diag:
        raise HTTPException(status_code=404, detail="Not found")

    base_url = settings.openemr_base_url.rstrip("/") + "/apis/default/fhir"
    target_url = f"{base_url}/Patient"
    params = {"identifier": "1"}

    # Force a fresh token so a stale cached one cannot mask the symptom.
    invalidate_token_cache()
    try:
        token = await get_access_token(force_refresh=True)
    except Exception as exc:
        return {
            "stage": "token",
            "ok": False,
            "error": str(exc),
            "token_url": settings.resolved_fhir_token_url,
        }

    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(
                target_url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/fhir+json",
                },
            )
        except Exception as exc:
            return {
                "stage": "fhir_get",
                "ok": False,
                "url": target_url,
                "params": params,
                "error": str(exc),
            }

    body_preview = resp.text[:500]
    parsed_total = None
    try:
        parsed = resp.json()
        parsed_total = parsed.get("total")
    except Exception:
        pass

    return {
        "stage": "fhir_get",
        "ok": resp.status_code == 200,
        "url": str(resp.request.url),
        "request_url_built": target_url,
        "params": params,
        "status": resp.status_code,
        "body_preview": body_preview,
        "total": parsed_total,
        "token_len": len(token),
        "token_url": settings.resolved_fhir_token_url,
    }


# ── UC-1 Triage Census ──────────────────────────────────────────────────────── LEGACY — retire after Phase 13 cutover

class CensusRequest(_CoerceModel):
    patient_ids: list[str]
    session_id: str | None = None
    provider_id: str | None = None
    # When the user clicks Refresh in the census header we set
    # force_refresh=True so the census tool bypasses the Redis cache and
    # produces a new generated_at timestamp. Default False keeps non-forced
    # reads on the warm path (the dispatcher's fast path also benefits).
    force_refresh: bool = False


@app.post("/triage/census")
async def triage_census(body: CensusRequest) -> dict:
    if not body.patient_ids:
        raise HTTPException(status_code=400, detail="patient_ids must not be empty")
    try:
        tool_result = await get_census_summary(
            {
                "provider_id": body.provider_id or "system",
                "patient_ids": body.patient_ids,
                "force_refresh": body.force_refresh,
            },
            session_context=_session_ctx(body.session_id),
        )
        result = tool_result["result"]
        # Emit Prometheus metrics per entry
        for entry in result.get("census", []):
            TRIAGE_LEVEL_COUNTER.labels(level=str(entry.get("triage_level", "?"))).inc()
        return result
    except Exception as exc:
        logger.error("Triage census failed error=%s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Triage census failed") from exc


# ── UC-2 Pre-Encounter Briefing ───────────────────────────────────────────────  LEGACY — retire after Phase 13 cutover

class BriefingRequest(BaseModel):
    # Optional body — old clients may POST {} or no body. When the user clicks
    # the in-UI Refresh button we set force_refresh=True so the briefing tool
    # bypasses both Redis caches (briefing + bundle) and produces a new
    # generated_at timestamp. Default False keeps non-forced reads on the warm
    # path.
    force_refresh: bool = False
    # When provided, persist a synthetic conversation turn so subsequent
    # dispatcher /agent/query calls see this brief in their loaded history
    # and can resolve pronouns ("can she have tylenol?") to this patient.
    # Without this, button-driven actions are invisible to the conversation
    # memory used by the LLM.
    session_id: str | None = None


def _briefing_button_placeholder(patient_id: str, briefing: dict[str, Any]) -> str:
    """Mirror dispatcher's _briefing_identity_summary for button-driven briefs."""
    name = briefing.get("name") or briefing.get("patient_name") or patient_id
    alerts = briefing.get("alerts") or []
    sections = briefing.get("sections") or {}
    meds = []
    if isinstance(sections, dict):
        for sec in sections.values():
            if isinstance(sec, list):
                for item in sec:
                    if isinstance(item, dict) and "medication" in str(item).lower():
                        meds.append(item)
    base = f"Briefing generated for {name} (patient_id={patient_id})."
    fact_parts: list[str] = []
    if alerts:
        fact_parts.append(f"{len(alerts)} active alerts")
    if fact_parts:
        return base + " " + ", ".join(fact_parts) + "."
    return base


def _meds_button_placeholder(patient_id: str, data: dict[str, Any]) -> str:
    """Identity line for a button-driven medication safety check."""
    name = data.get("name") or data.get("patient_name") or patient_id
    allergies = data.get("allergies") or []
    current_meds = data.get("current_medications") or data.get("medications") or []
    interactions = data.get("interactions") or []
    base = f"Medication safety check for {name} (patient_id={patient_id})."
    parts: list[str] = []
    if isinstance(allergies, list):
        parts.append(f"{len(allergies)} allergies")
    if isinstance(current_meds, list):
        parts.append(f"{len(current_meds)} current medications")
    if isinstance(interactions, list) and interactions:
        parts.append(f"{len(interactions)} interactions of concern")
    if parts:
        return base + " " + ", ".join(parts) + "."
    return base


async def _persist_button_action(
    session_id: str,
    user_message: str,
    placeholder_text: str,
) -> None:
    """Save a synthetic user/assistant turn pair so the conversation history
    used by the dispatcher reflects the button-driven action.

    Failures are logged and swallowed — the action's response to the user
    must not depend on persistence succeeding.
    """
    if not session_id:
        return
    try:
        ctx = _session_ctx(session_id)
        # Imported here (not top-level) to avoid circular import in the
        # legacy LEGACY-marked surface that already imports dispatcher.
        from agent.dispatcher import _save_turn
        await _save_turn(session_id, ctx, "user", user_message)
        await _save_turn(
            session_id,
            ctx,
            "assistant",
            [{"type": "text", "text": placeholder_text}],
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Button-action persistence failed", extra={"session_id": session_id, "error": str(exc)})


@app.post("/briefing/{patient_id}", response_model=BriefingResponse)
async def briefing(patient_id: str, body: BriefingRequest | None = None) -> BriefingResponse:
    _t0 = time.perf_counter()
    force_refresh = bool(body.force_refresh) if body is not None else False
    session_id = body.session_id if body is not None else None
    try:
        tool_result = await get_patient_briefing(
            {"patient_id": patient_id, "provider_id": "system", "force_refresh": force_refresh},
            session_context=_session_ctx(session_id),
        )
        BRIEFING_DURATION.observe(time.perf_counter() - _t0)
        result = tool_result["result"]
        if session_id:
            name = result.get("name") or patient_id
            await _persist_button_action(
                session_id,
                f"Brief {name}.",
                _briefing_button_placeholder(patient_id, result),
            )
        return BriefingResponse.model_validate(result)
    except Exception as exc:
        logger.error("Briefing failed patient_id=%s error=%s", patient_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Briefing generation failed") from exc


# ── UC-3 Targeted Record Query ────────────────────────────────────────────────  LEGACY — retire after Phase 13 cutover

class QueryRequest(BaseModel):
    patient_id: str
    query: str


@app.post("/session/{session_id}/query")
async def targeted_query(session_id: str, body: QueryRequest) -> dict:
    if _redis_saver is None or _sqlite_saver is None:
        raise HTTPException(status_code=503, detail="Checkpointers not ready")
    try:
        tool_result = await query_patient_records(
            {"patient_id": body.patient_id, "query": body.query, "provider_id": "system"},
            session_context=_session_ctx(session_id),
        )
        return tool_result["result"]
    except Exception as exc:
        logger.error("UC-3 query failed", extra={"session_id": session_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail="Query failed") from exc


# ── UC-4 Medication Safety ────────────────────────────────────────────────────  LEGACY — retire after Phase 13 cutover

@app.get("/medication/safety/{patient_id}")
async def medication_safety(
    patient_id: str,
    session_id: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """Button-driven medication safety surface.

    Optional ?session_id= query param: when provided, the result is persisted
    as a synthetic conversation turn so subsequent dispatcher calls see this
    in their loaded history (allows pronoun resolution after a button click).

    Optional ?force_refresh=true query param: when true, bypasses the bundle
    cache so the safety report reflects current FHIR state and the response
    carries a fresh ``generated_at`` timestamp. Wired to the in-UI Refresh
    button on the medication-safety bubble (mirrors the briefing endpoint).
    """
    try:
        tool_result = await get_medication_safety(
            {
                "patient_id": patient_id,
                "provider_id": "system",
                "force_refresh": force_refresh,
            },
            session_context=_session_ctx(session_id),
        )
        result = tool_result["result"]
        if session_id:
            name = result.get("name") or patient_id
            await _persist_button_action(
                session_id,
                f"Medication safety for {name}.",
                _meds_button_placeholder(patient_id, result),
            )
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail="FHIR upstream error") from exc


# ── UC-5 Parallel Handoff ─────────────────────────────────────────────────────  LEGACY — retire after Phase 13 cutover

class HandoffRequest(BaseModel):
    patient_ids: list[str]
    shift_end_time: str | None = None


@app.post("/handoff/generate")
async def handoff_generate(body: HandoffRequest) -> dict:
    if not body.patient_ids:
        raise HTTPException(status_code=400, detail="patient_ids must not be empty")
    try:
        tool_result = await generate_handoff(
            {"provider_id": "system", "patient_ids": body.patient_ids},
            session_context=_session_ctx(),
        )
        return tool_result["result"]
    except Exception as exc:
        logger.error("Handoff generation failed", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail="Handoff generation failed") from exc


# ── UC-5 Streaming Handoff (per-patient SSE) ─────────────────────────────────
#
# Returns Server-Sent Events; each per-patient ``_generate_one`` completion
# emits one ``handoff_chunk`` event so the UI can render incrementally
# instead of blocking ~38 s for the full census.

def _sse_format(event: str, data: dict[str, Any]) -> str:
    import json as _json
    return f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"


@app.post("/handoff/generate/stream")
async def handoff_generate_stream(body: HandoffRequest) -> StreamingResponse:
    if not body.patient_ids:
        raise HTTPException(status_code=400, detail="patient_ids must not be empty")

    from handoff.generator import HandoffSummary, generate_handoffs

    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
    started_at = time.monotonic()
    counts = {"succeeded": 0, "failed": 0}

    async def _on_complete(summary: HandoffSummary) -> None:
        summary_dict = asdict(summary)
        if summary.error:
            counts["failed"] += 1
            await queue.put((
                "error",
                {
                    "patient_id": summary.patient_id,
                    "error_class": "HandoffError",
                    "message": summary.error,
                    "summary": summary_dict,
                },
            ))
        else:
            counts["succeeded"] += 1
            await queue.put((
                "handoff_chunk",
                {"patient_id": summary.patient_id, "summary": summary_dict},
            ))

    gather_task = asyncio.create_task(
        generate_handoffs(
            patient_ids=body.patient_ids,
            langfuse=_langfuse,
            redis_client=_redis,
            on_patient_complete=_on_complete,
        ),
    )

    async def _sentinel_when_done() -> None:
        try:
            await gather_task
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Streaming handoff gather failed",
                extra={"error": str(exc)},
            )
        finally:
            await queue.put(None)

    sentinel_task = asyncio.create_task(_sentinel_when_done())

    async def _event_stream() -> Any:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    duration_ms = int((time.monotonic() - started_at) * 1000)
                    yield _sse_format(
                        "done",
                        {
                            "total": len(body.patient_ids),
                            "succeeded": counts["succeeded"],
                            "failed": counts["failed"],
                            "duration_ms": duration_ms,
                        },
                    )
                    return
                event_name, payload = item
                yield _sse_format(event_name, payload)
        finally:
            if not gather_task.done():
                gather_task.cancel()
            if not sentinel_task.done():
                sentinel_task.cancel()

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Triage rationale (direct-call, not dispatcher) ───────────────────────────

class TriageRationaleRequest(BaseModel):
    patient_id: str | None = None
    session_id: str | None = None
    provider_id: str = "system"


@app.post("/agent/triage_rationale/{patient_id}")
async def triage_rationale(patient_id: str, body: TriageRationaleRequest) -> dict:
    """Click-to-expand triage rationale — direct call, not dispatched."""
    try:
        tool_result = await get_triage_rationale(
            {"patient_id": patient_id, "provider_id": body.provider_id},
            session_context=_session_ctx(body.session_id),
        )
        return tool_result["result"]
    except Exception as exc:
        logger.error("Triage rationale failed", extra={"patient_id": patient_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail="Triage rationale failed") from exc


# ── Dispatcher endpoint (POST /agent/query) ───────────────────────────────────

class AgentQueryRequest(_CoerceModel):
    message: str
    session_id: str
    provider_id: str
    patient_ids: list[str] = []
    provider_name: str = "Provider"
    census_context: str | None = None


@app.post("/agent/query")
async def agent_query(request: AgentQueryRequest) -> dict:
    """Dispatcher — routes all physician queries through the tool_use loop."""
    session_context = {
        **_session_ctx(request.session_id),
        "provider_id": request.provider_id,
        "patient_ids": request.patient_ids,
        "provider_name": request.provider_name,
    }
    if request.census_context:
        session_context["census_context"] = request.census_context
    try:
        return await dispatch(request.message, request.session_id, session_context)
    except Exception as exc:
        logger.error("Dispatcher error session_id=%s error=%s", request.session_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Dispatcher error") from exc


# ── FHIR pre-fetch (fired by React panel on mount) ───────────────────────────

class PrefetchRequest(_CoerceModel):
    session_id: str
    provider_id: str
    patient_ids: list[str] = []
    # When True, the warm path bypasses every per-layer EXISTS check so
    # census, bundles, briefings, and medication-safety are all regenerated
    # against current FHIR state. Gated server-side by
    # ``settings.prefetch_force_refresh_on_login`` so a hostile or buggy
    # client cannot cost-bomb Anthropic by toggling this flag at will.
    force_refresh: bool = False


@app.get("/agent/prefetch/status")
async def agent_prefetch_status(session_id: str) -> dict:
    """Return per-patient warm status for the UI's ⚡/⏳ pills.

    Best-effort read from the Redis hash populated by ``_warm_one``.
    Empty result (no key, or Redis unavailable) means "no warm in
    progress for this session" — the UI treats every patient as
    "warmed" in that case so it doesn't get stuck showing pending
    pills forever.
    """
    if _redis is None:
        return {"session_id": session_id, "patients": {}}
    try:
        raw = await _redis.hgetall(f"copilot:warm-status:{session_id}")
    except Exception as exc:
        logger.warning(
            "warm_status_read_failed",
            extra={"session_id": session_id, "error": str(exc)},
        )
        return {"session_id": session_id, "patients": {}}
    return {"session_id": session_id, "patients": raw or {}}


@app.post("/agent/prefetch")
async def agent_prefetch(request: PrefetchRequest) -> dict:
    """Signal that the React panel has mounted and FHIR pre-fetch should begin.

    V1: acknowledges immediately; the actual cache warming is handled by the
    existing FHIR client on the first get_census_summary tool call.  This
    endpoint exists so the UI can fire a non-blocking fetch on mount without
    waiting for it — keeping the session-open flow in UX_SPEC §3 intact.
    """
    # Honour the server-side gate: clients can request force_refresh, but the
    # operator decides whether the deployment is willing to pay for it on
    # every login. See PREFETCH_FORCE_REFRESH_ON_LOGIN env var / config.py.
    effective_force_refresh: bool = bool(
        request.force_refresh and settings.prefetch_force_refresh_on_login
    )
    logger.info(
        "Pre-fetch signal received",
        extra={
            "session_id": request.session_id,
            "patient_count": len(request.patient_ids),
            "force_refresh_requested": request.force_refresh,
            "force_refresh_effective": effective_force_refresh,
        },
    )

    # Server-side dedupe: when the iframe + landing-page script both fire
    # /agent/prefetch within the same login flow (observed 2-3× per
    # login), each pass overwrites the warm-status hash and races on
    # bundle writes — that race was producing fingerprint mismatches
    # that invalidated the cached briefing on the first Brief click.
    # Hold a 30s in-flight lock per session so only the first POST runs
    # the warm; subsequent POSTs return immediately.
    if _redis is not None and request.session_id:
        in_flight_key = f"copilot:warm-inflight:{request.session_id}"
        try:
            acquired = await _redis.set(in_flight_key, "1", nx=True, ex=60)
            if not acquired:
                logger.info(
                    "Pre-fetch deduped — warm already in flight",
                    extra={"session_id": request.session_id},
                )
                return {"status": "deduped"}
        except Exception as exc:
            logger.warning(
                "Pre-fetch dedupe lock failed, proceeding anyway",
                extra={"session_id": request.session_id, "error": str(exc)},
            )

    # Eagerly seed "pending" status for every requested patient BEFORE the
    # background warm task even starts census-build. Without this the UI's
    # first poll (~1-2s in) sees an empty hash and renders no pills until
    # census finishes ~3-5s later — looks like the warm never started.
    if _redis is not None and request.patient_ids:
        warm_status_key_eager = f"copilot:warm-status:{request.session_id}"
        try:
            mapping = {pid: "pending" for pid in request.patient_ids}
            await _redis.hset(warm_status_key_eager, mapping=mapping)
            await _redis.expire(warm_status_key_eager, 300)
        except Exception as exc:
            logger.warning(
                "warm_status_eager_seed_failed",
                extra={"session_id": request.session_id, "error": str(exc)},
            )

    async def _warm() -> None:
        t_start = time.monotonic()
        logger.info(
            "Pre-fetch warm started",
            extra={
                "session_id": request.session_id,
                "patient_count": len(request.patient_ids),
                "force_refresh": effective_force_refresh,
            },
        )
        bulk_query_failed: bool = False
        entries: list[Any] = []
        try:
            census_result = await build_census(
                request.patient_ids,
                redis_client=_redis,
                cache_key=census_cache_key(request.provider_id, request.patient_ids),
                provider_id=request.provider_id,
                force_refresh=effective_force_refresh,
            )
            entries = list(census_result.verified)
        except Exception as exc:
            # Census build can fail when the FHIR bulk Patient query
            # (Patient?_count=200&_sort=_id) returns 500 — common on hosts
            # where OpenEMR's FHIR search is partially broken. We still
            # have the patient_ids the client passed in, so fall back to
            # per-patient bundle/briefing warming rather than bailing
            # entirely (which would leave every briefing cache cold).
            duration_ms = int((time.monotonic() - t_start) * 1000)
            if not request.patient_ids:
                # Nothing to fall back to — preserve the original failure path.
                agent_prewarm_duration_seconds.labels(outcome="failed").observe(time.monotonic() - t_start)
                agent_prewarm_runs_total.labels(outcome="failed").inc()
                logger.warning(
                    "Pre-fetch cache warming failed",
                    extra={"session_id": request.session_id, "error": str(exc), "duration_ms": duration_ms},
                )
                return
            bulk_query_failed = True
            logger.warning(
                "Pre-fetch cache warming failed",
                extra={"session_id": request.session_id, "error": str(exc), "duration_ms": duration_ms},
            )
            logger.info(
                "prefetch_bulk_query_failed_falling_back",
                extra={
                    "session_id": request.session_id,
                    "error": str(exc),
                    "patient_count": len(request.patient_ids),
                    "request_id": request_id_var.get(),
                },
            )

        # After census builds (or the bulk-Patient query falls back), fan
        # out bundle + briefing + medication-safety warmers, bounded by a
        # small semaphore so we don't hammer FHIR / Anthropic. Each warmer
        # EXISTS-checks before writing so re-mounts are cheap; when
        # effective_force_refresh is True the EXISTS check is skipped and
        # all three layers cascade-refresh against current FHIR state.
        # Strict batches of 6 (top-of-triage first). A semaphore alone would
        # let lower-priority patients enter as soon as ANY higher-priority
        # one finished — so the dots could light green out of triage order.
        # Batch barriers guarantee: top 6 ALL go green before any of the
        # bottom 4 starts warming. Wall-time same as semaphore=6 (~28s)
        # because the slowest patient in each batch dominates either way.
        BATCH_SIZE = 6

        # Track per-patient warm status in Redis under a session hash so
        # the UI can show ⚡/⏳ pills next to each census row. Best-effort:
        # any Redis failure here is logged but never blocks the warm.
        warm_status_key = f"copilot:warm-status:{request.session_id}"

        async def _record_warm_status(pid: str, status: str) -> None:
            if _redis is None:
                return
            try:
                await _redis.hset(warm_status_key, pid, status)
                # 5-min TTL — long enough to span the warm window plus a
                # few clicks, short enough to self-clean if the user
                # navigates away.
                await _redis.expire(warm_status_key, 300)
            except Exception as exc:
                logger.warning(
                    "warm_status_record_failed",
                    extra={"patient_id": pid, "status": status, "error": str(exc)},
                )

        async def _warm_one(pid: str, triage_rank: int | None, warmup_order: int) -> None:
            await _record_warm_status(pid, "warming")
            if True:
                t_warm_start = time.monotonic()
                # Bundle first — both downstream warmers read it from Redis.
                await warm_bundle_for_patient(
                    _redis, pid, force_refresh=effective_force_refresh
                )
                # Briefing and medication safety can run concurrently once
                # the bundle is in Redis: neither writes the bundle key.
                results = await asyncio.gather(
                    warm_briefing_for_patient(
                        _redis, pid, langfuse=_langfuse,
                        force_refresh=effective_force_refresh,
                    ),
                    warm_medication_safety_for_patient(
                        _redis, pid, langfuse=_langfuse,
                        force_refresh=effective_force_refresh,
                    ),
                    return_exceptions=True,
                )
                final_status = "warmed" if not any(isinstance(r, Exception) for r in results) else "failed"
                await _record_warm_status(pid, final_status)
                # Per-patient observability: one structured event per warmup so
                # we can verify ordering (highest-priority patient warms first)
                # and per-patient latency from the log stream alone. Pairs with
                # the aggregate agent_prewarm_duration_seconds histogram.
                logger.info(
                    "Pre-fetch patient warmed",
                    extra={
                        "patient_id": pid,
                        "triage_rank": triage_rank,
                        "warmup_order": warmup_order,
                        "duration_ms": int((time.monotonic() - t_warm_start) * 1000),
                        "cache": "miss" if effective_force_refresh else "n/a",
                    },
                )

        # Order the fan-out by triage rank so the patient most likely to be
        # clicked first (level 1 = most urgent) warms first. Within a bounded
        # semaphore the iteration order determines who gets a worker slot
        # first, which is exactly the latency the user perceives on the
        # initial click. Census entries are already sorted by
        # (triage_level, name) inside build_census; we make the contract
        # explicit here so a future re-shuffle of build_census's internal
        # sort cannot silently regress the warm-order guarantee.
        ranked_fanout: list[tuple[str, int | None]]
        if bulk_query_failed:
            # When census-build failed, fall back to the patient_ids the
            # client passed in. We have no triage rank for them — use None
            # and preserve client-supplied order.
            ranked_fanout = [(pid, None) for pid in request.patient_ids]
        else:
            # Tolerate entries that lack triage_level (legacy/test stubs);
            # those sort last and carry None as the rank in the warmup log.
            ranked_fanout = sorted(
                (
                    (entry.patient_id, getattr(entry, "triage_level", None))
                    for entry in entries
                ),
                key=lambda pair: (pair[1] is None, pair[1] if pair[1] is not None else 0),
            )

        # Seed "pending" for every patient BEFORE create_task so the UI's
        # status poll can show queued pills the moment it fires (otherwise
        # the first poll sees an empty hash and renders no pills until
        # the first task gets a worker slot, ~hundreds of ms later).
        for pid, _ in ranked_fanout:
            await _record_warm_status(pid, "pending")

        # Strict triage-ordered batches. Each batch waits for ALL members
        # to settle before the next batch enters — so the green dots cascade
        # top-to-bottom on the census view instead of finishing in random
        # FHIR-latency order.
        results: list[Any] = []
        total = len(ranked_fanout)
        for batch_start in range(0, total, BATCH_SIZE):
            batch = ranked_fanout[batch_start : batch_start + BATCH_SIZE]
            batch_tasks = [
                asyncio.create_task(_warm_one(pid, rank, batch_start + offset))
                for offset, (pid, rank) in enumerate(batch)
            ]
            if batch_tasks:
                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                results.extend(batch_results)

        failures = sum(1 for r in results if isinstance(r, Exception))
        if bulk_query_failed:
            # Distinguish the fallback path so dashboards can detect bulk-query
            # outages even when the per-patient warmers all succeeded.
            outcome = "bulk_query_failed_fallback"
        else:
            outcome = "success" if failures == 0 else ("partial" if failures < len(results) else "failed")
        duration_s = time.monotonic() - t_start
        duration_ms = int(duration_s * 1000)
        agent_prewarm_duration_seconds.labels(outcome=outcome).observe(duration_s)
        agent_prewarm_runs_total.labels(outcome=outcome).inc()
        logger.info(
            "Pre-fetch warm completed",
            extra={
                "session_id": request.session_id,
                "duration_ms": duration_ms,
                "outcome": outcome,
                "census_entries": len(entries),
                "bundle_warmed": len(warm_tasks),
                "briefing_warmed": len(warm_tasks),
                "medication_safety_warmed": len(warm_tasks),
                "warm_failures": failures,
                "force_refresh": effective_force_refresh,
                "caches_populated": [
                    "census", "bundle", "briefing", "medication_safety",
                ],
            },
        )

    asyncio.create_task(_warm())
    return {"status": "acknowledged", "session_id": request.session_id}


# ── Client-side timing receiver ──────────────────────────────────────────────

class ClientTimingRequest(BaseModel):
    action: str
    duration_ms: int
    request_id: str | None = None
    session_id: str | None = None
    t_navigation_start_ms: int | None = None
    extra: dict[str, Any] | None = None


@app.post("/agent/client-timing", status_code=204)
async def client_timing(body: ClientTimingRequest) -> Response:
    """Receive a browser-reported timing sample. Logs + emits a histogram observation."""
    duration_s = max(body.duration_ms, 0) / 1000.0
    agent_client_timing_seconds.labels(action=body.action).observe(duration_s)
    logger.info(
        "client_timing",
        extra={
            "action": body.action,
            "duration_ms": body.duration_ms,
            "client_request_id": body.request_id,
            "session_id": body.session_id,
            "t_navigation_start_ms": body.t_navigation_start_ms,
            "client_extra": body.extra,
        },
    )
    return Response(status_code=204)


# ── Raw conversation turns ────────────────────────────────────────────────────

class MessageRequest(BaseModel):
    role: str = "user"
    content: str


@app.post("/session/{session_id}/message")
async def post_message(session_id: str, body: MessageRequest) -> dict:
    if _redis_saver is None or _sqlite_saver is None:
        raise HTTPException(status_code=503, detail="Checkpointers not ready")

    try:
        turn_index = await _redis_saver.append(session_id, role=body.role, content=body.content)
    except Exception as exc:
        logger.warning("Redis unavailable, falling back to SQLite", extra={"error": str(exc)})
        turn_index = await _sqlite_saver.append(session_id, role=body.role, content=body.content)

    return {"session_id": session_id, "turn_index": turn_index, "status": "queued"}


@app.get("/session/{session_id}/history")
async def get_history(session_id: str) -> dict:
    if _redis_saver is None:
        raise HTTPException(status_code=503, detail="Checkpointers not ready")

    try:
        turns = await _redis_saver.load(session_id)
    except Exception:
        turns = await _sqlite_saver.load(session_id) if _sqlite_saver else []

    return {"session_id": session_id, "turns": turns}


# ── Document ingest (W2 Slices 1.6 + 1.7) ────────────────────────────────────
#
# POST /document/ingest — Path B inline ingestion (W2_ARCHITECTURE.md §4.2).
# Wires together fhir_writer (Binary + DocumentReference), documents.store
# (idempotent claim by content hash), and the lab extractor.  JWT-protected
# via the global middleware: this endpoint is NOT on the bypass list.
#
# Soft-warns (Slice 1.7) are computed inline by ``_build_soft_warns`` and
# returned alongside the extraction so the UI can surface confidence
# caveats without re-deriving them.

# Inline upload size + page guards (W2 §4.2 / §4.5). Beyond these the user
# is routed to Path A (overnight Documents-tab processing). The constants
# are module-level so tests can monkeypatch them down to a small value
# without crafting a 25 MB payload.
_DOC_INGEST_MAX_BYTES: int = 25 * 1024 * 1024
_DOC_INGEST_HARD_READ_CAP: int = 26 * 1024 * 1024
_DOC_INGEST_MAX_PAGES: int = 50
_DOC_INGEST_TOO_LARGE_MSG: str = (
    "Document too large for inline upload — attach via OpenEMR Documents tab "
    "so it processes overnight, or split into a smaller upload."
)


def _flatten_citations(extraction: Any) -> list[dict[str, Any]]:
    """Walk an ExtractionResult and collect every ``Citation`` it carries.

    Lab values and key-facts each own a ``citations`` list; the UI wants a
    single flat list per response so the badge renderer doesn't have to
    discriminate on ``kind``.
    """
    out: list[dict[str, Any]] = []
    kind = getattr(extraction, "kind", None)
    if kind == "lab_report":
        for value in getattr(extraction, "values", []) or []:
            for cit in getattr(value, "citations", []) or []:
                out.append(cit.model_dump(mode="json"))
    elif kind == "unknown":
        for fact in getattr(extraction, "key_facts", []) or []:
            for cit in getattr(fact, "citations", []) or []:
                out.append(cit.model_dump(mode="json"))
    return out


def _count_extracted_fields(extraction: Any) -> int:
    """Count of values / key_facts in an ExtractionResult — for audit only.

    NEVER returns the underlying clinical text; only the cardinality so the
    audit row can record "n_fields=12" without leaking a single value.
    """
    kind = getattr(extraction, "kind", None)
    if kind == "lab_report":
        return len(getattr(extraction, "values", []) or [])
    if kind == "unknown":
        return len(getattr(extraction, "key_facts", []) or [])
    return 0


def _build_soft_warns_from_dict(
    *,
    kind: str | None,
    ocr_confidence_range: Any,
    classifier_confidence: Any,
) -> list[dict[str, Any]]:
    """Compute soft-warns from raw values (used by both Pydantic and cached paths)."""
    warns: list[dict[str, Any]] = []
    if ocr_confidence_range is not None:
        try:
            ocr_min = float(ocr_confidence_range[0])
        except (TypeError, ValueError, IndexError):
            ocr_min = 1.0
        if ocr_min < 0.6:
            warns.append({
                "code": "ocr_confidence_low",
                "message": (
                    "Scan quality low — citations are best-effort, "
                    "value-fidelity check disabled. Verify against source."
                ),
                "fields": [],
            })

    if kind == "unknown":
        warns.append({
            "code": "unknown_document_class",
            "message": (
                "We're not sure this is a structured document type — "
                "verify before acting."
            ),
            "fields": [],
        })
    elif kind == "lab_report":
        try:
            confidence = float(classifier_confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence < 0.7:
            warns.append({
                "code": "classifier_low_confidence",
                "message": (
                    "We're not sure this is a lab_report — verify before acting."
                ),
                "fields": [],
            })
    return warns


def _build_soft_warns(extraction: Any) -> list[dict[str, Any]]:
    """Return the Slice 1.7 soft-warn list for an extraction.

    Pure function — no I/O — so the rule set is unit-testable in isolation
    from the FastAPI handler. Codes are stable string IDs the UI keys off.
    """
    return _build_soft_warns_from_dict(
        kind=getattr(extraction, "kind", None),
        ocr_confidence_range=getattr(extraction, "ocr_confidence_range", None),
        classifier_confidence=getattr(extraction, "classifier_confidence", 1.0),
    )


def _flatten_citations_from_dict(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a serialized ExtractionResult dict for embedded citations."""
    out: list[dict[str, Any]] = []
    kind = payload.get("kind")
    if kind == "lab_report":
        for value in payload.get("values") or []:
            for cit in (value or {}).get("citations") or []:
                if isinstance(cit, dict):
                    out.append(cit)
    elif kind == "unknown":
        for fact in payload.get("key_facts") or []:
            for cit in (fact or {}).get("citations") or []:
                if isinstance(cit, dict):
                    out.append(cit)
    return out


@app.post("/document/ingest")
async def document_ingest(
    request: Request,
    file: UploadFile = File(...),
    patient_id: str = Form(...),
    doc_type_hint: str | None = Form(None),
) -> Any:
    """Path B: inline upload of a clinical PDF (W2 §4.2 / §4.5 / §4.7).

    Pipeline: size/page guard → FHIR write → idempotent claim → extract →
    persist → audit. Soft-warns are returned inline (Slice 1.7).
    """
    # Imports are local so the route's module-level surface stays small and
    # so the pytest fixtures that monkeypatch these symbols can target the
    # canonical module path.
    from documents import fhir_writer as _fhir_writer
    from documents import store as _store
    from extractors import lab as _lab

    rid = request_id_var.get()
    try:
        principal = request_principal_var.get()
    except LookupError:
        principal = None
    provider_id = "system"
    if principal is not None:
        provider_id = str(principal.get("provider_id") or principal.get("sub") or "system")

    # 1) Size guard — read with a hard cap above the 25 MB limit so we can
    #    distinguish "exactly at limit" from "well over". The +1 MB allows
    #    PDFs that grow slightly during multipart re-encoding to pass.
    pdf_bytes = await file.read(_DOC_INGEST_HARD_READ_CAP + 1)
    size_bytes = len(pdf_bytes)
    if size_bytes > _DOC_INGEST_MAX_BYTES:
        raise HTTPException(status_code=413, detail=_DOC_INGEST_TOO_LARGE_MSG)

    # 2) Page guard — open via PyMuPDF. A non-PDF body raises here; we
    #    convert to a 400 so the client sees a clear error rather than a 500.
    try:
        import pymupdf as _fitz
        doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        logger.warning(
            "document_ingest_invalid_pdf",
            extra={"request_id": rid, "size_bytes": size_bytes, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail="Invalid PDF upload") from exc

    try:
        page_count = int(doc.page_count)
    finally:
        try:
            doc.close()
        except Exception:
            pass

    if page_count > _DOC_INGEST_MAX_PAGES:
        raise HTTPException(status_code=413, detail=_DOC_INGEST_TOO_LARGE_MSG)

    # 3) FHIR write FIRST so we always have a document_reference_id even
    #    when extraction fails downstream.
    try:
        write_result = await _fhir_writer.write_document(
            patient_id=patient_id,
            pdf_bytes=pdf_bytes,
            doc_type_hint=doc_type_hint,
        )
    except _fhir_writer.FhirWriteError as exc:
        logger.error(
            "document_ingest_fhir_write_failed",
            extra={"request_id": rid, "patient_id": patient_id, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail="Document upload to chart failed") from exc

    # 4) Idempotent claim.
    content_sha256 = _store.compute_sha256(pdf_bytes)
    try:
        claim = await _store.claim_or_get(
            document_reference_id=write_result.document_reference_id,
            content_sha256=content_sha256,
            patient_id=patient_id,
        )
    except Exception as exc:
        logger.error(
            "document_ingest_claim_failed",
            extra={"request_id": rid, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Document claim failed") from exc

    # 4a) Cached payload — re-ingest of an identical PDF. Walk the dict
    #     directly: re-validating through pydantic's strict mode rejects
    #     ISO datetimes / list-from-tuple coercions that ``model_dump`` emits.
    if claim.cached_payload is not None:
        cached = claim.cached_payload
        kind_value = cached.get("kind", "unknown")
        ocr_range_value = cached.get("ocr_confidence_range") or [1.0, 1.0]
        classifier_conf_value = cached.get("classifier_confidence", 1.0)
        soft_warns = _build_soft_warns_from_dict(
            kind=kind_value,
            ocr_confidence_range=ocr_range_value,
            classifier_confidence=classifier_conf_value,
        )
        return {
            "document_reference_id": write_result.document_reference_id,
            "extraction_id": claim.extraction_id,
            "extraction": cached,
            "citations": _flatten_citations_from_dict(cached),
            "soft_warns": soft_warns,
            "metadata": {
                "cached": True,
                "fhir_write_path": write_result.path,
                "request_id": rid,
                "size_bytes": size_bytes,
                "page_count": page_count,
            },
        }

    # 4b) Another worker holds the claim — return 202.
    if not claim.owns_claim:
        return JSONResponse(
            status_code=202,
            content={
                "status": "processing",
                "document_reference_id": write_result.document_reference_id,
                "extraction_id": claim.extraction_id,
            },
        )

    # 5) Run extraction.
    try:
        extraction = await _lab.extract(
            pdf_bytes,
            patient_id=patient_id,
            document_reference_id=write_result.document_reference_id,
        )
    except _lab.ExtractionFailed as exc:
        try:
            await _store.fail(extraction_id=claim.extraction_id, error="extraction failed")
        except Exception as fail_exc:  # pragma: no cover — best-effort
            logger.error(
                "document_ingest_fail_record_failed",
                extra={"request_id": rid, "error": str(fail_exc)},
            )
        logger.error(
            "document_ingest_extraction_failed",
            extra={"request_id": rid, "extraction_id": claim.extraction_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Document extraction failed") from exc

    # 6) Persist.
    try:
        await _store.complete(
            extraction_id=claim.extraction_id,
            kind=extraction.kind,
            payload=extraction.model_dump(mode="json"),
            classifier_confidence=float(extraction.classifier_confidence),
            ocr_confidence_range=tuple(extraction.ocr_confidence_range),  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.error(
            "document_ingest_persist_failed",
            extra={"request_id": rid, "extraction_id": claim.extraction_id, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Document persist failed") from exc

    # 7) Audit — two events. detail_json is structured codes only; never
    #    extraction values, never raw OCR.
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type="document_ingested",
                request_id=rid,
                provider_id=provider_id,
                patient_id=patient_id,
                outcome="success",
                detail_json={
                    "path": write_result.path,
                    "size_bytes": size_bytes,
                    "page_count": page_count,
                },
            )
        )
        await audit_writer.emit(
            AuditEvent(
                event_type="document_extracted",
                request_id=rid,
                provider_id=provider_id,
                patient_id=patient_id,
                outcome="success",
                detail_json={
                    "kind": extraction.kind,
                    "classifier_confidence": float(extraction.classifier_confidence),
                    "ocr_confidence_range": [
                        float(extraction.ocr_confidence_range[0]),
                        float(extraction.ocr_confidence_range[1]),
                    ],
                    "n_fields": _count_extracted_fields(extraction),
                },
            )
        )
    except Exception as exc:  # pragma: no cover — audit must never break the request
        logger.warning(
            "document_ingest_audit_emit_failed",
            extra={"request_id": rid, "error": str(exc)},
        )

    soft_warns = _build_soft_warns(extraction)
    return {
        "document_reference_id": write_result.document_reference_id,
        "extraction_id": claim.extraction_id,
        "extraction": extraction.model_dump(mode="json"),
        "citations": _flatten_citations(extraction),
        "soft_warns": soft_warns,
        "metadata": {
            "cached": False,
            "fhir_write_path": write_result.path,
            "request_id": rid,
            "size_bytes": size_bytes,
            "page_count": page_count,
        },
    }


# ── W2 Dispatch (Slice 3.9) ──────────────────────────────────────────────────
#
# POST /agent/w2/dispatch — single entry point for the LangGraph pipeline
# (W2_ARCHITECTURE §5.1 / §5.9). Accepts an optional file (lab PDF) plus
# optional message; the supervisor routes inside the graph.  Streams SSE
# back; today only emits a single ``done`` frame after ``ainvoke``
# completes (TODO(slice-future): swap to ``astream_events`` for per-node
# progress frames once the LangGraph version is bumped).
#
# JWT-protected via the global middleware (no bypass-list change).

# Per-process file-bytes stash. The key is the inbound request_id so
# concurrent uploads in the same session don't collide. TTL-free because
# each closure pops its key when the graph completes.
# TODO(phase-8): move to a session-keyed Redis blob with a 600s TTL
# (``copilot:w2:filebytes:{session}:{request_id}``) so a multi-replica
# deploy can share state.
_w2_file_bytes_stash: dict[str, bytes] = {}


@app.post("/agent/w2/dispatch")
async def agent_w2_dispatch(
    request: Request,
    file: UploadFile | None = File(None),
    patient_id: str | None = Form(None),
    message: str | None = Form(None),
    session_id: str = Form(...),
    provider_id: str = Form(...),
    doc_type_hint: str | None = Form(None),
) -> StreamingResponse:
    """W2 LangGraph dispatch endpoint.

    Accepts a multipart upload with any of:
      * ``file`` — PDF for the document path.
      * ``message`` — free-text query for the structured / retriever path.
      * ``patient_id`` — required for demographics check on the document path.

    Returns ``text/event-stream``. Currently emits a single ``event: done``
    frame carrying the ``finalized`` envelope; per-node progress frames are
    a future enhancement.
    """
    from graph import compile_graph as _compile_graph
    from graph import make_initial_state as _make_initial_state
    from graph.nodes.finalize import sse_frame as _sse_frame
    from langgraph.checkpoint.memory import MemorySaver

    rid = request_id_var.get() or uuid.uuid4().hex

    file_bytes_ref: str | None = None
    if file is not None:
        # Same size guard as /document/ingest.
        pdf_bytes = await file.read(_DOC_INGEST_HARD_READ_CAP + 1)
        size_bytes = len(pdf_bytes)
        if size_bytes > _DOC_INGEST_MAX_BYTES:
            raise HTTPException(
                status_code=413, detail=_DOC_INGEST_TOO_LARGE_MSG
            )
        file_bytes_ref = f"w2:{session_id}:{rid}"
        _w2_file_bytes_stash[file_bytes_ref] = pdf_bytes

    async def _file_bytes_provider(ref: str) -> bytes:
        return _w2_file_bytes_stash.get(ref, b"")

    async def _fhir_patient_provider(pid: str) -> dict[str, Any]:
        return await fhir_client.get_patient(pid)

    compiled = _compile_graph(
        checkpointer=MemorySaver(),
        file_bytes_provider=_file_bytes_provider,
        fhir_patient_provider=_fhir_patient_provider,
    )

    initial_state = _make_initial_state(
        request_id=rid,
        session_id=session_id,
        provider_id=provider_id,
        patient_id=patient_id,
        message=message,
        file_bytes_ref=file_bytes_ref,
        doc_type_hint=doc_type_hint,
    )
    config = {"configurable": {"thread_id": session_id}}

    async def _event_stream() -> Any:
        try:
            # TODO(slice-future): replace with ``compiled.astream_events(...)``
            # so the UI sees per-node progress frames; today we ship a single
            # ``done`` frame after ``ainvoke`` completes.
            try:
                final = await compiled.ainvoke(initial_state, config=config)
            except Exception as exc:  # noqa: BLE001 — boundary
                logger.error(
                    "w2_dispatch_graph_failed",
                    extra={"request_id": rid, "error_type": type(exc).__name__},
                )
                yield _sse_frame(
                    "error",
                    {
                        "request_id": rid,
                        "error_class": type(exc).__name__,
                        "message": "graph execution failed",
                    },
                )
                return
            yield _sse_frame(
                "done",
                {
                    "request_id": rid,
                    "finalized": final.get("finalized") or {},
                    "extraction": final.get("extraction"),
                    "demographic_check": final.get("demographic_check"),
                    "critic_decision": final.get("critic_decision"),
                    "soft_warns": final.get("soft_warns") or [],
                    "errors": final.get("errors") or [],
                },
            )
        finally:
            if file_bytes_ref is not None:
                _w2_file_bytes_stash.pop(file_bytes_ref, None)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
