"""Tests for the error_class / retry_suggested envelope contract.

The dispatcher maps an internal :class:`ToolFailureClass` enum onto a
small set of UI-visible categories (``transient``, ``persistent``,
``missing_data``, ``unknown``).  The frontend switches on these strings
and must not break when we ship.

Mapping found in ``agent-api/agent/dispatcher.py``::

    FHIR_UNAVAILABLE   -> "transient"   retry_suggested=True
    LLM_TIMEOUT        -> "transient"   retry_suggested=True
    TOOL_VALIDATION    -> "persistent"  retry_suggested=False
    VERIFICATION_BLOCK -> "persistent"  retry_suggested=False
    UNKNOWN            -> "unknown"     retry_suggested=True

Note ``missing_data`` is reserved but currently unused on the failure
path (no ToolFailureClass maps to it), so it is exercised here only by
calling the private ``_error_response`` helper indirectly via the
mapping tables — not via end-to-end dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher  # noqa: E402
from agent.dispatcher import (  # noqa: E402
    ERROR_CLASS_MISSING_DATA,
    ERROR_CLASS_PERSISTENT,
    ERROR_CLASS_TRANSIENT,
    ERROR_CLASS_UNKNOWN,
    ToolFailureClass,
    _error_response,
    _FAILURE_CLASS_TO_ERROR_CLASS,
    _RETRY_BY_ERROR_CLASS,
    dispatch,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _planner_response(tool_name: str, tool_input: dict[str, Any] | None = None) -> SimpleNamespace:
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input or {},
        id="tu_err_1",
    )
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="tool_use", content=[tool_use], usage=usage)


def _framing_response(text: str = "Done.") -> SimpleNamespace:
    text_block = SimpleNamespace(text=text)
    text_block.type = "text"
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="end_turn", content=[text_block], usage=usage)


# Map ToolFailureClass → an exception message that ``_classify_failure``
# narrows to that class.  Drives the parametrized end-to-end test.
_TRIGGER_MESSAGE: dict[ToolFailureClass, str] = {
    ToolFailureClass.FHIR_UNAVAILABLE: "FHIR upstream 502",
    ToolFailureClass.LLM_TIMEOUT: "operation timed out",
    ToolFailureClass.TOOL_VALIDATION: "validation: bad input",
    ToolFailureClass.VERIFICATION_BLOCK: "verification: blocked",
    ToolFailureClass.UNKNOWN: "something weird happened",
}

_EXPECTED_ERROR_CLASS: dict[ToolFailureClass, str] = {
    ToolFailureClass.FHIR_UNAVAILABLE: ERROR_CLASS_TRANSIENT,
    ToolFailureClass.LLM_TIMEOUT: ERROR_CLASS_TRANSIENT,
    ToolFailureClass.TOOL_VALIDATION: ERROR_CLASS_PERSISTENT,
    ToolFailureClass.VERIFICATION_BLOCK: ERROR_CLASS_PERSISTENT,
    ToolFailureClass.UNKNOWN: ERROR_CLASS_UNKNOWN,
}

_EXPECTED_RETRY: dict[ToolFailureClass, bool] = {
    ToolFailureClass.FHIR_UNAVAILABLE: True,
    ToolFailureClass.LLM_TIMEOUT: True,
    ToolFailureClass.TOOL_VALIDATION: False,
    ToolFailureClass.VERIFICATION_BLOCK: False,
    # NOTE: UNKNOWN currently maps to retry_suggested=True.  This is the
    # shipped behaviour — debatable on its merits since "unknown" by
    # definition gives no signal that retry will help.  Keeping the
    # assertion in lockstep with the implementation so a deliberate
    # change still trips the test.
    ToolFailureClass.UNKNOWN: True,
}


# ── Mapping tables (unit-level) ──────────────────────────────────────────────


@pytest.mark.parametrize("failure_class", list(ToolFailureClass))
@pytest.mark.hard_failure
def test_failure_class_mapping_table(failure_class: ToolFailureClass) -> None:
    """Every enum value has a stable error_class in the mapping table."""
    expected = _EXPECTED_ERROR_CLASS[failure_class]
    assert _FAILURE_CLASS_TO_ERROR_CLASS[failure_class] == expected


@pytest.mark.parametrize(
    "error_class,expected_retry",
    [
        (ERROR_CLASS_TRANSIENT, True),
        (ERROR_CLASS_PERSISTENT, False),
        (ERROR_CLASS_MISSING_DATA, False),
        (ERROR_CLASS_UNKNOWN, True),
    ],
)
@pytest.mark.hard_failure
def test_retry_table(error_class: str, expected_retry: bool) -> None:
    assert _RETRY_BY_ERROR_CLASS[error_class] is expected_retry


# ── _error_response shape ────────────────────────────────────────────────────


@pytest.mark.parametrize("failure_class", list(ToolFailureClass))
@pytest.mark.hard_failure
def test_error_response_envelope_shape(failure_class: ToolFailureClass) -> None:
    """The error envelope keys are exactly the contract the UI consumes."""
    env = _error_response(failure_class, "detail")

    # Top-level shape: {type, data, narrative, citations, metadata}
    assert set(env.keys()) == {"type", "data", "narrative", "citations", "metadata"}
    assert env["type"] == "error"
    assert env["data"] is None
    assert env["citations"] == []

    md = env["metadata"]
    assert md["error"] is True
    assert md["failure_class"] == failure_class.value
    assert md["error_class"] == _EXPECTED_ERROR_CLASS[failure_class]
    assert md["retry_suggested"] is _EXPECTED_RETRY[failure_class]
    # retry_after_ms is omitted unless explicitly supplied.
    assert "retry_after_ms" not in md


@pytest.mark.hard_failure
def test_error_response_includes_retry_after_ms_when_supplied() -> None:
    """Rate-limit / 429-like cases include retry_after_ms as int."""
    env = _error_response(
        ToolFailureClass.FHIR_UNAVAILABLE, "rate limited", retry_after_ms=2500,
    )
    assert env["metadata"]["retry_after_ms"] == 2500
    assert isinstance(env["metadata"]["retry_after_ms"], int)


# ── End-to-end dispatcher tests ──────────────────────────────────────────────


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_dispatch_unhandled_exception_returns_unknown_error_envelope() -> None:
    """An exception escaping the main loop produces an error envelope
    classified as UNKNOWN → error_class='unknown', retry_suggested=True."""
    # Force the very first Anthropic call to raise outside any inner
    # try/except, so the outer ``except Exception`` returns
    # ``_error_response(UNKNOWN, ...)``.
    fake_create = AsyncMock(side_effect=RuntimeError("anthropic exploded"))
    with patch.object(dispatcher._anthropic.messages, "create", fake_create):
        result = await dispatch(
            message="anything",
            session_id="sess-unhandled",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert result["type"] == "error"
    md = result["metadata"]
    assert md["failure_class"] == ToolFailureClass.UNKNOWN.value
    assert md["error_class"] == ERROR_CLASS_UNKNOWN
    assert md["retry_suggested"] is True


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_dispatch_max_tokens_with_no_data_returns_llm_timeout_envelope() -> None:
    """``max_tokens`` with no tool result yields an LLM_TIMEOUT (transient)."""
    max_tok_response = SimpleNamespace(
        stop_reason="max_tokens",
        content=[],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    fake_create = AsyncMock(side_effect=[max_tok_response])
    with patch.object(dispatcher._anthropic.messages, "create", fake_create):
        result = await dispatch(
            message="anything",
            session_id="sess-maxtok",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert result["type"] == "error"
    md = result["metadata"]
    assert md["failure_class"] == ToolFailureClass.LLM_TIMEOUT.value
    assert md["error_class"] == ERROR_CLASS_TRANSIENT
    assert md["retry_suggested"] is True


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_dispatch_tool_failure_does_not_short_circuit_to_error_envelope() -> None:
    """Documented behaviour: when a tool raises, the dispatcher hands the
    error to the LLM as a tool_result and lets it framing-narrate.  The
    final envelope is NOT an error envelope and carries no error_class.

    This is intentionally separate from the UNKNOWN/TIMEOUT envelope
    tests above so a future change to short-circuit on tool-call errors
    immediately surfaces here as a deliberate contract change.
    """
    tool_name = "query_patient_records"
    fake_create = AsyncMock(side_effect=[
        _planner_response(tool_name, {"patient_id": "p1"}),
        _framing_response("I couldn't fetch that."),
    ])
    fake_tool = AsyncMock(side_effect=RuntimeError("FHIR upstream 502"))

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="what was the potassium?",
            session_id="sess-tool-fail-narrated",
            session_context={"provider_id": "prov-1", "patient_ids": ["p1"]},
        )

    assert result["type"] != "error"
    md = result.get("metadata", {})
    assert "error_class" not in md
    assert "failure_class" not in md
    assert "retry_suggested" not in md


@pytest.mark.parametrize(
    "failure_class",
    [
        ToolFailureClass.FHIR_UNAVAILABLE,
        ToolFailureClass.LLM_TIMEOUT,
        ToolFailureClass.TOOL_VALIDATION,
        ToolFailureClass.VERIFICATION_BLOCK,
        ToolFailureClass.UNKNOWN,
    ],
    ids=lambda fc: fc.value,
)
@pytest.mark.hard_failure
def test_error_response_path_per_failure_class(
    failure_class: ToolFailureClass,
) -> None:
    """Direct ``_error_response`` exercise for every enum value: the
    envelope fields the UI switches on (error_class, retry_suggested,
    failure_class) match the documented mapping."""
    env = _error_response(failure_class, "x")
    md = env["metadata"]
    assert md["failure_class"] == failure_class.value
    assert md["error_class"] == _EXPECTED_ERROR_CLASS[failure_class]
    assert md["retry_suggested"] is _EXPECTED_RETRY[failure_class]


# ── Edge cases ───────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_no_error_path_envelope_has_no_error_class() -> None:
    """A successful dispatch returns an envelope with NO error_class /
    retry_suggested keys — they only appear on the error path."""
    tool_name = "get_census_summary"
    fake_create = AsyncMock(side_effect=[_planner_response(tool_name)])
    fake_tool = AsyncMock(return_value={
        "result": {"patients": [{"id": "p1"}]},
        "citations": [],
    })

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: fake_tool}, clear=False):
        result = await dispatch(
            message="run census",
            session_id="sess-ok-no-err",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert result["type"] == "census"
    md = result["metadata"]
    assert "error_class" not in md
    assert "retry_suggested" not in md
    assert "failure_class" not in md


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_recovery_after_failed_then_successful_call() -> None:
    """If the first tool call fails and a subsequent call succeeds, the
    final envelope reflects the success — it does NOT carry the prior
    error_class or failure_class."""
    tool_name = "query_patient_records"

    # Two planner turns (each returns one tool_use) followed by a
    # framing turn.  The first tool invocation fails, the second
    # succeeds.
    fake_create = AsyncMock(side_effect=[
        _planner_response(tool_name, {"patient_id": "p1"}),
        _planner_response(tool_name, {"patient_id": "p1"}),
        _framing_response("Final answer."),
    ])

    call_count = {"n": 0}

    async def _flaky_tool(tool_input: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("FHIR upstream 502")
        return {"result": {"answer": "recovered"}, "citations": []}

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {tool_name: _flaky_tool}, clear=False):
        result = await dispatch(
            message="what was the potassium?",
            session_id="sess-recover",
            session_context={"provider_id": "prov-1", "patient_ids": ["p1"]},
        )

    md = result["metadata"]
    assert "error_class" not in md
    assert "failure_class" not in md
    assert "retry_suggested" not in md
    # The final type is the LLM-led free-text path.
    assert result["type"] == "query_answer"


@pytest.mark.hard_failure
def test_error_envelope_keys_snapshot() -> None:
    """Pinned snapshot of the error envelope's metadata keys.

    If this fails, the frontend ``QueryAnswerRenderer`` / error banner
    likely needs to be updated alongside.
    """
    env = _error_response(ToolFailureClass.UNKNOWN, "boom")
    assert set(env.keys()) == {"type", "data", "narrative", "citations", "metadata"}
    expected_md_keys = {
        "error",
        "failure_class",
        "error_class",
        "retry_suggested",
        "detail",
    }
    assert set(env["metadata"].keys()) == expected_md_keys
