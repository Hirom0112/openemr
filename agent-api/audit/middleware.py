"""FastAPI HTTP middleware that records every authenticated request.

Runs AFTER the route handler completes, captures method/path/status/
duration/principal, and dispatches a single :class:`AuditEvent` to
:func:`audit.writer.emit`. The writer is fire-and-forget so middleware
adds at most one async DB call to each request — and even that is
skipped when ``settings.audit_db_url`` is empty.

Bypass list mirrors the JWT middleware so liveness probes, OpenAPI
docs, and CORS preflights do not pollute the audit table.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from fastapi import Request
from starlette.responses import Response

from audit.models import AuditEvent
from audit.writer import emit as _emit_event

logger = logging.getLogger(__name__)

# Mirror auth.jwt_middleware._BYPASS_PATHS so request-audit and auth share
# the same surface. Duplicated (not imported) to keep ``audit`` a true leaf
# package per .importlinter.
_BYPASS_PATHS: tuple[str, ...] = (
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def _is_bypassed(request: Request) -> bool:
    if request.method == "OPTIONS":
        return True
    path = request.url.path
    for prefix in _BYPASS_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _outcome_for_status(status_code: int) -> str:
    if status_code in (401, 403):
        return "denied"
    if 200 <= status_code < 400:
        return "success"
    return "failure"


async def _safe_extract_auth_reason(response: Response) -> str | None:
    """Pull the ``reason`` field from a 401 JSON body, if present.

    Returns ``None`` for any response we cannot safely read.  Best-effort:
    a corrupt or chunked body is silently ignored — auth_reason is
    nice-to-have telemetry, not load-bearing.
    """
    body = getattr(response, "body", None)
    if not body:
        return None
    try:
        if isinstance(body, (bytes, bytearray)):
            text = body.decode("utf-8", errors="replace")
        else:
            text = str(body)
        parsed = json.loads(text)
    except Exception:
        return None
    if isinstance(parsed, dict):
        reason = parsed.get("reason")
        if isinstance(reason, str):
            return reason
    return None


async def audit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Record one PHI-audit row per authenticated request.

    MUST be registered AFTER ``RequestIdMiddleware`` so ``request_id_var``
    is bound, and AFTER ``jwt_middleware`` so ``request_principal_var``
    carries the verified principal (when auth is on).
    """
    if _is_bypassed(request):
        return await call_next(request)

    started = time.perf_counter()
    response: Response
    try:
        response = await call_next(request)
    except Exception:
        # Re-raise after recording a synthetic 500 row so the audit log
        # captures the request even when the handler crashed.
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _record(request, status_code=500, duration_ms=duration_ms, auth_reason=None)
        raise

    duration_ms = int((time.perf_counter() - started) * 1000)
    auth_reason: str | None = None
    if response.status_code == 401:
        auth_reason = await _safe_extract_auth_reason(response)
    await _record(request, status_code=response.status_code, duration_ms=duration_ms, auth_reason=auth_reason)
    return response


async def _record(
    request: Request,
    *,
    status_code: int,
    duration_ms: int,
    auth_reason: str | None,
) -> None:
    """Build the AuditEvent and dispatch to the writer. Never raises."""
    # Principal is stashed on ``request.state`` by the HTTP entrypoint
    # (main.py) before this middleware runs — keeps the ``audit`` package
    # a true leaf in .importlinter (no dependency on the ``auth`` module).
    principal: dict[str, Any] | None = getattr(request.state, "audit_principal", None)

    detail: dict[str, Any] = {}
    if auth_reason:
        detail["auth_reason"] = auth_reason

    # request_id is stashed by RequestIdMiddleware on request.state — reading
    # it from there (rather than the observability ContextVar) keeps the
    # audit package a true leaf in .importlinter.
    request_id = getattr(request.state, "request_id", None)

    event = AuditEvent(
        event_type="request",
        request_id=request_id,
        session_id=(principal or {}).get("session_id"),
        provider_id=(principal or {}).get("provider_id"),
        method=request.method,
        path=request.url.path,
        status_code=status_code,
        duration_ms=duration_ms,
        outcome=_outcome_for_status(status_code),
        detail_json=detail,
    )
    try:
        await _emit_event(event)
    except Exception as exc:  # pragma: no cover — emit() already swallows
        logger.warning(
            "audit_middleware_emit_failed",
            extra={"path": request.url.path, "error": str(exc)},
        )
