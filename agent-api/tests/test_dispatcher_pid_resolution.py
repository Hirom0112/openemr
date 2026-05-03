"""Tests for the dispatcher's pre-scope patient_id resolution.

The census-scope guard at ``agent/dispatcher.py`` previously rejected any
``tool_input.patient_id`` that wasn't a verbatim member of the active
census. The planner, however, may emit either:

* a synthetic-prefix form (``pt-001``) that needs ``_normalize_patient_id``,
* or a free-text patient name (``"Marcus Webb"``) that needs
  ``_resolve_patient_from_census``,

before the value matches the canonical census pid. This module verifies
the dispatcher resolves both forms, rewrites ``tool_input.patient_id`` to
the canonical pid, increments the ``agent_pid_resolution_total`` counter,
and only falls into ``census_scope_violation`` (or the new
``ambiguous_name`` failure_class) when no resolution is possible.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher  # noqa: E402
from agent.dispatcher import dispatch  # noqa: E402
from agent.metrics import agent_pid_resolution_total  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


def _planner_tool_use(tool_name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input,
        id="tu_pid_test",
    )
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="tool_use", content=[tool_use], usage=usage)


def _planner_end_turn(text: str = "done") -> SimpleNamespace:
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        input_tokens=5,
        output_tokens=3,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="end_turn", content=[block], usage=usage)


def _resolution_counter(method: str) -> float:
    return agent_pid_resolution_total.labels(method=method)._value.get()  # type: ignore[attr-defined]


def _make_session_context(
    *,
    patient_ids: list[str],
    census: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {"provider_id": "prov-1", "patient_ids": patient_ids}
    if census is not None:
        ctx["census"] = census
    return ctx


def _tool_response(patient_id: str | None = None) -> dict[str, Any]:
    return {
        "result": {"patient_id": patient_id, "ok": True},
        "citations": [],
        "metadata": {},
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_synthetic_prefix_normalizes_to_canonical_pid() -> None:
    """``pt-001`` is rewritten to ``"1"`` before the tool runs."""
    fake_create = AsyncMock(side_effect=[
        _planner_tool_use("get_patient_briefing", {"patient_id": "pt-001"}),
        _planner_end_turn(),
    ])
    fake_tool = AsyncMock(return_value=_tool_response("1"))
    fake_census = AsyncMock(return_value={"result": {"census": []}, "citations": [], "metadata": {}})

    before = _resolution_counter("normalize")
    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_patient_briefing": fake_tool,
                 "get_census_summary": fake_census,
             },
             clear=False,
         ):
        await dispatch(
            message="show briefing",
            session_id="sess-pid-norm",
            session_context=_make_session_context(patient_ids=["1", "2"]),
        )

    assert fake_tool.await_count == 1
    called_input = fake_tool.await_args.args[0]
    assert called_input["patient_id"] == "1", "tool_input not rewritten to canonical pid"
    assert _resolution_counter("normalize") - before == pytest.approx(1.0)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_patient_name_resolves_via_census() -> None:
    """``"Marcus Webb"`` is rewritten to the matching census pid."""
    fake_create = AsyncMock(side_effect=[
        _planner_tool_use("get_patient_briefing", {"patient_id": "Marcus Webb"}),
        _planner_end_turn(),
    ])
    fake_tool = AsyncMock(return_value=_tool_response("pt-001"))
    fake_census = AsyncMock(return_value={"result": {"census": []}, "citations": [], "metadata": {}})

    census_entries = [
        {"patient_id": "pt-001", "name": "Marcus Webb"},
        {"patient_id": "pt-002", "name": "Sara Chen"},
    ]
    before = _resolution_counter("name_match")
    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_patient_briefing": fake_tool,
                 "get_census_summary": fake_census,
             },
             clear=False,
         ):
        await dispatch(
            message="brief that patient",
            session_id="sess-pid-name",
            session_context=_make_session_context(
                patient_ids=["pt-001", "pt-002"],
                census=census_entries,
            ),
        )

    assert fake_tool.await_count == 1
    called_input = fake_tool.await_args.args[0]
    assert called_input["patient_id"] == "pt-001"
    assert _resolution_counter("name_match") - before == pytest.approx(1.0)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_ambiguous_name_emits_ambiguous_failure_class() -> None:
    """A name matching 2+ census entries → ambiguous_name failure_class."""
    fake_create = AsyncMock(side_effect=[
        _planner_tool_use("get_patient_briefing", {"patient_id": "Marcus"}),
        _planner_end_turn(),
    ])
    fake_tool = AsyncMock(return_value=_tool_response("pt-001"))
    fake_census = AsyncMock(return_value={"result": {"census": []}, "citations": [], "metadata": {}})

    census_entries = [
        {"patient_id": "pt-001", "name": "Marcus Webb"},
        {"patient_id": "pt-002", "name": "Marcus Stone"},
    ]
    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.object(dispatcher, "emit_audit_event", _capture), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_patient_briefing": fake_tool,
                 "get_census_summary": fake_census,
             },
             clear=False,
         ):
        await dispatch(
            message="brief Marcus",
            session_id="sess-pid-amb",
            session_context=_make_session_context(
                patient_ids=["pt-001", "pt-002"],
                census=census_entries,
            ),
        )

    # Tool must NOT have been invoked: ambiguous resolution blocks dispatch.
    assert fake_tool.await_count == 0
    failure_classes = [c.get("failure_class") for c in captured if c.get("failure_class")]
    assert "ambiguous_name" in failure_classes, (
        f"expected ambiguous_name failure_class, got {failure_classes!r}"
    )
    assert "census_scope_violation" not in failure_classes


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_unknown_pid_falls_through_to_scope_violation() -> None:
    """Unresolvable input → unchanged census_scope_violation behavior."""
    fake_create = AsyncMock(side_effect=[
        _planner_tool_use("get_patient_briefing", {"patient_id": "Nonexistent Person"}),
        _planner_end_turn(),
    ])
    fake_tool = AsyncMock(return_value=_tool_response("pt-001"))
    fake_census = AsyncMock(return_value={"result": {"census": []}, "citations": [], "metadata": {}})

    census_entries = [{"patient_id": "pt-001", "name": "Marcus Webb"}]
    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.object(dispatcher, "emit_audit_event", _capture), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_patient_briefing": fake_tool,
                 "get_census_summary": fake_census,
             },
             clear=False,
         ):
        await dispatch(
            message="brief stranger",
            session_id="sess-pid-unknown",
            session_context=_make_session_context(
                patient_ids=["pt-001"],
                census=census_entries,
            ),
        )

    assert fake_tool.await_count == 0
    failure_classes = [c.get("failure_class") for c in captured if c.get("failure_class")]
    assert "census_scope_violation" in failure_classes
    assert "ambiguous_name" not in failure_classes


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_canonical_pid_skips_resolution_helpers() -> None:
    """Already-in-census pid → fast path, helpers never consulted."""
    fake_create = AsyncMock(side_effect=[
        _planner_tool_use("get_patient_briefing", {"patient_id": "pt-001"}),
        _planner_end_turn(),
    ])
    fake_tool = AsyncMock(return_value=_tool_response("pt-001"))
    fake_census = AsyncMock(return_value={"result": {"census": []}, "citations": [], "metadata": {}})

    fake_normalize = Mock(side_effect=AssertionError("normalize must not run"))
    fake_resolve = Mock(side_effect=AssertionError("name resolver must not run"))

    # Stub history loader so the deterministic briefing fast path defers to
    # the LLM dispatch loop (its cold-start gate requires empty history).
    async def _fake_load(session_id: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "earlier"}]

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.object(dispatcher, "_load_history", _fake_load), \
         patch.object(dispatcher, "_normalize_patient_id", fake_normalize), \
         patch.object(dispatcher, "_resolve_patient_from_census", fake_resolve), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_patient_briefing": fake_tool,
                 "get_census_summary": fake_census,
             },
             clear=False,
         ):
        await dispatch(
            message="show vitals",
            session_id="sess-pid-canonical",
            session_context=_make_session_context(patient_ids=["pt-001"]),
        )

    assert fake_tool.await_count == 1
    assert fake_normalize.call_count == 0
    assert fake_resolve.call_count == 0
    called_input = fake_tool.await_args.args[0]
    assert called_input["patient_id"] == "pt-001"
