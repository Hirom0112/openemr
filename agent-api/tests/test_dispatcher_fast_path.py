"""Tests for the deterministic briefing fast path in dispatcher.dispatch.

The fast path detects free-text "brief X" / "pre-encounter briefing for X"
/ "summary of X" phrasings, resolves the patient reference against the
active census, and calls ``get_patient_briefing`` directly — bypassing the
Anthropic planner loop entirely.

This test suite verifies:

* Happy path makes ZERO Anthropic calls and increments the fast-path counter.
* Cold-start gate: any prior turn defers to the LLM path.
* Bail-outs (no match, multiple matches, census error) fall through silently
  to the LLM dispatch loop without raising.
* Direct ``pt-NNN`` references skip the census lookup.
* Audit event is emitted exactly once with the expected fields.
* Non-matching messages do not trigger the fast path.

Mocking pattern mirrors ``tests/test_dispatcher_structured_skip.py``.
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
from agent.dispatcher import dispatch  # noqa: E402
from agent.metrics import agent_fast_path_hits_total  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


def _planner_response(tool_name: str, tool_input: dict[str, Any] | None = None) -> SimpleNamespace:
    """Fake Anthropic response that issues a single tool_use block."""
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input or {},
        id="tu_fp_test",
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


def _census_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "result": {
            "census": entries,
            "total": len(entries),
            "requested": 0,
            "dropped": 0,
            "dropped_ids": [],
        },
        "citations": [],
        "metadata": {},
    }


def _briefing_payload(patient_id: str) -> dict[str, Any]:
    return {
        "result": {"patient_id": patient_id, "sections": [], "alerts": []},
        "citations": [{"patient_id": patient_id, "resource_type": "Patient"}],
        "metadata": {"tool": "get_patient_briefing"},
    }


def _fast_path_counter(tool: str = "get_patient_briefing") -> float:
    return agent_fast_path_hits_total.labels(tool=tool)._value.get()  # type: ignore[attr-defined]


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_happy_path_skips_llm_and_increments_counter() -> None:
    """'brief Marcus Webb' → census resolves to pt-001 → briefing tool runs.

    Asserts no Anthropic call is made (fast path skips the planner entirely)
    and the fast-path counter advances by one.
    """
    fake_create = AsyncMock()  # should NEVER be called
    fake_census = AsyncMock(
        return_value=_census_payload([{"patient_id": "pt-001", "name": "Marcus Webb"}])
    )
    fake_briefing = AsyncMock(return_value=_briefing_payload("pt-001"))

    before = _fast_path_counter()
    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="brief Marcus Webb",
            session_id="sess-fp-happy",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 0, "Fast path must not call Anthropic"
    assert fake_briefing.await_count == 1
    assert result["type"] == "briefing"
    assert result["data"]["patient_id"] == "pt-001"
    assert result["metadata"].get("fast_path") is True
    assert _fast_path_counter() - before == pytest.approx(1.0)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_cold_start_required_history_falls_through() -> None:
    """When the session has prior turns, the fast path defers to the LLM."""
    fake_create = AsyncMock(side_effect=[_planner_response("get_patient_briefing", {"patient_id": "pt-001"})])
    fake_briefing = AsyncMock(return_value=_briefing_payload("pt-001"))
    # Census MUST NOT run — we never reach the resolution step.
    fake_census = AsyncMock(side_effect=AssertionError("census should not run"))

    # Stub history loader to return a non-empty conversation.
    async def _fake_load(session_id: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "earlier message"}]

    with patch.object(dispatcher, "_load_history", _fake_load), \
         patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="brief Marcus Webb",
            session_id="sess-fp-history",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 1, (
        "Cold-start gate regressed: fast path fired despite prior history"
    )
    assert result["type"] == "briefing"
    assert result["metadata"].get("fast_path") is not True


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_no_name_match_falls_through_to_llm() -> None:
    """Reference that does not match any census patient → LLM dispatch path."""
    fake_create = AsyncMock(side_effect=[_planner_response("get_patient_briefing", {"patient_id": "pt-001"})])
    fake_census = AsyncMock(
        return_value=_census_payload([{"patient_id": "pt-001", "name": "Marcus Webb"}])
    )
    fake_briefing = AsyncMock(return_value=_briefing_payload("pt-001"))

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="brief XYZ Patient",
            session_id="sess-fp-nomatch",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    # Census ran during fast-path resolution attempt, then bailed; LLM path
    # took over and called the planner once.
    assert fake_create.await_count == 1
    # Fast path did NOT mark this dispatch.
    assert result["metadata"].get("fast_path") is not True


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_multiple_matches_fall_through_to_llm() -> None:
    """Two census entries matching the reference → fast path bails."""
    fake_create = AsyncMock(side_effect=[_planner_response("get_patient_briefing", {"patient_id": "pt-001"})])
    fake_census = AsyncMock(
        return_value=_census_payload([
            {"patient_id": "pt-001", "name": "Marcus Webb"},
            {"patient_id": "pt-002", "name": "Marcus Stone"},
        ])
    )
    fake_briefing = AsyncMock(return_value=_briefing_payload("pt-001"))

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="brief Marcus",
            session_id="sess-fp-multi",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 1
    assert result["metadata"].get("fast_path") is not True


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_direct_id_format_skips_census_lookup() -> None:
    """'brief patient pt-001' → bypass census, call briefing directly."""
    fake_create = AsyncMock()
    fake_census = AsyncMock(side_effect=AssertionError("census must not run for direct ID"))
    fake_briefing = AsyncMock(return_value=_briefing_payload("pt-001"))

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="brief patient pt-001",
            session_id="sess-fp-id",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 0
    assert fake_census.await_count == 0
    assert fake_briefing.await_count == 1
    assert result["type"] == "briefing"
    assert result["metadata"].get("fast_path") is True


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_audit_event_emitted_exactly_once_on_success() -> None:
    """Fast-path success → exactly one audit event with the briefing fields."""
    fake_create = AsyncMock()
    fake_census = AsyncMock(
        return_value=_census_payload([{"patient_id": "pt-001", "name": "Marcus Webb"}])
    )
    fake_briefing = AsyncMock(return_value=_briefing_payload("pt-001"))

    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ), \
         patch.object(dispatcher, "emit_audit_event", side_effect=_capture):
        await dispatch(
            message="pre-encounter briefing for Marcus Webb",
            session_id="sess-fp-audit",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert len(captured) == 1
    ev = captured[0]
    assert ev["tool_name"] == "get_patient_briefing"
    assert ev["outcome"] == "ok"
    assert ev["patient_id"] == "pt-001"
    assert ev["provider_id"] == "prov-1"
    assert isinstance(ev["duration_ms"], int)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_census_error_falls_through_to_llm() -> None:
    """Census tool raising must NOT crash dispatch — fall through to LLM."""
    fake_create = AsyncMock(side_effect=[_planner_response("get_patient_briefing", {"patient_id": "pt-001"})])
    fake_census = AsyncMock(side_effect=RuntimeError("FHIR upstream 502"))
    fake_briefing = AsyncMock(return_value=_briefing_payload("pt-001"))

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="brief Marcus Webb",
            session_id="sess-fp-census-err",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    # LLM path ran; fast path bailed silently.
    assert fake_create.await_count == 1
    assert result["metadata"].get("fast_path") is not True


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_pattern_non_match_skips_fast_path() -> None:
    """Unrelated message ('what's the weather?') → fast path does not fire."""
    fake_create = AsyncMock(side_effect=[_planner_response("query_patient_records", {"patient_id": "pt-001"})])
    # Census must not run — patterns don't match.
    fake_census = AsyncMock(side_effect=AssertionError("census should not run"))
    fake_query = AsyncMock(return_value={"result": {"answer": "n/a"}, "citations": []})

    # Pad in case the LLM loop ends with a framing turn.
    text_block = SimpleNamespace(text="ok")
    text_block.type = "text"
    framing = SimpleNamespace(
        stop_reason="end_turn",
        content=[text_block],
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    fake_create.side_effect = [
        _planner_response("query_patient_records", {"patient_id": "pt-001"}),
        framing,
    ]

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "query_patient_records": fake_query,
             },
             clear=False,
         ):
        result = await dispatch(
            message="what's the weather?",
            session_id="sess-fp-nopattern",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    # LLM path took over.
    assert fake_create.await_count >= 1
    assert result["metadata"].get("fast_path") is not True
