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
import re
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
    agent_post_ingest_duration_seconds,
    agent_post_ingest_requests_total,
    agent_prewarm_duration_seconds,
    agent_prewarm_runs_total,
    agent_prompt_cache_hits_total,
    agent_prompt_cache_misses_total,
    agent_quarantine_resolver_decisions_total,
    agent_quarantine_total,
    agent_quarantine_transitions_total,
    agent_resolver_duration_seconds,
    agent_tool_calls_total,
    agent_tool_misroute_total,
    agent_w2_classifier_confidence,
    agent_w2_document_ingest_total,
    agent_w2_extraction_duration_seconds,
    agent_w2_ocr_confidence,
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

# CORS configuration. The actual add_middleware call lives at the BOTTOM of
# the middleware stack registration block so it ends up OUTERMOST in the
# Starlette pipeline (last-added = outermost). That ordering guarantees
# CORSMiddleware sees jwt_middleware's 401 / audit_middleware's 500 / etc.
# on the response path and attaches Access-Control-Allow-Origin even on
# error. Without this, the browser blocks every error response with the
# generic "No 'Access-Control-Allow-Origin' header is present" — and the
# iframe console can't surface the real error.
if settings.openemr_origin:
    _cors_origins = [settings.openemr_origin]
else:
    logger.warning(
        "CORS origin not configured (OPENEMR_ORIGIN empty) — falling back to '*'. "
        "Do not run this in production."
    )
    _cors_origins = ["*"]


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
# order below is from INNER (runs near handler) to OUTER (runs first on
# the request).
#
#   audit_middleware                     — innermost; needs request_id + principal
#   _audit_principal_stash               — copies principal ContextVar → request.state
#   jwt_middleware                       — verifies JWT, sets principal ContextVar
#   RequestIdMiddleware                  — sets request_id ContextVar
#   CORSMiddleware                       — outermost; wraps every response, including
#                                          errors from the auth layer below it, so the
#                                          browser sees Access-Control-Allow-Origin
#                                          on 401/403 too
#
# When a request arrives: CORS → RequestId → JWT → stash → audit → handler.
# CORS being outermost means even short-circuit responses (jwt 401, audit 500)
# get the ACAO header on the way back through the stack.

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

# CORS LAST → outermost in the Starlette pipeline → wraps every response,
# including 401s from jwt_middleware. Browsers see ACAO on error responses
# and can surface the real error to the iframe console.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

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
_langfuse_callback: Any | None = None


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

    # LangChain CallbackHandler — attached to LangGraph invocations so node
    # transitions show up as Langfuse traces. Created once at startup since
    # the handler reads creds from env / Langfuse client and is safe to share
    # across requests (LangChain itself reuses callback handlers per-run).
    # Guard the import: in environments missing langchain (isolated/unit
    # tests on the host), we degrade gracefully — graph still runs, traces
    # just aren't emitted.
    global _langfuse_callback
    _langfuse_callback = None
    try:
        from langfuse.callback import CallbackHandler as _LFCallbackHandler

        _langfuse_callback = _LFCallbackHandler(
            secret_key=settings.langfuse_secret_key,
            public_key=settings.langfuse_public_key,
            host=settings.langfuse_host,
        )
    except Exception as _exc:  # noqa: BLE001 — boundary; missing langchain is OK
        logger.warning(
            "langfuse_callback_init_skipped",
            extra={"error_type": type(_exc).__name__},
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
    """Route physician queries through the LangGraph supervisor pipeline.

    Topology: supervisor -> structured (which wraps the legacy dispatcher)
    -> critic -> finalize. The graph's structured_node calls
    ``agent.dispatcher.dispatch`` internally, so the response contract
    (``{narrative, data, citations, ...}``) is preserved — we surface
    ``final["finalized"]["structured_response"]`` as the response body.

    Payload mapping:
      * ``message``, ``session_id``, ``provider_id`` map 1:1 to graph state.
      * ``patient_ids[0]`` becomes ``state["patient_id"]`` (graph state holds
        a single patient; the structured_node rebuilds session_context from
        graph state).
      * ``provider_name`` and ``census_context`` are NOT plumbed through the
        graph today — these were prompt-enrichment hints for the dispatcher
        and degrade gracefully when absent. If they prove load-bearing,
        extend ``W2State`` and ``_build_session_context`` in a follow-up.
    """
    from graph import compile_graph as _compile_graph
    from graph import make_initial_state as _make_initial_state
    from langgraph.checkpoint.memory import MemorySaver

    rid = request_id_var.get() or uuid.uuid4().hex
    patient_id = request.patient_ids[0] if request.patient_ids else None

    async def _fhir_patient_provider(pid: str) -> dict[str, Any]:
        return await fhir_client.get_patient(pid)

    compiled = _compile_graph(
        checkpointer=MemorySaver(),
        fhir_patient_provider=_fhir_patient_provider,
    )
    initial_state = _make_initial_state(
        request_id=rid,
        session_id=request.session_id,
        provider_id=request.provider_id,
        patient_id=patient_id,
        patient_ids=list(request.patient_ids or []),
        message=request.message,
    )
    config: dict[str, Any] = {"configurable": {"thread_id": request.session_id}}
    if _langfuse_callback is not None:
        config["callbacks"] = [_langfuse_callback]

    try:
        final = await compiled.ainvoke(initial_state, config=config)
    except Exception as exc:
        logger.error(
            "agent_query_graph_failed",
            extra={"session_id": request.session_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Dispatcher error") from exc

    finalized = final.get("finalized") or {}
    structured = finalized.get("structured_response") or final.get(
        "structured_response"
    )
    if isinstance(structured, dict):
        return structured
    # Defensive fallback — graph short-circuited (e.g. empty message routed
    # to finalize). Return a minimally-shaped response so the UI does not
    # crash on a missing ``narrative`` field.
    return {
        "narrative": "",
        "data": None,
        "citations": [],
        "errors": finalized.get("errors") or [],
    }


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
                # Renamed from warm_tasks → ranked_fanout in an earlier
                # refactor; the log keys still describe the per-patient
                # warmups so we count the size of the fanout list.
                "bundle_warmed": len(ranked_fanout),
                "briefing_warmed": len(ranked_fanout),
                "medication_safety_warmed": len(ranked_fanout),
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


def _bucket(confidence: float) -> str:
    """Map a 0..1 confidence float to a low|medium|high bucket label.

    Bucket thresholds match the soft-warn cutoffs in
    :func:`_build_soft_warns` so the metric label aligns with the user-visible
    severity. Used to label ``agent_w2_extraction_duration_seconds``.
    """
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "low"
    if c < 0.6:
        return "low"
    if c < 0.85:
        return "medium"
    return "high"


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
    elif kind == "intake_form":
        # Walk every cite-bearing IntakeForm field. Demographics holds
        # TextField sub-fields; chief_concern is a TextField; meds /
        # allergies / family_history / code_status own their own citations.
        demographics = getattr(extraction, "demographics", None)
        if demographics is not None:
            for attr in ("name", "dob", "sex", "mrn", "address"):
                tf = getattr(demographics, attr, None)
                if tf is not None:
                    for cit in getattr(tf, "citations", []) or []:
                        out.append(cit.model_dump(mode="json"))
        chief = getattr(extraction, "chief_concern", None)
        if chief is not None:
            for cit in getattr(chief, "citations", []) or []:
                out.append(cit.model_dump(mode="json"))
        for collection_attr in (
            "current_medications",
            "allergies",
            "family_history",
        ):
            for item in getattr(extraction, collection_attr, []) or []:
                for cit in getattr(item, "citations", []) or []:
                    out.append(cit.model_dump(mode="json"))
        cs = getattr(extraction, "code_status", None)
        if cs is not None:
            for cit in getattr(cs, "citations", []) or []:
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
    if kind == "intake_form":
        n = 0
        n += len(getattr(extraction, "current_medications", []) or [])
        n += len(getattr(extraction, "allergies", []) or [])
        n += len(getattr(extraction, "family_history", []) or [])
        if getattr(extraction, "chief_concern", None) is not None:
            n += 1
        if getattr(extraction, "code_status", None) is not None:
            n += 1
        return n
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


def _normalize_cached_citation(cit: dict[str, Any]) -> dict[str, Any]:
    """Backfill ``bbox`` / ``page`` keys for cached payloads written before
    the Citation schema gained layout coordinates. Older rows simply lack
    the keys; we surface them as ``None`` so the frontend contract is
    uniform regardless of cache vintage.
    """
    if "bbox" not in cit:
        cit = {**cit, "bbox": None}
    if "page" not in cit:
        cit = {**cit, "page": None}
    return cit


def _flatten_citations_from_dict(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a serialized ExtractionResult dict for embedded citations.

    Mirrors ``_flatten_citations`` but consumes the dict shape persisted in
    the extraction cache. Older cached rows predate the bbox/page columns
    on Citation; missing keys are normalized to ``None`` rather than
    omitted, so the response contract is stable across cache vintages.
    """
    out: list[dict[str, Any]] = []
    kind = payload.get("kind")
    if kind == "lab_report":
        for value in payload.get("values") or []:
            for cit in (value or {}).get("citations") or []:
                if isinstance(cit, dict):
                    out.append(_normalize_cached_citation(cit))
    elif kind == "unknown":
        for fact in payload.get("key_facts") or []:
            for cit in (fact or {}).get("citations") or []:
                if isinstance(cit, dict):
                    out.append(_normalize_cached_citation(cit))
    elif kind == "intake_form":
        demographics = payload.get("demographics") or {}
        if isinstance(demographics, dict):
            for attr in ("name", "dob", "sex", "mrn", "address"):
                tf = demographics.get(attr)
                if isinstance(tf, dict):
                    for cit in tf.get("citations") or []:
                        if isinstance(cit, dict):
                            out.append(_normalize_cached_citation(cit))
        chief = payload.get("chief_concern")
        if isinstance(chief, dict):
            for cit in chief.get("citations") or []:
                if isinstance(cit, dict):
                    out.append(_normalize_cached_citation(cit))
        for collection_attr in ("current_medications", "allergies", "family_history"):
            for item in payload.get(collection_attr) or []:
                if isinstance(item, dict):
                    for cit in item.get("citations") or []:
                        if isinstance(cit, dict):
                            out.append(_normalize_cached_citation(cit))
        cs = payload.get("code_status")
        if isinstance(cs, dict):
            for cit in cs.get("citations") or []:
                if isinstance(cit, dict):
                    out.append(_normalize_cached_citation(cit))
    return out


@app.post("/document/ingest")
async def document_ingest(
    request: Request,
    file: UploadFile = File(...),
    patient_id: str | None = Form(None),
    doc_type_hint: str | None = Form(None),
    format_hint: str | None = Form(None),
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
    from documents.ocr import extract_layout as _extract_layout
    from extractors import intake as _intake
    from extractors import lab as _lab
    from extractors.classifier import classify_keywords as _classify_keywords

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
        agent_w2_document_ingest_total.labels(
            path="unknown", doc_type=(doc_type_hint or "unknown"), outcome="too_large"
        ).inc()
        logger.info(
            "document_ingest_metric",
            extra={
                "request_id": rid,
                "outcome": "too_large",
                "size_bytes": size_bytes,
            },
        )
        raise HTTPException(status_code=413, detail=_DOC_INGEST_TOO_LARGE_MSG)

    # 1a) Phase 9 Slice 9.2 — pre-extraction resolver. When the caller did
    #     not supply a patient_id, dispatch the per-format probe and either
    #     resolve to a chart patient (Pass → fall through with
    #     match_provenance) or write a quarantine row (return 202). The
    #     resolver is a leaf and must not import auth.fhir_client directly
    #     (per .importlinter ``demographics-isolated``); we inject the
    #     fhir search callable here.
    match_provenance: str = "supplied"
    if patient_id is None:
        from demographics import resolver as _resolver
        from demographics import quarantine as _quar

        resolver_format = (format_hint or doc_type_hint or "pdf").lower()
        _t0_resolver = time.perf_counter()
        try:
            outcome = await _resolver.resolve(
                raw=pdf_bytes,
                format_hint=resolver_format,
                fhir_search=fhir_client.search,
            )
        except Exception as exc:
            logger.error(
                "document_ingest_resolver_failed",
                extra={"request_id": rid, "format": resolver_format, "error": str(exc)},
            )
            agent_quarantine_resolver_decisions_total.labels(
                outcome="error", format=resolver_format
            ).inc()
            raise HTTPException(status_code=500, detail="Patient resolver failed") from exc
        finally:
            agent_resolver_duration_seconds.labels(format=resolver_format).observe(
                max(0.0, time.perf_counter() - _t0_resolver)
            )

        if isinstance(outcome, _resolver.Quarantine):
            agent_quarantine_resolver_decisions_total.labels(
                outcome="quarantine", format=resolver_format
            ).inc()
            agent_quarantine_total.labels(reason_code=outcome.reason_code).inc()
            try:
                pool = await audit_writer.get_pool()
            except Exception as exc:
                logger.error(
                    "document_ingest_quarantine_pool_failed",
                    extra={"request_id": rid, "error": str(exc)},
                )
                raise HTTPException(status_code=500, detail="Quarantine store unavailable") from exc

            # Determine panel_id from the principal (per f41827440 precedent —
            # one provider == one panel; production payloads may carry an
            # explicit panel_id alongside provider_id in a future slice).
            panel_id = provider_id
            parsed_hint = (
                outcome.candidate_hints[0]
                if outcome.candidate_hints
                else {"format": resolver_format}
            )
            doc_ref_placeholder = f"unresolved:{rid or 'no-rid'}"
            try:
                row = await _quar.quarantine_document(
                    pool=pool,
                    document_reference_id=doc_ref_placeholder,
                    file_batch_id=None,
                    panel_id=panel_id,
                    parsed_identity=parsed_hint,
                    candidate_matches=outcome.candidate_hints[1:],
                    reason_code=outcome.reason_code,
                )
            except Exception as exc:
                logger.error(
                    "document_ingest_quarantine_write_failed",
                    extra={"request_id": rid, "error": str(exc)},
                )
                raise HTTPException(status_code=500, detail="Quarantine write failed") from exc

            logger.info(
                "document_ingest_quarantined",
                extra={
                    "request_id": rid,
                    "outcome": "quarantined",
                    "reason_code": outcome.reason_code,
                    "format": resolver_format,
                    "quarantine_id": row["quarantine_id"],
                    "panel_id": panel_id,
                },
            )
            try:
                await audit_writer.emit(
                    AuditEvent(
                        event_type="document_quarantined",
                        request_id=rid,
                        provider_id=provider_id,
                        patient_id=None,
                        outcome="blocked",
                        detail_json={
                            "reason_code": outcome.reason_code,
                            "format": resolver_format,
                            "candidates_seen": len(outcome.candidate_hints),
                            "quarantine_id": row["quarantine_id"],
                        },
                    )
                )
            except Exception:  # pragma: no cover — audit must never break the request
                pass

            return JSONResponse(
                status_code=202,
                content={
                    "status": "quarantined",
                    "quarantine_id": row["quarantine_id"],
                    "document_reference_id": doc_ref_placeholder,
                    "reason_code": outcome.reason_code,
                    "hint_summary": parsed_hint,
                    "expires_at": row["expires_at"],
                },
            )

        # Pass — accept the resolved patient_id and continue the existing
        # pipeline. The match_provenance field is echoed back in the
        # response so the iframe can paint a "matched via name+DOB" chip.
        agent_quarantine_resolver_decisions_total.labels(
            outcome="pass", format=resolver_format
        ).inc()
        patient_id = outcome.patient_id
        match_provenance = outcome.source
        logger.info(
            "document_ingest_resolver_pass",
            extra={
                "request_id": rid,
                "format": resolver_format,
                "patient_id": patient_id,
                "match_provenance": match_provenance,
                "candidates_seen": outcome.candidates_seen,
            },
        )
    elif patient_id == "":
        raise HTTPException(status_code=400, detail="patient_id must not be empty")

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
        agent_w2_document_ingest_total.labels(
            path="unknown", doc_type=(doc_type_hint or "unknown"), outcome="too_large"
        ).inc()
        logger.info(
            "document_ingest_metric",
            extra={
                "request_id": rid,
                "outcome": "too_large",
                "page_count": page_count,
            },
        )
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
        agent_w2_document_ingest_total.labels(
            path="fhir", doc_type=(doc_type_hint or "unknown"), outcome="fhir_failed"
        ).inc()
        logger.info(
            "document_ingest_metric",
            extra={"request_id": rid, "outcome": "fhir_failed"},
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
        # Recompute layout on cache hit so the UI can paint bbox overlays
        # without a separate round-trip. extract_layout is deterministic and
        # cheap on already-loaded PDF bytes; we soft-fail to an empty list
        # if the layout pass blows up (the inline `bbox` on each citation
        # remains the primary source of truth).
        try:
            _cached_layout = _extract_layout(pdf_bytes)
            bbox_layout_payload = [b.to_dict() for b in _cached_layout]
        except Exception as exc:  # noqa: BLE001 — soft-fail boundary
            logger.warning(
                "document_ingest_cached_layout_failed",
                extra={"request_id": rid, "error": str(exc)},
            )
            bbox_layout_payload = []
        return {
            "document_reference_id": write_result.document_reference_id,
            "extraction_id": claim.extraction_id,
            "extraction": cached,
            "citations": _flatten_citations_from_dict(cached),
            "bbox_layout": bbox_layout_payload,
            "soft_warns": soft_warns,
            "match_provenance": match_provenance,
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

    # 5) Run extraction. Classifier-first dispatch: intake_form goes to the
    #    intake extractor; everything else (lab_report, unknown, no-verdict)
    #    flows through the lab extractor's existing fallback logic.
    import time as _time
    _extract_t0 = _time.monotonic()
    try:
        _layout_blocks = _extract_layout(pdf_bytes)
        _verdict = _classify_keywords(_layout_blocks) if _layout_blocks else None
        if _verdict is not None and _verdict.kind == "intake_form":
            extraction = await _intake.extract_intake(
                pdf_bytes,
                patient_id=patient_id,
                document_reference_id=write_result.document_reference_id,
            )
        else:
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
        agent_w2_document_ingest_total.labels(
            path=write_result.path,
            doc_type=(doc_type_hint or "unknown"),
            outcome="extraction_failed",
        ).inc()
        logger.info(
            "document_ingest_metric",
            extra={"request_id": rid, "outcome": "extraction_failed"},
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

    # 6a) Phase-2 follow-up: derive one FHIR Observation per extracted
    #     LabValue with derivedFrom -> DocumentReference. OpenEMR's deployed
    #     FHIR layer doesn't implement Observation write, so we route through
    #     the custom oe-module-clinical-copilot endpoint. Failures are
    #     surfaced as soft-warns; they NEVER fail the ingest.
    observation_ids: list[str] = []
    observation_soft_warns: list[dict[str, Any]] = []
    if extraction.kind == "lab_report":
        from observations.writer import (
            deterministic_observation_id as _det_obs_id,
            lookup_loinc as _lookup_loinc,
            write_observation as _write_obs,
        )

        # Pull the trailing integer from doc references shaped like
        # "copilot:117" / "rest:9876" / "doc-test-1" so the deterministic
        # id satisfies the PHP r"^copilot-\d+-..." rule. Fall back to a
        # hash-derived integer when no trailing int is present.
        _doc_ref = write_result.document_reference_id
        _m = re.search(r"(\d+)$", _doc_ref or "")
        _doc_id_int = _m.group(1) if _m else str(abs(hash(_doc_ref)) % (10**9))

        for value in getattr(extraction, "values", []) or []:
            try:
                _code, _ = _lookup_loinc(value.normalized_test_name)
                _obs_id = _det_obs_id(_doc_id_int, _code)
                await _write_obs(
                    document_id=_doc_id_int,
                    patient_id=patient_id,
                    lab_value=value,
                    observation_id=_obs_id,
                )
                observation_ids.append(_obs_id)
            except Exception as obs_exc:  # noqa: BLE001 — soft-fail boundary
                logger.warning(
                    "observation_write_soft_failed",
                    extra={
                        "request_id": rid,
                        "extraction_id": claim.extraction_id,
                        "normalized_test_name": value.normalized_test_name,
                        "error_type": type(obs_exc).__name__,
                    },
                )
                observation_soft_warns.append(
                    {
                        "code": "observation_write_failed",
                        "field": value.normalized_test_name,
                    }
                )

        if observation_ids:
            try:
                await _store.record_observation_ids(
                    extraction_id=claim.extraction_id,
                    ids=observation_ids,
                )
            except Exception as rec_exc:  # pragma: no cover — best-effort
                logger.warning(
                    "observation_record_ids_failed",
                    extra={"request_id": rid, "error": str(rec_exc)},
                )

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

    # ── Metrics — success path. Pair each metric with one structured log
    #    event per CLAUDE.md "Observability" rule. NO clinical values.
    try:
        _classifier_conf = float(extraction.classifier_confidence)
        _bucket_label = _bucket(_classifier_conf)
        _ocr_low = float(extraction.ocr_confidence_range[0])
        agent_w2_document_ingest_total.labels(
            path=write_result.path,
            doc_type=extraction.kind,
            outcome="success",
        ).inc()
        agent_w2_extraction_duration_seconds.labels(
            doc_type=extraction.kind,
            classifier_confidence_bucket=_bucket_label,
        ).observe(max(0.0, _time.monotonic() - _extract_t0))
        agent_w2_classifier_confidence.labels(doc_type=extraction.kind).observe(
            _classifier_conf
        )
        agent_w2_ocr_confidence.labels(doc_type=extraction.kind).observe(_ocr_low)
        logger.info(
            "document_ingest_metric",
            extra={
                "request_id": rid,
                "outcome": "success",
                "doc_type": extraction.kind,
                "classifier_confidence_bucket": _bucket_label,
                "duration_ms": int(
                    max(0.0, _time.monotonic() - _extract_t0) * 1000
                ),
            },
        )
    except Exception as exc:  # pragma: no cover — metrics must never break the request
        logger.warning(
            "document_ingest_metric_emit_failed",
            extra={"request_id": rid, "error": str(exc)},
        )

    soft_warns = _build_soft_warns(extraction) + observation_soft_warns
    # Reuse the layout already computed for the classifier (line ~1681) so the
    # UI can paint bbox overlays without a follow-up fetch. _layout_blocks may
    # be empty for image-only / unparseable inputs — pass through as-is.
    bbox_layout_payload = [b.to_dict() for b in (_layout_blocks or [])]
    return {
        "document_reference_id": write_result.document_reference_id,
        "extraction_id": claim.extraction_id,
        "extraction": extraction.model_dump(mode="json"),
        "citations": _flatten_citations(extraction),
        "bbox_layout": bbox_layout_payload,
        "soft_warns": soft_warns,
        "match_provenance": match_provenance,
        "metadata": {
            "cached": False,
            "fhir_write_path": write_result.path,
            "request_id": rid,
            "size_bytes": size_bytes,
            "page_count": page_count,
            "observation_ids": observation_ids,
        },
    }


# ── Evidence search (W2 Slice 4.4) ───────────────────────────────────────────
#
# POST /evidence/search — hybrid sparse+dense retrieval against
# copilot_guideline_chunks, optionally re-ranked by Cohere. Returns the top
# k snippets (chunk metadata + content + score). The retriever_node in the
# LangGraph pipeline calls rag.retrieve.search() directly; this route is the
# external surface for ad-hoc queries from agent-ui or evals.

class EvidenceSearchRequest(BaseModel):
    query: str
    k: int = 5


@app.post("/evidence/search")
async def evidence_search(body: EvidenceSearchRequest) -> dict:
    if not body.query or not body.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    # Local import keeps the optional numpy / pgvector deps out of the
    # /health and /agent/query critical paths.
    from rag import retrieve as _rag_retrieve

    snippets = await _rag_retrieve.search(body.query, k=body.k)
    out: list[dict[str, Any]] = []
    for s in snippets:
        d = s._asdict()
        ivd = d.get("indexed_version_date")
        if ivd is not None and not isinstance(ivd, str):
            try:
                d["indexed_version_date"] = ivd.isoformat()
            except Exception:
                d["indexed_version_date"] = str(ivd)
        out.append(d)
    return {"query": body.query, "snippets": out}


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
    config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
    if _langfuse_callback is not None:
        config["callbacks"] = [_langfuse_callback]

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


# ── Post-ingest context + document chat (clinical-copilot) ───────────────────
#
# After /document/ingest returns an ExtractionResult + flat citations, the UI
# wants two things the existing pipeline doesn't surface:
#   1. A short clinical summary + relevant guideline snippets keyed off the
#      extracted findings (POST /document/post-ingest-context).
#   2. A doc-grounded Q&A loop over that extraction + retrieved guidelines
#      (POST /document/{document_reference_id}/chat).
# Both routes import ``rag.retrieve`` and ``anthropic`` lazily/inline so the
# /health and /agent/query critical paths stay free of the optional deps.

_DOC_CHAT_MODEL = "claude-haiku-4-5-20251001"
_DOC_CHAT_SYSTEM_PROMPT = (
    "You are a clinical assistant answering questions about a specific "
    "document. Ground every claim in the provided extraction or guidelines. "
    "If unknown, say so. Cite using [G:chunk_id] for guidelines and "
    "[D:field_name] for extraction fields."
)
_CITATION_RE = re.compile(r"\[(G|D):([^\]\s]+)\]")


_CONDITION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "t2dm management hba1c target intensification": (
        "metformin", "ozempic", "semaglutide", "liraglutide", "dulaglutide",
        "tirzepatide", "empagliflozin", "dapagliflozin", "canagliflozin",
        "sitagliptin", "linagliptin", "glipizide", "glimepiride",
        "insulin glargine", "insulin lispro", "insulin aspart",
        "hemoglobin a1c", "hba1c", "fasting glucose",
    ),
    "lipid management ldl statin ascvd primary prevention": (
        "atorvastatin", "rosuvastatin", "simvastatin", "pravastatin",
        "ezetimibe", "fenofibrate", "gemfibrozil",
        "ldl", "cholesterol", "triglycerides",
    ),
    "chronic kidney disease ckd staging egfr nephrology": (
        "egfr", "creatinine", "bun", "albumin creatinine ratio", "acr",
    ),
    "nafld masld transaminitis liver fibrosis": (
        "alt", "ast", "alanine aminotransferase", "aspartate aminotransferase",
    ),
    "atrial fibrillation anticoagulation cha2ds2-vasc": (
        "apixaban", "rivaroxaban", "dabigatran", "warfarin",
        "atrial fibrillation", "afib",
    ),
    "anemia occult gi bleed iron deficiency on anticoagulant": (
        "hemoglobin", "hgb", "hematocrit", "hct",
    ),
    "alcohol screening audit-c brief intervention": (
        "alcohol",
    ),
    "hypertension blood pressure target": (
        "lisinopril", "losartan", "amlodipine", "metoprolol", "atenolol",
        "hydrochlorothiazide",
    ),
}


def _extract_condition_tags(extraction: dict) -> list[str]:
    """Derive clinical-condition retrieval tags from meds + abnormal labs.

    Maps medication names and abnormal lab markers to the management-oriented
    keyword phrases the guideline corpus indexes well against. Returns an
    ordered, deduplicated list (most-evidenced first).
    """
    if not isinstance(extraction, dict):
        return []

    haystack: list[str] = []
    for med in extraction.get("current_medications") or []:
        if isinstance(med, dict):
            mname = med.get("name")
            if isinstance(mname, str):
                haystack.append(mname.lower())
    for value in extraction.get("values") or []:
        if isinstance(value, dict) and value.get("abnormal_flag") in {
            "high", "low", "critical_high", "critical_low",
        }:
            n = value.get("normalized_test_name") or value.get("test_name")
            if isinstance(n, str):
                haystack.append(n.lower())
    chief = extraction.get("chief_concern")
    if isinstance(chief, dict) and isinstance(chief.get("value"), str):
        haystack.append(chief["value"].lower())

    if not haystack:
        return []
    blob = " ".join(haystack)

    tags: list[str] = []
    for tag, needles in _CONDITION_KEYWORDS.items():
        if any(n in blob for n in needles):
            tags.append(tag)
    return tags


def _build_rag_query_from_extraction(extraction: dict) -> str:
    """Produce a short (<200 char) RAG query from an ExtractionResult dict.

    Returns ``""`` when nothing extractable is present so the caller can
    short-circuit. Never echoes raw clinical values into logs — callers must
    only log a prefix.
    """
    if not isinstance(extraction, dict):
        return ""

    kind = extraction.get("kind")
    tokens: list[str] = []

    # Lead with condition tags so retrieval pulls management/target content
    # rather than only symptom-similarity content.
    tags = _extract_condition_tags(extraction)
    tokens.extend(tags[:2])

    if kind == "lab_report":
        abnormal_codes = {"high", "low", "critical_high", "critical_low"}
        for value in extraction.get("values") or []:
            if not isinstance(value, dict):
                continue
            flag = value.get("abnormal_flag")
            if flag in abnormal_codes:
                name = value.get("normalized_test_name") or value.get("test_name")
                if isinstance(name, str) and name.strip():
                    tokens.append(name.strip().lower())
            if len(tokens) >= 5:
                break
        if not tags and tokens:
            tokens.append("sepsis" if "lactate" in tokens else "abnormal lab")

    elif kind == "intake_form":
        # Skip chief_concern when condition tags fire — narrative complaint
        # text ("complications", "worry about") biases dense retrieval toward
        # symptom-similarity chunks (BP target, kidney referral) and drowns
        # out the management/target chunks the tags would surface.
        if not tags:
            chief = extraction.get("chief_concern")
            if isinstance(chief, dict):
                cc_val = chief.get("value")
                if isinstance(cc_val, str) and cc_val.strip():
                    tokens.append(cc_val.strip())

    elif kind == "unknown":
        for fact in (extraction.get("key_facts") or [])[:2]:
            if isinstance(fact, dict):
                ftext = fact.get("text")
                if isinstance(ftext, str) and ftext.strip():
                    tokens.append(ftext.strip())

    query = " ".join(tokens).strip()
    if not query:
        return ""
    return query[:199]


def _synthesize_doc_summary(extraction: dict) -> str:
    """Build a deterministic 2-3 sentence clinical summary from the extraction.

    No LLM call. The summary contains clinical text by construction — never
    log its content; log only its length.
    """
    if not isinstance(extraction, dict):
        return ""

    kind = extraction.get("kind")

    if kind == "lab_report":
        values = extraction.get("values") or []
        n_total = len(values)
        abnormal_codes = {"high", "low", "critical_high", "critical_low"}
        abnormal_names: list[str] = []
        for v in values:
            if isinstance(v, dict) and v.get("abnormal_flag") in abnormal_codes:
                nm = v.get("normalized_test_name") or v.get("test_name")
                if isinstance(nm, str) and nm.strip():
                    abnormal_names.append(nm.strip())
        if abnormal_names:
            head = ", ".join(abnormal_names[:5])
            return (
                f"Lab report with {n_total} result(s); {len(abnormal_names)} "
                f"flagged abnormal: {head}. Review the flagged values against "
                "the patient's clinical context before acting."
            )
        return (
            f"Lab report with {n_total} result(s); none flagged abnormal. "
            "Confirm completeness against the order before signing off."
        )

    if kind == "intake_form":
        chief = extraction.get("chief_concern")
        chief_txt = ""
        if isinstance(chief, dict):
            cv = chief.get("value")
            if isinstance(cv, str):
                chief_txt = cv.strip()
        n_meds = len(extraction.get("current_medications") or [])
        n_allergies = len(extraction.get("allergies") or [])
        cs_obj = extraction.get("code_status")
        cs_val = ""
        if isinstance(cs_obj, dict):
            csv = cs_obj.get("value")
            if isinstance(csv, str):
                cs_val = csv
        first = (
            f"Intake form: chief concern {chief_txt!r}."
            if chief_txt
            else "Intake form: chief concern not recorded."
        )
        second = (
            f"{n_meds} active medication(s), {n_allergies} allergy/-ies on file."
        )
        third = (
            f"Code status: {cs_val}." if cs_val else "Code status not documented."
        )
        return f"{first} {second} {third}"

    if kind == "unknown":
        guess = extraction.get("document_kind_guess") or "document"
        n_facts = len(extraction.get("key_facts") or [])
        summary = extraction.get("summary")
        tail = (
            str(summary).strip()[:160]
            if isinstance(summary, str) and summary.strip()
            else "Review the original document for clinical context."
        )
        return (
            f"Unclassified document (guess: {guess}) with {n_facts} key "
            f"fact(s). {tail}"
        )

    return ""


class PostIngestContextRequest(BaseModel):
    extraction: dict[str, Any]
    patient_id: str
    document_reference_id: str


@app.post("/document/post-ingest-context")
async def document_post_ingest_context(
    body: PostIngestContextRequest,
) -> dict[str, Any]:
    """Return a deterministic doc summary + RAG-retrieved guideline snippets.

    Empty guidelines is a valid response (200, ``guidelines: []``). The route
    never raises 500 for an unrecognised extraction shape — that path is
    short-circuited with an empty query string.
    """
    from observability.tool_logging import log_tool_outcome as _log_tool_outcome

    rid = request_id_var.get() or uuid.uuid4().hex
    started = time.perf_counter()
    _endpoint_label = "post_ingest_context"
    _outcome = "success"

    try:
        extraction = body.extraction or {}
        summary = _synthesize_doc_summary(extraction)
        query = _build_rag_query_from_extraction(extraction)

        guideline_dicts: list[dict[str, Any]] = []
        if query:
            # Local import — same pattern as /evidence/search.
            from rag import retrieve as _rag_retrieve

            try:
                snippets = await _rag_retrieve.search(query, k=5)
            except Exception as exc:  # noqa: BLE001 — retriever boundary
                logger.warning(
                    "post_ingest_context_retriever_failed",
                    extra={
                        "request_id": rid,
                        "error_type": type(exc).__name__,
                    },
                )
                snippets = []

            for s in snippets:
                d = s._asdict()
                ivd = d.get("indexed_version_date")
                if ivd is not None and not isinstance(ivd, str):
                    try:
                        d["indexed_version_date"] = ivd.isoformat()
                    except Exception:
                        d["indexed_version_date"] = str(ivd)
                content = d.get("content")
                if isinstance(content, str) and len(content) > 400:
                    d["content"] = content[:400]
                guideline_dicts.append(
                    {
                        "chunk_id": d.get("chunk_id"),
                        "source_id": d.get("source_id"),
                        "document_title": d.get("document_title"),
                        "section": d.get("section"),
                        "page_number": d.get("page_number"),
                        "content": d.get("content"),
                        "relevance_score": d.get("relevance_score"),
                    }
                )

        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "post_ingest_context_completed",
            extra={
                "request_id": rid,
                "query_prefix": query[:30],
                "n_guidelines": len(guideline_dicts),
                "duration_ms": duration_ms,
            },
        )

        return {
            "summary": summary,
            "query_used": query,
            "guidelines": guideline_dicts,
            "metadata": {
                "request_id": rid,
                "patient_id": body.patient_id,
                "document_reference_id": body.document_reference_id,
            },
        }
    except Exception:
        _outcome = "error"
        raise
    finally:
        _duration_s = time.perf_counter() - started
        _duration_ms = int(_duration_s * 1000)
        agent_post_ingest_requests_total.labels(
            endpoint=_endpoint_label, outcome=_outcome
        ).inc()
        agent_post_ingest_duration_seconds.labels(
            endpoint=_endpoint_label
        ).observe(_duration_s)
        _log_tool_outcome(
            tool_name=_endpoint_label,
            duration_ms=_duration_ms,
            cache="n/a",
            patient_id=body.patient_id,
            extra={"endpoint": _endpoint_label, "outcome": _outcome},
        )


class _ChatHistoryTurn(BaseModel):
    role: str
    content: str


class DocumentChatRequest(BaseModel):
    patient_id: str
    question: str
    extraction: dict[str, Any]
    guidelines: list[dict[str, Any]] = []
    history: list[_ChatHistoryTurn] = []


def _parse_citations_from_answer(text: str) -> list[str]:
    """Return de-duplicated ``G:.../D:...`` citation tokens, in first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _CITATION_RE.finditer(text or ""):
        token = f"{match.group(1)}:{match.group(2)}"
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


@app.post("/document/{document_reference_id}/chat")
async def document_chat(
    document_reference_id: str,
    body: DocumentChatRequest,
) -> dict[str, Any]:
    """Document-grounded chat. Returns answer + parsed citations.

    The route serializes extraction + guidelines into the user message so the
    model sees a single, self-contained prompt. We do not call
    ``query.conversation`` — that path runs FHIR retrieval, which is the wrong
    semantics here (we already have the document in hand).
    """
    from observability.tool_logging import log_tool_outcome as _log_tool_outcome

    rid = request_id_var.get() or uuid.uuid4().hex
    _started = time.perf_counter()
    _endpoint_label = "document_chat"
    _outcome = "success"

    try:
        question = (body.question or "").strip()
        if not question:
            _outcome = "error"
            raise HTTPException(status_code=400, detail="question must not be empty")

        # Compact JSON (no whitespace) keeps the prompt short.
        import json as _json

        extraction_json = _json.dumps(body.extraction or {}, separators=(",", ":"))[:8000]

        # Question-aware second retrieval: the post-ingest pass biases toward the
        # document; here we re-retrieve scoped to what the clinician actually
        # asked, then merge with the doc-time guidelines (dedup by chunk_id).
        merged: dict[str, dict[str, Any]] = {}
        for g in (body.guidelines or []):
            if isinstance(g, dict) and g.get("chunk_id"):
                merged[g["chunk_id"]] = g
        try:
            from rag import retrieve as _rag_retrieve

            tags = _extract_condition_tags(body.extraction or {})
            question_query = " ".join([*tags[:1], question]).strip()[:199]
            if question_query:
                extra_snippets = await _rag_retrieve.search(question_query, k=5)
                for s in extra_snippets:
                    d = s._asdict()
                    ivd = d.get("indexed_version_date")
                    if ivd is not None and not isinstance(ivd, str):
                        d["indexed_version_date"] = ivd.isoformat()
                    cid = d.get("chunk_id")
                    if cid and cid not in merged:
                        merged[cid] = d
        except Exception as exc:  # noqa: BLE001 — retriever boundary
            logger.warning(
                "document_chat_question_retrieval_failed",
                extra={"request_id": rid, "error_type": type(exc).__name__},
            )

        guidelines_compact = [
            {"chunk_id": g.get("chunk_id"), "content": g.get("content")}
            for g in merged.values()
            if isinstance(g, dict)
        ]
        guidelines_json = _json.dumps(guidelines_compact, separators=(",", ":"))[:8000]

        messages: list[dict[str, Any]] = []
        for turn in body.history or []:
            if turn.role in ("user", "assistant") and turn.content:
                messages.append({"role": turn.role, "content": turn.content})

        user_payload = (
            f"DOCUMENT_EXTRACTION:\n{extraction_json}\n\n"
            f"GUIDELINES:\n{guidelines_json}\n\n"
            f"DOCUMENT_REFERENCE_ID: {document_reference_id}\n"
            f"QUESTION: {question}"
        )
        messages.append({"role": "user", "content": user_payload})

        # Inline anthropic client — replicates the conversation.py setup pattern
        # without importing conversation.py (per task constraint).
        import anthropic as _anthropic

        client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        try:
            completion = await client.messages.create(
                model=_DOC_CHAT_MODEL,
                max_tokens=1024,
                system=_DOC_CHAT_SYSTEM_PROMPT,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 — provider boundary
            logger.error(
                "document_chat_anthropic_failed",
                extra={
                    "request_id": rid,
                    "error_type": type(exc).__name__,
                    "n_guidelines_provided": len(guidelines_compact),
                },
            )
            _outcome = "error"
            raise HTTPException(status_code=502, detail="Chat unavailable")

        # Extract plain-text answer from the response. The Anthropic SDK returns
        # a list of content blocks; we want the concatenated ``text`` blocks.
        raw_content = getattr(completion, "content", None) or []
        parts: list[str] = []
        for block in raw_content:
            text_attr = getattr(block, "text", None)
            if isinstance(text_attr, str):
                parts.append(text_attr)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        answer_text = "".join(parts)

        citations_used = _parse_citations_from_answer(answer_text)

        logger.info(
            "document_chat_completed",
            extra={
                "request_id": rid,
                "n_guidelines_provided": len(guidelines_compact),
                "n_citations_used": len(citations_used),
                "answer_len": len(answer_text),
            },
        )

        return {
            "answer": answer_text,
            "citations_used": citations_used,
            "metadata": {
                "request_id": rid,
                "model": _DOC_CHAT_MODEL,
                "n_guidelines_provided": len(guidelines_compact),
            },
        }
    except HTTPException:
        if _outcome == "success":
            _outcome = "error"
        raise
    except Exception:
        _outcome = "error"
        raise
    finally:
        _duration_s = time.perf_counter() - _started
        _duration_ms = int(_duration_s * 1000)
        agent_post_ingest_requests_total.labels(
            endpoint=_endpoint_label, outcome=_outcome
        ).inc()
        agent_post_ingest_duration_seconds.labels(
            endpoint=_endpoint_label
        ).observe(_duration_s)
        _log_tool_outcome(
            tool_name=_endpoint_label,
            duration_ms=_duration_ms,
            cache="n/a",
            patient_id=body.patient_id,
            extra={"endpoint": _endpoint_label, "outcome": _outcome},
        )


# ─────────────────── Phase 9 Slice 9.2 — Quarantine ───────────────────
# Panel-scoped quarantine queue for documents whose patient identity
# could not be resolved pre-extraction. Each route:
#   * derives panel_id from request_principal_var (f41827440 precedent),
#   * emits one Prometheus instrument + one structured log line per
#     CLAUDE.md "Observability — verifiable latency claims" rule, and
#   * fires an audit row through ``audit_writer.emit`` for every state
#     transition (PHI-safe detail_json: codes only, no values).
# State machine + DB queries live in ``demographics/quarantine.py``.

class _QuarantineMatchBody(BaseModel):
    target_patient_id: str


class _QuarantineRejectBody(BaseModel):
    reason: str


def _quarantine_principal() -> tuple[str, str]:
    """Return ``(provider_id, panel_id)`` for the inbound request.

    panel_id == provider_id today (one-provider == one-panel per the
    persistent Sara demo panel; see f41827440). Future slices may add an
    explicit panel claim to the JWT.
    """
    try:
        principal = request_principal_var.get()
    except LookupError:
        principal = None
    if principal is None:
        return "unauthenticated", "unauthenticated"
    pid = str(principal.get("provider_id") or principal.get("sub") or "system")
    return pid, pid


@app.get("/document/quarantine")
async def list_quarantine(state: str | None = None) -> dict:
    """Panel-scoped quarantine queue.

    Optional ``state`` filter: ``unclaimed | claimed | matched | rejected``.
    """
    from demographics import quarantine as _quar

    rid = request_id_var.get()
    provider_id, panel_id = _quarantine_principal()
    valid_states = {"unclaimed", "claimed", "matched", "rejected", "expired"}
    if state is not None and state not in valid_states:
        raise HTTPException(status_code=400, detail=f"invalid state filter: {state}")

    pool = await audit_writer.get_pool()
    rows = await _quar.list_quarantined(
        pool=pool, panel_id=panel_id, state=state  # type: ignore[arg-type]
    )
    agent_quarantine_transitions_total.labels(
        **{"from": "n/a", "to": "list", "role": "clinician"}
    ).inc()
    logger.info(
        "quarantine_list",
        extra={
            "request_id": rid,
            "provider_id": provider_id,
            "panel_id": panel_id,
            "state_filter": state,
            "n_rows": len(rows),
        },
    )
    return {"rows": rows, "panel_id": panel_id}


@app.post("/document/quarantine/{quarantine_id}/claim")
async def claim_quarantine(quarantine_id: str) -> dict:
    """Acquire the 10-minute exclusive claim lock on a quarantined upload."""
    from demographics import quarantine as _quar

    rid = request_id_var.get()
    provider_id, panel_id = _quarantine_principal()
    pool = await audit_writer.get_pool()
    try:
        result = await _quar.claim(
            pool=pool,
            quarantine_id=quarantine_id,
            claimer_provider_id=provider_id,
        )
    except _quar.QuarantineError as exc:
        status = {"not_found": 404, "conflict": 409}.get(exc.code, 400)
        agent_quarantine_transitions_total.labels(
            **{"from": "unclaimed", "to": "error", "role": "clinician"}
        ).inc()
        logger.info(
            "quarantine_claim_rejected",
            extra={
                "request_id": rid,
                "quarantine_id": quarantine_id,
                "code": exc.code,
            },
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    agent_quarantine_transitions_total.labels(
        **{"from": "unclaimed", "to": "claimed", "role": "clinician"}
    ).inc()
    logger.info(
        "quarantine_claimed",
        extra={
            "request_id": rid,
            "provider_id": provider_id,
            "panel_id": panel_id,
            "quarantine_id": result.quarantine_id,
        },
    )
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type="document_quarantine_claimed",
                request_id=rid,
                provider_id=provider_id,
                outcome="success",
                detail_json={
                    "quarantine_id": result.quarantine_id,
                    "claim_ttl_seconds": _quar.CLAIM_TTL_SECONDS,
                },
            )
        )
    except Exception:  # pragma: no cover — audit must never break the request
        pass

    expires_iso: str | None = None
    if result.claim_expires_at is not None:
        try:
            expires_iso = result.claim_expires_at.isoformat()
        except Exception:
            expires_iso = str(result.claim_expires_at)
    return {
        "quarantine_id": result.quarantine_id,
        "state": result.state,
        "claim_expires_at": expires_iso,
    }


@app.post("/document/quarantine/{quarantine_id}/match")
async def match_quarantine(
    quarantine_id: str, body: _QuarantineMatchBody
) -> dict:
    """Resolve a quarantined upload to ``target_patient_id``."""
    from demographics import quarantine as _quar

    rid = request_id_var.get()
    provider_id, panel_id = _quarantine_principal()
    pool = await audit_writer.get_pool()

    # Panel inclusion check is best-effort — if the principal doesn't carry
    # a panel patient list, we fall back to provider-id == panel-id check
    # (no extra constraint). The match SQL itself enforces the claim gate.
    panel_patient_ids: list[str] | None = None

    try:
        result = await _quar.match(
            pool=pool,
            quarantine_id=quarantine_id,
            target_patient_id=body.target_patient_id,
            claimer_provider_id=provider_id,
            panel_patient_ids=panel_patient_ids,
        )
    except _quar.QuarantineError as exc:
        status_map = {
            "not_found": 404,
            "conflict": 409,
            "claim_violation": 409,
            "claim_expired": 410,
            "panel_violation": 403,
            "invalid_target": 400,
        }
        status = status_map.get(exc.code, 400)
        agent_quarantine_transitions_total.labels(
            **{"from": "claimed", "to": "error", "role": "clinician"}
        ).inc()
        logger.info(
            "quarantine_match_rejected",
            extra={
                "request_id": rid,
                "quarantine_id": quarantine_id,
                "code": exc.code,
            },
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    agent_quarantine_transitions_total.labels(
        **{"from": "claimed", "to": "matched", "role": "clinician"}
    ).inc()
    logger.info(
        "quarantine_matched",
        extra={
            "request_id": rid,
            "provider_id": provider_id,
            "panel_id": panel_id,
            "quarantine_id": result.quarantine_id,
            "resolved_patient_id": result.resolved_patient_id,
        },
    )
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type="document_quarantine_matched",
                request_id=rid,
                provider_id=provider_id,
                patient_id=result.resolved_patient_id,
                outcome="success",
                detail_json={
                    "quarantine_id": result.quarantine_id,
                },
            )
        )
    except Exception:  # pragma: no cover — audit must never break the request
        pass

    return {
        "quarantine_id": result.quarantine_id,
        "state": result.state,
        "resolved_patient_id": result.resolved_patient_id,
    }


@app.post("/document/quarantine/{quarantine_id}/reject")
async def reject_quarantine(
    quarantine_id: str, body: _QuarantineRejectBody
) -> dict:
    """Permanently reject a quarantined upload."""
    from demographics import quarantine as _quar

    rid = request_id_var.get()
    provider_id, panel_id = _quarantine_principal()
    pool = await audit_writer.get_pool()
    try:
        result = await _quar.reject(
            pool=pool,
            quarantine_id=quarantine_id,
            reason=body.reason,
            claimer_provider_id=provider_id,
            role="clinician",
        )
    except _quar.QuarantineError as exc:
        status_map = {
            "not_found": 404,
            "conflict": 409,
            "claim_violation": 409,
            "claim_expired": 410,
            "invalid_reason": 400,
        }
        status = status_map.get(exc.code, 400)
        agent_quarantine_transitions_total.labels(
            **{"from": "claimed", "to": "error", "role": "clinician"}
        ).inc()
        logger.info(
            "quarantine_reject_rejected",
            extra={
                "request_id": rid,
                "quarantine_id": quarantine_id,
                "code": exc.code,
            },
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    agent_quarantine_transitions_total.labels(
        **{"from": "claimed", "to": "rejected", "role": "clinician"}
    ).inc()
    logger.info(
        "quarantine_rejected",
        extra={
            "request_id": rid,
            "provider_id": provider_id,
            "panel_id": panel_id,
            "quarantine_id": result.quarantine_id,
        },
    )
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type="document_quarantine_rejected",
                request_id=rid,
                provider_id=provider_id,
                outcome="success",
                detail_json={
                    "quarantine_id": result.quarantine_id,
                    "reason_chars": len(body.reason or ""),
                },
            )
        )
    except Exception:  # pragma: no cover — audit must never break the request
        pass

    return {
        "quarantine_id": result.quarantine_id,
        "state": result.state,
    }
