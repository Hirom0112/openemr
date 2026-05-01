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
  POST /agent/query                  — dispatcher: all use cases via tool_use loop
  POST /agent/triage_rationale/{id}  — direct-call triage rationale (click-to-expand)
  POST /session/{id}/message         — raw conversation turn (checkpointer)
  GET  /session/{id}/history         — stored conversation turns
"""

import asyncio
import logging
import time

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langfuse import Langfuse
from prometheus_client import Counter, Histogram, make_asgi_app
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from pydantic import ConfigDict as _PydanticConfig

# PHP sends numeric PIDs and provider IDs as JSON numbers.
# _CoerceModel tells Pydantic v2 to coerce numbers to str instead of 422-ing.
class _CoerceModel(BaseModel):
    model_config = _PydanticConfig(coerce_numbers_to_str=True)

from agent.dispatcher import dispatch
from agent.metrics import (
    agent_cache_hits_total,
    agent_cache_misses_total,
    agent_dispatch_latency_seconds,
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
)
from auth.fhir_client import (
    fhir_client,
    get_access_token,
    invalidate_token_cache,
)
from briefing.schema import BriefingResponse
from checkpointer.redis_saver import RedisSaver
from checkpointer.sqlite_saver import SqliteSaver
from config import settings
from triage.census import build_census

logging.basicConfig(level=settings.log_level)
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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


@app.post("/triage/census")
async def triage_census(body: CensusRequest) -> dict:
    if not body.patient_ids:
        raise HTTPException(status_code=400, detail="patient_ids must not be empty")
    try:
        tool_result = await get_census_summary(
            {"provider_id": "system", "patient_ids": body.patient_ids},
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

@app.post("/briefing/{patient_id}", response_model=BriefingResponse)
async def briefing(patient_id: str) -> BriefingResponse:
    _t0 = time.perf_counter()
    try:
        tool_result = await get_patient_briefing(
            {"patient_id": patient_id, "provider_id": "system"},
            session_context=_session_ctx(),
        )
        BRIEFING_DURATION.observe(time.perf_counter() - _t0)
        return BriefingResponse.model_validate(tool_result["result"])
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
async def medication_safety(patient_id: str) -> dict:
    try:
        tool_result = await get_medication_safety(
            {"patient_id": patient_id, "provider_id": "system"},
            session_context=_session_ctx(),
        )
        return tool_result["result"]
    except Exception as exc:
        raise HTTPException(status_code=502, detail="FHIR upstream error") from exc


# ── UC-5 Parallel Handoff ─────────────────────────────────────────────────────  LEGACY — retire after Phase 13 cutover

class HandoffRequest(BaseModel):
    patient_ids: list[str]


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


@app.post("/agent/prefetch")
async def agent_prefetch(request: PrefetchRequest) -> dict:
    """Signal that the React panel has mounted and FHIR pre-fetch should begin.

    V1: acknowledges immediately; the actual cache warming is handled by the
    existing FHIR client on the first get_census_summary tool call.  This
    endpoint exists so the UI can fire a non-blocking fetch on mount without
    waiting for it — keeping the session-open flow in UX_SPEC §3 intact.
    """
    logger.info(
        "Pre-fetch signal received",
        extra={"session_id": request.session_id, "patient_count": len(request.patient_ids)},
    )

    async def _warm() -> None:
        try:
            await build_census(
                request.patient_ids,
                redis_client=_redis,
                cache_key=f"copilot:census:{request.session_id}",
            )
        except Exception as exc:
            logger.warning(
                "Pre-fetch cache warming failed",
                extra={"session_id": request.session_id, "error": str(exc)},
            )

    asyncio.create_task(_warm())
    return {"status": "acknowledged", "session_id": request.session_id}


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
