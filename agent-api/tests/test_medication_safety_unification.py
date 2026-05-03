"""Unify the medication-safety button path and the typed-query dispatcher path.

Both code paths must surface the tool's ``summary`` (the LLM-generated
physician-readable analysis from ``medication/safety.py::add_llm_summary``)
as the ``narrative`` the UI's MedicationSafetyRenderer renders under
"Analysis".  Historically the button path returned ``narrative=''`` and the
typed path ran a second LLM "framing turn" — two different strings for the
same question.  This test pins the unified contract.

Covers:
  - Typed path: dispatcher consumes the tool's ``summary`` directly and
    skips the second Anthropic call entirely.
  - Button path: ``GET /medication/safety/{patient_id}`` exposes ``summary``
    in its flat payload so ``getMedicationSafety`` in agent-ui/src/api.ts
    can lift it into ``AgentResponse.narrative``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher
from agent.dispatcher import _STRUCTURED_RESPONSE_TYPES, dispatch


_SUMMARY = (
    "Marcus is critically ill with sepsis; weigh any new antibiotic against "
    "his documented penicillin allergy. Verify in the chart."
)


def _planner_response(tool_name: str, tool_input: dict[str, Any] | None = None) -> SimpleNamespace:
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input or {},
        id="tu_med_safety_1",
    )
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="tool_use", content=[tool_use], usage=usage)


@pytest.mark.hard_failure
def test_medication_safety_in_structured_response_types() -> None:
    """Pin the policy: medication_safety joins the structured-skip set."""
    assert "medication_safety" in _STRUCTURED_RESPONSE_TYPES


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_typed_path_uses_tool_summary_as_narrative() -> None:
    """Dispatcher's medication_safety path lifts ``result.summary`` into the
    response narrative and makes exactly ONE Anthropic call (planner only)."""
    payload: dict[str, Any] = {
        "patient_id": "pt-001",
        "summary": _SUMMARY,
        "current_medications": ["Vancomycin"],
        "allergies": ["Penicillin"],
        "interactions": [],
        "generated_at": "2026-05-03T12:00:00+00:00",
    }

    fake_create = AsyncMock(side_effect=[_planner_response("get_medication_safety", {"patient_id": "pt-001"})])
    fake_tool = AsyncMock(return_value={"result": payload, "citations": []})

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {"get_medication_safety": fake_tool}, clear=False):
        result = await dispatch(
            message="any allergies for him",
            session_id="sess-med-typed",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    # Exactly one Anthropic call — no framing turn.
    assert fake_create.await_count == 1, (
        f"Expected ONE Anthropic call (planner only); got {fake_create.await_count}. "
        "medication_safety must skip the framing turn now that it consumes "
        "the tool's summary directly."
    )
    assert result["type"] == "medication_safety"
    assert result["data"] == payload
    # Narrative starts with the tool's summary verbatim (canary suffixes may
    # be appended when blank-allergy heuristics fire — this fixture has a
    # documented allergy so no canary is appended).
    assert result["narrative"].startswith(_SUMMARY), result["narrative"]


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_typed_path_falls_back_when_summary_missing() -> None:
    """When the tool returns no ``summary`` (legacy / failure path), the
    placeholder narrative still renders so verification stays well-formed."""
    payload: dict[str, Any] = {
        "patient_id": "pt-002",
        "current_medications": [],
        "allergies": [],
        "interactions": [],
    }

    fake_create = AsyncMock(side_effect=[_planner_response("get_medication_safety", {"patient_id": "pt-002"})])
    fake_tool = AsyncMock(return_value={"result": payload, "citations": []})

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.dict(dispatcher.TOOL_REGISTRY, {"get_medication_safety": fake_tool}, clear=False):
        result = await dispatch(
            message="check medication safety",
            session_id="sess-med-fallback",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 1
    assert isinstance(result["narrative"], str) and result["narrative"]
    assert "pt-002" in result["narrative"]


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_button_endpoint_exposes_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /medication/safety/{patient_id}`` must expose ``summary`` in its
    flat payload so ``getMedicationSafety`` in api.ts can lift it into
    ``AgentResponse.narrative`` — unifying with the typed-query path."""
    async def _fake_tool(input_dict: dict[str, Any], session_context: dict[str, Any]) -> dict[str, Any]:
        return {
            "result": {
                "patient_id": input_dict["patient_id"],
                "medications_reviewed": 1,
                "flag_count": 0,
                "flags": [],
                "summary": _SUMMARY,
                "current_medications": ["Vancomycin"],
                "allergies": ["Penicillin"],
                "interactions": [],
                "generated_at": "2026-05-03T12:00:00+00:00",
            },
            "citations": [],
            "metadata": {},
        }

    import main as main_module
    monkeypatch.setattr(main_module, "get_medication_safety", _fake_tool)

    client = TestClient(main_module.app)
    resp = client.get("/medication/safety/pt-unify")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Contract for api.ts: top-level ``summary`` is what becomes
    # ``AgentResponse.narrative`` on the button path.
    assert body.get("summary") == _SUMMARY, (
        "Button-path response must surface the tool's summary so api.ts can "
        "use it as the AgentResponse.narrative (parity with the typed path)."
    )
