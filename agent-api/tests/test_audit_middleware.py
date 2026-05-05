"""Tests for the PHI audit middleware (``audit.middleware``).

Covers:

* The middleware records exactly one event per non-bypassed request.
* Bypass-listed paths (``/health``, ``/metrics``) record nothing.
* The recorded outcome maps correctly from status_code:
  2xx → ``success``, 401/403 → ``denied``, 5xx → ``failure``.
* When the underlying writer raises, the response is still returned
  unchanged (audit must never affect request correctness).
* The principal stash → request.state path is honoured: the middleware
  reads provider_id / session_id from ``request.state.audit_principal``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from audit import middleware as audit_mw  # noqa: E402
from audit.middleware import audit_middleware  # noqa: E402


def _build_app(captured: list) -> FastAPI:
    app = FastAPI()
    # principal-stash hop, mirrors main.py
    @app.middleware("http")
    async def _stash(request: Any, call_next: Any) -> Any:
        # Manually stash a fake principal so middleware can read it.
        request.state.audit_principal = {
            "provider_id": "prov-7",
            "session_id": "sess-abc",
        }
        return await call_next(request)

    app.middleware("http")(audit_middleware)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/thing")
    async def thing() -> dict:
        return {"thing": "value"}

    @app.get("/api/boom")
    async def boom() -> dict:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="boom")

    @app.get("/api/forbidden")
    async def forbidden() -> dict:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="nope")

    return app


def _patch_emit(captured: list) -> Any:
    async def _capture(event: Any) -> None:
        captured.append(event)
    return patch.object(audit_mw, "_emit_event", _capture)


def test_records_exactly_one_event_per_request() -> None:
    captured: list = []
    app = _build_app(captured)
    with _patch_emit(captured):
        client = TestClient(app)
        resp = client.get("/api/thing")
    assert resp.status_code == 200
    assert resp.json() == {"thing": "value"}
    assert len(captured) == 1
    e = captured[0]
    assert e.event_type == "request"
    assert e.method == "GET"
    assert e.path == "/api/thing"
    assert e.status_code == 200
    assert e.outcome == "success"
    assert e.provider_id == "prov-7"
    assert e.session_id == "sess-abc"


def test_bypass_paths_record_nothing() -> None:
    captured: list = []
    app = _build_app(captured)
    with _patch_emit(captured):
        client = TestClient(app)
        resp = client.get("/health")
    assert resp.status_code == 200
    assert captured == []


def test_outcome_maps_403_to_denied() -> None:
    captured: list = []
    app = _build_app(captured)
    with _patch_emit(captured):
        client = TestClient(app)
        resp = client.get("/api/forbidden")
    assert resp.status_code == 403
    assert len(captured) == 1
    assert captured[0].outcome == "denied"
    assert captured[0].status_code == 403


def test_outcome_maps_500_to_failure() -> None:
    captured: list = []
    app = _build_app(captured)
    with _patch_emit(captured):
        client = TestClient(app)
        resp = client.get("/api/boom")
    assert resp.status_code == 500
    assert len(captured) == 1
    assert captured[0].outcome == "failure"


def test_writer_failure_does_not_break_response() -> None:
    """If the writer raises, the original response MUST still be returned."""
    captured: list = []
    app = _build_app(captured)

    async def _explode(_event: Any) -> None:
        raise RuntimeError("writer is down")

    with patch.object(audit_mw, "_emit_event", _explode):
        client = TestClient(app)
        resp = client.get("/api/thing")
    assert resp.status_code == 200
    assert resp.json() == {"thing": "value"}


def test_duration_ms_is_recorded_and_nonnegative() -> None:
    captured: list = []
    app = _build_app(captured)
    with _patch_emit(captured):
        client = TestClient(app)
        client.get("/api/thing")
    assert len(captured) == 1
    assert isinstance(captured[0].duration_ms, int)
    assert captured[0].duration_ms >= 0
