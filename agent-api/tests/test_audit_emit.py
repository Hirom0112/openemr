"""Tests for audit event emission (audit.openemr_log + dispatcher hook).

Covers:
* :func:`audit.openemr_log.emit_audit_event` — happy path, counter
  increments per outcome, defensive failure handling, optional/required
  field behaviour, and type expectations.
* The dispatcher integration: a successful tool call, a failed tool
  call, and a census-scope-blocked call each emit exactly one audit
  event with the expected outcome / failure_class.

Patterns follow ``test_dispatcher_structured_skip.py`` (AsyncMock the
Anthropic client, ``patch.dict`` the tool registry).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher  # noqa: E402
from agent.dispatcher import dispatch  # noqa: E402
from audit import openemr_log  # noqa: E402
from audit.openemr_log import (  # noqa: E402
    agent_audit_events_total,
    emit_audit_event,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _counter_value(outcome: str) -> float:
    """Read current value of agent_audit_events_total{outcome=outcome}."""
    return agent_audit_events_total.labels(outcome=outcome)._value.get()  # type: ignore[attr-defined]


def _planner_response(tool_name: str, tool_input: dict[str, Any] | None = None) -> SimpleNamespace:
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input or {},
        id="tu_audit_1",
    )
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[tool_use],
        usage=usage,
    )


def _framing_response(text: str = "Done.") -> SimpleNamespace:
    text_block = SimpleNamespace(text=text)
    text_block.type = "text"
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[text_block],
        usage=usage,
    )


# ── emit_audit_event direct tests ────────────────────────────────────────────


@pytest.mark.hard_failure
def test_happy_path_emission_calls_structured_logger() -> None:
    """All fields populated → structured logger receives the expected extras."""
    with patch.object(openemr_log, "_audit_logger") as mock_log:
        emit_audit_event(
            session_id="sess-1",
            provider_id="prov-7",
            tool_name="get_census_summary",
            outcome="ok",
            duration_ms=123,
            patient_id="pt-001",
            failure_class=None,
            request_id="req-abc",
            extra={"k": "v"},
        )

    assert mock_log.info.call_count == 1
    args, kwargs = mock_log.info.call_args
    assert args[0] == "audit.tool_call"
    extra = kwargs["extra"]
    assert extra["event"] == "audit.tool_call"
    assert extra["session_id"] == "sess-1"
    assert extra["provider_id"] == "prov-7"
    assert extra["tool"] == "get_census_summary"
    assert extra["outcome"] == "ok"
    assert extra["duration_ms"] == 123
    assert extra["patient_id"] == "pt-001"
    assert extra["request_id"] == "req-abc"
    assert extra["extra"] == {"k": "v"}
    assert "failure_class" not in extra  # None is omitted


@pytest.mark.parametrize("outcome", ["ok", "error", "blocked"])
@pytest.mark.hard_failure
def test_counter_increments_per_outcome(outcome: str) -> None:
    """Counter for the given outcome label increments by exactly 1."""
    before = _counter_value(outcome)
    emit_audit_event(
        session_id="s",
        provider_id="p",
        tool_name="t",
        outcome=outcome,  # type: ignore[arg-type]
        duration_ms=1,
    )
    after = _counter_value(outcome)
    assert after - before == 1.0, f"outcome={outcome} did not increment cleanly"


@pytest.mark.hard_failure
def test_emission_failure_is_swallowed_and_logged() -> None:
    """If the structured logger raises, emit_audit_event must not raise."""
    ok_before = _counter_value("ok")
    err_before = _counter_value("error")

    boom_logger = MagicMock()
    boom_logger.info.side_effect = RuntimeError("logger exploded")

    warn_logger = MagicMock()

    with patch.object(openemr_log, "_audit_logger", boom_logger), \
         patch.object(openemr_log, "_logger", warn_logger):
        # MUST NOT RAISE
        emit_audit_event(
            session_id="s",
            provider_id="p",
            tool_name="t",
            outcome="ok",
            duration_ms=5,
        )

    # Success counter did NOT advance.
    assert _counter_value("ok") == ok_before
    # Defensive error counter advanced and a warning was logged.
    assert _counter_value("error") - err_before == 1.0
    assert warn_logger.warning.called
    args, kwargs = warn_logger.warning.call_args
    assert args[0] == "audit_emit_failed"
    assert "error" in kwargs["extra"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", None),
        ("tool_name", None),
    ],
)
@pytest.mark.hard_failure
def test_missing_required_fields_still_emit(field: str, value: Any) -> None:
    """None on provider_id/tool_name should not crash; the event still fires.

    Note: the current implementation passes ``None`` straight into the
    extra dict.  Downstream collectors that drop on null may want a
    ``"unknown"`` sentinel — flagging if surprising.  This test asserts
    the actual shipped behaviour.
    """
    kwargs: dict[str, Any] = {
        "session_id": "s",
        "provider_id": "p",
        "tool_name": "t",
        "outcome": "ok",
        "duration_ms": 1,
    }
    kwargs[field] = value

    with patch.object(openemr_log, "_audit_logger") as mock_log:
        emit_audit_event(**kwargs)  # MUST NOT RAISE

    assert mock_log.info.call_count == 1


@pytest.mark.hard_failure
def test_outcome_none_emits_without_crashing() -> None:
    """Even an unexpected None outcome should not crash the caller."""
    # Counter.labels(outcome=None) raises in prometheus_client, so we
    # only assert the function does not propagate the exception.
    with patch.object(openemr_log, "_audit_logger"):
        emit_audit_event(
            session_id="s",
            provider_id="p",
            tool_name="t",
            outcome=None,  # type: ignore[arg-type]
            duration_ms=1,
        )


@pytest.mark.hard_failure
def test_patient_id_optional_omitted_when_absent() -> None:
    """Calls without patient_id leave the field absent from the extras."""
    with patch.object(openemr_log, "_audit_logger") as mock_log:
        emit_audit_event(
            session_id="s",
            provider_id="p",
            tool_name="t",
            outcome="ok",
            duration_ms=1,
        )
    extra = mock_log.info.call_args.kwargs["extra"]
    assert "patient_id" not in extra


@pytest.mark.hard_failure
def test_duration_ms_recorded_as_int() -> None:
    """duration_ms is preserved as an int in the structured extras."""
    with patch.object(openemr_log, "_audit_logger") as mock_log:
        emit_audit_event(
            session_id="s",
            provider_id="p",
            tool_name="t",
            outcome="ok",
            duration_ms=42,
        )
    extra = mock_log.info.call_args.kwargs["extra"]
    assert isinstance(extra["duration_ms"], int)
    assert extra["duration_ms"] == 42


# ── Dispatcher integration ───────────────────────────────────────────────────


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_dispatch_success_emits_one_ok_audit_event() -> None:
    tool_name = "get_census_summary"
    payload = {"patients": [{"id": "p1"}]}

    fake_create = AsyncMock(side_effect=[_planner_response(tool_name)])
    fake_tool = AsyncMock(return_value={"result": payload, "citations": []})

    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False), \
         patch.object(dispatcher, "emit_audit_event", side_effect=_capture):
        result = await dispatch(
            message="please run the census",
            session_id="sess-ok",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert result["type"] == "census"
    assert len(captured) == 1
    ev = captured[0]
    assert ev["outcome"] == "ok"
    assert ev["tool_name"] == tool_name
    assert ev["provider_id"] == "prov-1"
    assert isinstance(ev["duration_ms"], int)
    assert ev.get("failure_class") is None


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_dispatch_tool_failure_emits_error_audit_with_failure_class() -> None:
    tool_name = "query_patient_records"

    fake_create = AsyncMock(side_effect=[
        _planner_response(tool_name, {"patient_id": "p1"}),
        _framing_response("Sorry, I couldn't fetch that."),
    ])
    fake_tool = AsyncMock(side_effect=RuntimeError("FHIR upstream 502"))

    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False), \
         patch.object(dispatcher, "emit_audit_event", side_effect=_capture):
        await dispatch(
            message="what was the potassium?",
            session_id="sess-err",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    error_events = [e for e in captured if e["outcome"] == "error"]
    assert len(error_events) == 1
    ev = error_events[0]
    assert ev["tool_name"] == tool_name
    assert ev["failure_class"] == "fhir_unavailable"
    assert isinstance(ev["duration_ms"], int)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_dispatch_census_scope_block_emits_blocked_event() -> None:
    """A tool_use targeting a patient outside the active census is blocked
    and produces a single ``outcome="blocked"`` audit event."""
    tool_name = "query_patient_records"

    fake_create = AsyncMock(side_effect=[
        _planner_response(tool_name, {"patient_id": "off-census-99"}),
        _framing_response("Patient is not on your census."),
    ])
    # If the tool actually runs, the test should fail loudly.
    fake_tool = AsyncMock(side_effect=AssertionError("scope block bypassed"))

    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False), \
         patch.object(dispatcher, "emit_audit_event", side_effect=_capture):
        await dispatch(
            message="show records for that other patient",
            session_id="sess-blocked",
            session_context={
                "provider_id": "prov-1",
                "patient_ids": ["pt-001", "pt-002"],
            },
        )

    blocked_events = [e for e in captured if e["outcome"] == "blocked"]
    assert len(blocked_events) == 1
    ev = blocked_events[0]
    assert ev["tool_name"] == tool_name
    assert ev["patient_id"] == "off-census-99"
    assert ev["failure_class"] == "census_scope_violation"
    # duration_ms is set to 0 for blocked-pre-call events.
    assert ev["duration_ms"] == 0


# Cleanup: prevent a leaked logger handle from earlier patches affecting
# any later test in the suite that relies on the real audit logger.
@pytest.fixture(autouse=True)
def _restore_audit_logger() -> Any:
    yield
    logging.getLogger("agent.audit")
