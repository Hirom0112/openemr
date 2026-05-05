"""HS256 JWT verification middleware for the Clinical Co-Pilot agent-api.

Verifies tokens minted by the OpenEMR PHP layer and binds verified claims to
``request_principal_var`` so downstream code (audit logger, dispatcher) can
read the authenticated principal without threading it through every call.

Security model
--------------
* HS256 only; secret loaded from ``settings.copilot_jwt_secret``.
* Bypass list (``/health``, ``/metrics``, ``/docs``, ``/openapi.json``,
  ``/redoc``, OPTIONS preflight) skips JWT entirely.
* Empty secret → middleware bypasses auth entirely (no-op) and logs a single
  startup warning. Keeps tests + dev passing without per-fixture changes.
* Provider-id mismatch: if a POST body contains a top-level ``provider_id``
  that doesn't match the token's ``sub``, return 403 ``scope_violation``.
  Defends against a JWT for provider X being used to pull provider Y's
  patients.
* Failure responses NEVER include the raw token or stack trace.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from config import settings

logger = logging.getLogger(__name__)


# Bound to the verified token claims for the lifetime of a request. ``None``
# means either (a) auth is bypassed (empty secret / bypass-listed path) or
# (b) we are outside a request context (background tasks, tests).
request_principal_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "request_principal_var", default=None
)


# Endpoints that must work without a token (liveness probes, OpenAPI docs).
# Match by path prefix so ``/metrics`` and ``/metrics/`` both bypass.
_BYPASS_PATHS: tuple[str, ...] = (
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
)


_warned_empty_secret = False


def _is_bypassed(request: Request) -> bool:
    if request.method == "OPTIONS":
        return True
    path = request.url.path
    for prefix in _BYPASS_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _unauth(reason: str) -> JSONResponse:
    # Reason is a stable enum-ish string — never the token, never an exception
    # message. Logged at info level (auth failures are operational, not errors).
    logger.info("auth_failed", extra={"reason": reason})
    return JSONResponse(
        status_code=401,
        content={"detail": "auth_failed", "reason": reason},
    )


async def jwt_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """FastAPI HTTP middleware: verify Bearer JWT, bind principal, enforce scope."""
    global _warned_empty_secret

    # Bypass mode — empty secret means deployment without auth (dev/test).
    if not settings.copilot_jwt_secret:
        if not _warned_empty_secret:
            logger.warning(
                "JWT auth BYPASSED — COPILOT_JWT_SECRET is empty. "
                "All requests are unauthenticated. Do not run this in production."
            )
            _warned_empty_secret = True
        return await call_next(request)

    if _is_bypassed(request):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _unauth("missing_bearer")
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        return _unauth("empty_token")

    try:
        claims = jwt.decode(
            token,
            settings.copilot_jwt_secret,
            algorithms=["HS256"],
            issuer="openemr-copilot",
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        return _unauth("expired")
    except jwt.InvalidIssuerError:
        return _unauth("bad_issuer")
    except jwt.InvalidSignatureError:
        return _unauth("bad_signature")
    except jwt.InvalidTokenError:
        return _unauth("invalid_token")
    except Exception:  # defensive — never leak details
        return _unauth("invalid_token")

    sub = claims.get("sub")
    sid = claims.get("sid")
    exp = claims.get("exp")
    if not isinstance(sub, str) or not sub:
        return _unauth("missing_sub")
    if not isinstance(sid, str) or not sid:
        return _unauth("missing_sid")

    principal: dict[str, Any] = {
        "provider_id": sub,
        "session_id": sid,
        "exp": int(exp) if exp is not None else 0,
    }

    # Provider-id mismatch check on POST + JSON bodies.
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type.lower():
            body = await request.body()
            # Cache so route handlers re-read the same bytes (Starlette
            # consumes the receive stream on first .body() call).
            request._body = body  # type: ignore[attr-defined]
            if body:
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict) and "provider_id" in parsed:
                    body_pid = parsed.get("provider_id")
                    if body_pid is not None and str(body_pid) != sub:
                        logger.warning(
                            "scope_violation",
                            extra={
                                "token_provider_id": sub,
                                "body_provider_id": str(body_pid),
                                "path": request.url.path,
                            },
                        )
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "scope_violation"},
                        )

    token_ctx = request_principal_var.set(principal)
    try:
        return await call_next(request)
    finally:
        request_principal_var.reset(token_ctx)
