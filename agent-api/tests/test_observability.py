"""Tests for JSON logging, request_id ContextVar propagation, and the
client-timing receiver endpoint."""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed in host environment")
pytest.importorskip("httpx", reason="httpx not installed in host environment")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.json_logging import (
    JsonLogFormatter,
    RequestIdFilter,
    request_id_var,
)


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_json_formatter_includes_extras_and_required_fields() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.session_id = "sess-123"
    record.duration_ms = 42

    line = formatter.format(record)
    payload = json.loads(line)

    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["session_id"] == "sess-123"
    assert payload["duration_ms"] == 42
    assert "ts" in payload


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_request_id_filter_pulls_from_contextvar() -> None:
    flt = RequestIdFilter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )

    token = request_id_var.set("abc-123")
    try:
        assert flt.filter(record) is True
        assert getattr(record, "request_id") == "abc-123"
    finally:
        request_id_var.reset(token)


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_request_id_filter_noop_when_unset() -> None:
    flt = RequestIdFilter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )
    assert request_id_var.get() is None
    assert flt.filter(record) is True
    assert not hasattr(record, "request_id")


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_request_id_middleware_sets_response_header_and_contextvar() -> None:
    """End-to-end: middleware mints a request_id, exposes it via header, and
    leaves it visible to handlers via the ContextVar."""
    import uuid as _uuid

    from starlette.middleware.base import BaseHTTPMiddleware

    app = FastAPI()

    class _Mw(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
            rid = request.headers.get("X-Request-ID") or _uuid.uuid4().hex
            request.state.request_id = rid
            tok = request_id_var.set(rid)
            try:
                response = await call_next(request)
            finally:
                request_id_var.reset(tok)
            response.headers["X-Request-ID"] = rid
            return response

    app.add_middleware(_Mw)

    @app.get("/echo")
    async def echo() -> dict:
        return {"request_id_in_handler": request_id_var.get()}

    client = TestClient(app)

    r = client.get("/echo")
    assert r.status_code == 200
    rid = r.headers["X-Request-ID"]
    assert rid
    assert r.json()["request_id_in_handler"] == rid

    r2 = client.get("/echo", headers={"X-Request-ID": "fixed-id"})
    assert r2.headers["X-Request-ID"] == "fixed-id"
    assert r2.json()["request_id_in_handler"] == "fixed-id"


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_client_timing_endpoint_returns_204_and_increments_metric() -> None:
    from prometheus_client import CollectorRegistry, generate_latest

    from main import app
    from agent.metrics import agent_client_timing_seconds

    client = TestClient(app)

    before = agent_client_timing_seconds.labels(action="panel_open")._sum.get()  # type: ignore[attr-defined]

    resp = client.post(
        "/agent/client-timing",
        json={
            "action": "panel_open",
            "duration_ms": 250,
            "session_id": "sess-x",
            "request_id": "req-y",
        },
    )
    assert resp.status_code == 204
    assert resp.headers.get("X-Request-ID")

    after = agent_client_timing_seconds.labels(action="panel_open")._sum.get()  # type: ignore[attr-defined]
    assert after - before == pytest.approx(0.250, rel=1e-3)
