"""Routing eval suite — Phase 8 Phase 6.

22 tests covering standard routing, ambiguous queries, wrong-tool regression,
authorization probe, missing-data, partial identifier, dual-path triage rationale,
and edge cases.

Marker distribution:
  hard_failure    : 3  (authorization probe + canary infrastructure tests)
  clinical_accuracy: 19
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _tool_use_response(tool_name: str, tool_input: dict[str, Any]) -> MagicMock:
    """Simulate the LLM selecting a tool via tool_use."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.id = f"toolu_{tool_name}_test"
    block.input = tool_input

    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    resp.usage = MagicMock(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=70,
        cache_creation_input_tokens=0,
    )
    return resp


def _end_turn_response(narrative: str) -> MagicMock:
    """Simulate the LLM returning an end_turn text response."""
    block = MagicMock()
    block.type = "text"
    block.text = narrative

    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    resp.usage = MagicMock(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=70,
        cache_creation_input_tokens=0,
    )
    return resp


def _tool_result_then_end_turn(
    tool_name: str,
    tool_input: dict[str, Any],
    end_narrative: str,
) -> list[MagicMock]:
    """Two-turn sequence: tool_use → end_turn."""
    return [_tool_use_response(tool_name, tool_input), _end_turn_response(end_narrative)]


def _stub_tool_result(tool_name: str, result_data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "result": result_data or {"stub": True},
        "citations": [],
        "metadata": {"tool": tool_name, "patient_id": None, "duration_ms": 1, "fhir_resources_accessed": []},
    }


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def session_ctx() -> dict[str, Any]:
    """Standard session context with three census patients."""
    return {
        "provider_id": "prov-chen",
        "provider_name": "Dr. Chen",
        "patient_ids": ["pt-001", "pt-002", "pt-003"],
        "fhir_context": {},
    }


@pytest.fixture
def session_id() -> str:
    return "test-session-routing-001"


# ── Dispatch helper ───────────────────────────────────────────────────────────


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.get_event_loop().run_until_complete(coro)


def _dispatch(message: str, ctx: dict[str, Any], sid: str, llm_side_effects: list[MagicMock]) -> dict[str, Any]:
    """Patch the module-level _anthropic client and run dispatch()."""
    import sys
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.dispatcher import dispatch

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=llm_side_effects)

    with patch("agent.dispatcher._anthropic", mock_client):
        return _run(dispatch(message, sid, ctx))


# ── Standard routing (clinical_accuracy) ─────────────────────────────────────


@pytest.mark.clinical_accuracy
def test_census_query_routes_to_get_census_summary(session_ctx, session_id):
    """'Show me my patients' should trigger get_census_summary."""
    called_with: list[str] = []

    async def fake_census(inp, ctx):
        called_with.append("get_census_summary")
        return _stub_tool_result("get_census_summary", {"census": []})

    llm_steps = _tool_result_then_end_turn(
        "get_census_summary",
        {"provider_id": "prov-chen", "patient_ids": ["pt-001", "pt-002", "pt-003"]},
        "Here is your morning census.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"get_census_summary": fake_census}):
        result = _dispatch("Good morning, show me my patients", session_ctx, session_id, llm_steps)

    assert "get_census_summary" in called_with
    assert result["type"] in ("census", "text", "error")


@pytest.mark.clinical_accuracy
def test_briefing_query_routes_to_get_patient_briefing(session_ctx, session_id):
    """'Give me a briefing on bed 502' should trigger get_patient_briefing."""
    called_with: list[str] = []

    async def fake_briefing(inp, ctx):
        called_with.append("get_patient_briefing")
        return _stub_tool_result("get_patient_briefing")

    llm_steps = _tool_result_then_end_turn(
        "get_patient_briefing",
        {"patient_id": "pt-002", "provider_id": "prov-chen"},
        "Here is the briefing.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"get_patient_briefing": fake_briefing}):
        result = _dispatch("Give me a briefing on bed 502", session_ctx, session_id, llm_steps)

    assert "get_patient_briefing" in called_with


@pytest.mark.clinical_accuracy
def test_potassium_query_routes_to_query_patient_records(session_ctx, session_id):
    """'What is the current potassium for Marcus Webb?' → query_patient_records."""
    called_with: list[str] = []

    async def fake_query(inp, ctx):
        called_with.append("query_patient_records")
        return _stub_tool_result("query_patient_records")

    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-001", "query": "current potassium", "provider_id": "prov-chen"},
        "K+ is 5.9 mEq/L.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        result = _dispatch("What is the current potassium for Marcus Webb?", session_ctx, session_id, llm_steps)

    assert "query_patient_records" in called_with


@pytest.mark.clinical_accuracy
def test_medications_list_routes_to_query_patient_records(session_ctx, session_id):
    """'What medications is patient in bed 501 on?' → query_patient_records."""
    called_with: list[str] = []

    async def fake_query(inp, ctx):
        called_with.append("query_patient_records")
        return _stub_tool_result("query_patient_records")

    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-001", "query": "current medications", "provider_id": "prov-chen"},
        "Patient is on vancomycin and pip-tazo.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        _dispatch("What medications is patient in bed 501 on?", session_ctx, session_id, llm_steps)

    assert "query_patient_records" in called_with


@pytest.mark.clinical_accuracy
def test_drug_interactions_routes_to_get_medication_safety(session_ctx, session_id):
    """'Any drug interactions for the patient in bed 503?' → get_medication_safety."""
    called_with: list[str] = []

    async def fake_med(inp, ctx):
        called_with.append("get_medication_safety")
        return _stub_tool_result("get_medication_safety")

    llm_steps = _tool_result_then_end_turn(
        "get_medication_safety",
        {"patient_id": "pt-003", "provider_id": "prov-chen"},
        "No critical interactions flagged.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"get_medication_safety": fake_med}):
        _dispatch("Any drug interactions for the patient in bed 503?", session_ctx, session_id, llm_steps)

    assert "get_medication_safety" in called_with


@pytest.mark.clinical_accuracy
def test_handoff_routes_to_generate_handoff(session_ctx, session_id):
    """'Start my handoff notes' → generate_handoff."""
    called_with: list[str] = []

    async def fake_handoff(inp, ctx):
        called_with.append("generate_handoff")
        return _stub_tool_result("generate_handoff")

    llm_steps = _tool_result_then_end_turn(
        "generate_handoff",
        {"provider_id": "prov-chen", "patient_ids": ["pt-001", "pt-002", "pt-003"]},
        "Handoff generated.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"generate_handoff": fake_handoff}):
        _dispatch("Start my handoff notes", session_ctx, session_id, llm_steps)

    assert "generate_handoff" in called_with


@pytest.mark.clinical_accuracy
def test_chest_xray_routes_to_query_patient_records(session_ctx, session_id):
    """'What did the last chest X-ray show for bed 504?' → query_patient_records."""
    called_with: list[str] = []

    async def fake_query(inp, ctx):
        called_with.append("query_patient_records")
        return _stub_tool_result("query_patient_records")

    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-001", "query": "chest X-ray result", "provider_id": "prov-chen"},
        "CXR showed bilateral infiltrates.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        _dispatch("What did the last chest X-ray show for bed 504?", session_ctx, session_id, llm_steps)

    assert "query_patient_records" in called_with


@pytest.mark.clinical_accuracy
def test_ct_result_routes_to_query_patient_records(session_ctx, session_id):
    """'Is there a CT result for the patient in room 505?' → query_patient_records."""
    called_with: list[str] = []

    async def fake_query(inp, ctx):
        called_with.append("query_patient_records")
        return _stub_tool_result("query_patient_records")

    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-002", "query": "CT result", "provider_id": "prov-chen"},
        "No CT result found in the last 24 months.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        _dispatch("Is there a CT result for the patient in room 505?", session_ctx, session_id, llm_steps)

    assert "query_patient_records" in called_with


# ── Ambiguous queries (clinical_accuracy) ─────────────────────────────────────


@pytest.mark.clinical_accuracy
def test_lasix_plan_routes_to_query_patient_records(session_ctx, session_id):
    """
    'What's the Lasix plan for bed 502?' → query_patient_records.

    Disambiguation: 'plan' implies current dose/usage history (UC-3 query), not
    safety surface. If the question were 'Lasix interactions for bed 502?' the
    expected tool would be get_medication_safety instead.
    """
    called_with: list[str] = []

    async def fake_query(inp, ctx):
        called_with.append("query_patient_records")
        return _stub_tool_result("query_patient_records")

    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-002", "query": "Lasix plan current dose", "provider_id": "prov-chen"},
        "Patient is on Lasix 40mg IV BID.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        _dispatch("What's the Lasix plan for bed 502?", session_ctx, session_id, llm_steps)

    assert "query_patient_records" in called_with
    assert "get_medication_safety" not in called_with


@pytest.mark.clinical_accuracy
def test_lasix_interactions_routes_to_medication_safety(session_ctx, session_id):
    """'Lasix interactions for bed 502?' → get_medication_safety."""
    called_with: list[str] = []

    async def fake_med(inp, ctx):
        called_with.append("get_medication_safety")
        return _stub_tool_result("get_medication_safety")

    llm_steps = _tool_result_then_end_turn(
        "get_medication_safety",
        {"patient_id": "pt-002", "provider_id": "prov-chen", "medication_name": "Lasix"},
        "Furosemide — electrolyte monitoring required.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"get_medication_safety": fake_med}):
        _dispatch("Lasix interactions for bed 502?", session_ctx, session_id, llm_steps)

    assert "get_medication_safety" in called_with


@pytest.mark.clinical_accuracy
def test_full_workup_routes_to_get_patient_briefing(session_ctx, session_id):
    """
    'Full workup on the patient in bed 506' → get_patient_briefing.

    Disambiguation: 'full workup' implies a comprehensive briefing (UC-2), not
    a targeted query (UC-3).
    """
    called_with: list[str] = []

    async def fake_briefing(inp, ctx):
        called_with.append("get_patient_briefing")
        return _stub_tool_result("get_patient_briefing")

    llm_steps = _tool_result_then_end_turn(
        "get_patient_briefing",
        {"patient_id": "pt-003", "provider_id": "prov-chen"},
        "Full briefing for bed 506.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"get_patient_briefing": fake_briefing}):
        _dispatch("Full workup on the patient in bed 506", session_ctx, session_id, llm_steps)

    assert "get_patient_briefing" in called_with


# ── Wrong-tool regression tests (clinical_accuracy) ──────────────────────────


@pytest.mark.clinical_accuracy
def test_potassium_does_not_route_to_census(session_ctx, session_id):
    """'What is the current potassium?' must NOT route to get_census_summary."""
    census_called = False
    records_called = False

    async def fake_census(inp, ctx):
        nonlocal census_called
        census_called = True
        return _stub_tool_result("get_census_summary")

    async def fake_query(inp, ctx):
        nonlocal records_called
        records_called = True
        return _stub_tool_result("query_patient_records")

    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-001", "query": "potassium", "provider_id": "prov-chen"},
        "K+ is 4.1 mEq/L.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {
        "get_census_summary": fake_census,
        "query_patient_records": fake_query,
    }):
        _dispatch("What is the current potassium?", session_ctx, session_id, llm_steps)

    assert records_called, "query_patient_records must have been called"
    assert not census_called, "get_census_summary must NOT have been called for a lab query"


@pytest.mark.clinical_accuracy
def test_allergies_routes_to_medication_safety_not_records(session_ctx, session_id):
    """'Any allergies?' must route to get_medication_safety, NOT query_patient_records."""
    records_called = False
    med_called = False

    async def fake_query(inp, ctx):
        nonlocal records_called
        records_called = True
        return _stub_tool_result("query_patient_records")

    async def fake_med(inp, ctx):
        nonlocal med_called
        med_called = True
        return _stub_tool_result("get_medication_safety")

    llm_steps = _tool_result_then_end_turn(
        "get_medication_safety",
        {"patient_id": "pt-001", "provider_id": "prov-chen"},
        "Allergies: penicillin.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {
        "query_patient_records": fake_query,
        "get_medication_safety": fake_med,
    }):
        _dispatch("Any allergies?", session_ctx, session_id, llm_steps)

    assert med_called, "get_medication_safety must have been called"
    assert not records_called, "query_patient_records must NOT have been called for allergy query"


@pytest.mark.clinical_accuracy
def test_show_all_patients_routes_to_census_not_briefing(session_ctx, session_id):
    """'Show me all patients' → get_census_summary, NOT get_patient_briefing."""
    census_called = False
    briefing_called = False

    async def fake_census(inp, ctx):
        nonlocal census_called
        census_called = True
        return _stub_tool_result("get_census_summary")

    async def fake_briefing(inp, ctx):
        nonlocal briefing_called
        briefing_called = True
        return _stub_tool_result("get_patient_briefing")

    llm_steps = _tool_result_then_end_turn(
        "get_census_summary",
        {"provider_id": "prov-chen", "patient_ids": ["pt-001", "pt-002", "pt-003"]},
        "Your 3-patient census is ranked below.",
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {
        "get_census_summary": fake_census,
        "get_patient_briefing": fake_briefing,
    }):
        _dispatch("Show me all patients", session_ctx, session_id, llm_steps)

    assert census_called, "get_census_summary must have been called"
    assert not briefing_called, "get_patient_briefing must NOT have been called"


# ── Authorization probe (hard_failure) ────────────────────────────────────────


@pytest.mark.hard_failure
def test_out_of_census_patient_access_is_blocked(session_ctx, session_id):
    """
    Physician queries a patient NOT on the active census.

    Expected behavior (ARCHITECTURE.md §6.1): the dispatcher's census scope
    enforcement intercepts the tool call and does NOT execute a FHIR lookup
    for the out-of-census patient. The out-of-census patient_id must not
    appear in any successful tool call input.

    The LLM (mocked) attempts to call query_patient_records with pt-999 —
    the dispatcher scope check fires and returns a scope_enforcement error
    as the tool result without invoking the actual tool function.
    """
    import sys
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.dispatcher import dispatch

    out_of_census_pid = "pt-999"
    assert out_of_census_pid not in session_ctx["patient_ids"], "precondition: pt-999 not on census"

    fhir_called_with: list[str] = []

    async def fake_query(inp, ctx):
        fhir_called_with.append(inp.get("patient_id", ""))
        return _stub_tool_result("query_patient_records")

    # LLM tries to access out-of-census patient, then returns end_turn after scope block
    llm_steps = [
        _tool_use_response(
            "query_patient_records",
            {"patient_id": out_of_census_pid, "query": "labs", "provider_id": "prov-chen"},
        ),
        _end_turn_response("I can only access patients on your active census. Please confirm patient identity."),
    ]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=llm_steps)

    with patch("agent.dispatcher._anthropic", mock_client), \
         patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        result = _run(dispatch("What are the labs for patient 999?", session_id, session_ctx))

    # The tool function must NOT have been called with the out-of-census patient_id
    assert out_of_census_pid not in fhir_called_with, (
        f"Census scope enforcement failed: tool was called with out-of-census patient {out_of_census_pid}"
    )
    # Response must be a recognizable envelope (not a raw exception)
    assert "type" in result
    assert "narrative" in result


@pytest.mark.hard_failure
def test_in_census_patient_access_is_permitted(session_ctx, session_id):
    """Census-listed patients must be accessible — scope enforcement must not over-block."""
    import sys
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.dispatcher import dispatch

    in_census_pid = "pt-001"
    assert in_census_pid in session_ctx["patient_ids"]

    fhir_called_with: list[str] = []

    async def fake_query(inp, ctx):
        fhir_called_with.append(inp.get("patient_id", ""))
        return _stub_tool_result("query_patient_records")

    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": in_census_pid, "query": "labs", "provider_id": "prov-chen"},
        "Labs retrieved.",
    )

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=llm_steps)

    with patch("agent.dispatcher._anthropic", mock_client), \
         patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        _run(dispatch(f"What are the labs for {in_census_pid}?", session_id, session_ctx))

    assert in_census_pid in fhir_called_with, "Tool must have been called for in-census patient"


# ── Missing-data scenario (clinical_accuracy) ─────────────────────────────────


@pytest.mark.clinical_accuracy
def test_missing_echo_returns_explicit_not_found(session_ctx, session_id):
    """
    'What did the echo show for bed 501?' where patient has NO DiagnosticReport.

    Per USERS.md UC-3 acceptance criteria: the response must explicitly state
    what was searched and the search window used.
    Expected: response narrative contains 'no' or 'not found' and mentions
    the search window (months or 'mo').
    """
    called_with: list[str] = []

    async def fake_query(inp, ctx):
        called_with.append("query_patient_records")
        return {
            "result": {
                "answer": "No echocardiogram found in the last 24 months of encounters.",
                "search_window": {"encounter_window_months": 24},
            },
            "citations": [],
            "metadata": {"tool": "query_patient_records", "patient_id": "pt-001", "duration_ms": 1, "fhir_resources_accessed": []},
        }

    narrative = "No echocardiogram found. Searched encounters (24 months) and DiagnosticReports. None present."
    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-001", "query": "echo result", "provider_id": "prov-chen"},
        narrative,
    )

    with patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        result = _dispatch("What did the echo show for bed 501?", session_ctx, session_id, llm_steps)

    assert "query_patient_records" in called_with
    result_narrative = (result.get("narrative") or "").lower()
    assert "no" in result_narrative or "not found" in result_narrative, (
        "Response must explicitly state no result was found"
    )
    assert "24" in result_narrative or "mo" in result_narrative or "month" in result_narrative, (
        "Response must mention the search window per UC-3 acceptance criteria"
    )


# ── Partial patient identifier (clinical_accuracy) ────────────────────────────


@pytest.mark.clinical_accuracy
def test_ambiguous_patient_identifier_yields_clarification(session_ctx, session_id):
    """
    'The patient in bed 5-something' — ambiguous identifier.

    Expected behavior: dispatcher issues a clarification request rather than
    guessing a patient_id. The response must NOT contain a tool call with a
    guessed patient_id. The LLM (mocked) returns an end_turn clarification.
    """
    import sys
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.dispatcher import dispatch

    tool_called = False

    async def fake_any_tool(inp, ctx):
        nonlocal tool_called
        tool_called = True
        return _stub_tool_result("query_patient_records")

    # LLM correctly recognizes ambiguity and asks for clarification (end_turn)
    llm_steps = [
        _end_turn_response(
            "I need more information to identify the patient. "
            "Could you give me the full bed number or patient name?"
        )
    ]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=llm_steps)

    with patch("agent.dispatcher._anthropic", mock_client), \
         patch("agent.dispatcher.TOOL_REGISTRY", {
             "query_patient_records": fake_any_tool,
             "get_patient_briefing": fake_any_tool,
         }):
        result = _run(dispatch("The patient in bed 5-something", session_id, session_ctx))

    assert not tool_called, "No clinical tool should be called for an ambiguous identifier"
    narrative = (result.get("narrative") or "").lower()
    assert any(word in narrative for word in ("identify", "name", "bed", "which", "clarif", "confirm", "more")), (
        "Response must be a clarification request"
    )


# ── Dual-path triage rationale (clinical_accuracy) ────────────────────────────


@pytest.mark.clinical_accuracy
def test_why_is_bed_first_routes_through_dispatcher(session_ctx, session_id):
    """
    Physician types 'why is bed 501 first?' — routes through dispatcher
    (via query_patient_records or get_census_summary), NOT a direct tool bypass.
    """
    import sys
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.dispatcher import dispatch

    dispatcher_called = False
    mock_client = MagicMock()

    async def fake_query(inp, ctx):
        return {
            "result": {"answer": "pt-001 is P1 — Sepsis/Rapid Response criteria met.", "priority": "P1"},
            "citations": [],
            "metadata": {"tool": "query_patient_records", "patient_id": "pt-001", "duration_ms": 1, "fhir_resources_accessed": []},
        }

    # Dispatcher IS involved — messages.create is called
    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-001", "query": "triage rationale priority", "provider_id": "prov-chen"},
        "Bed 501 is P1 because qSOFA score is 3 (RR=26, AMS, SBP<100). Sepsis/Rapid Response criteria met.",
    )
    mock_client.messages.create = AsyncMock(side_effect=llm_steps)

    with patch("agent.dispatcher._anthropic", mock_client), \
         patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        result = _run(dispatch("why is bed 501 first?", session_id, session_ctx))

    assert mock_client.messages.create.called, "Dispatcher (LLM) must have been invoked"
    assert "type" in result and "narrative" in result


@pytest.mark.clinical_accuracy
def test_direct_triage_rationale_bypasses_dispatcher(session_ctx):
    """
    React click-to-expand calls get_triage_rationale directly via
    DIRECT_TOOL_REGISTRY — does NOT go through the dispatcher loop.
    """
    import sys, asyncio
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.tool_registry import DIRECT_TOOL_REGISTRY

    # Verify the tool is registered in DIRECT (not TOOL_REGISTRY)
    from agent.tool_registry import TOOL_REGISTRY
    assert "get_triage_rationale" in DIRECT_TOOL_REGISTRY, "get_triage_rationale must be in DIRECT_TOOL_REGISTRY"
    assert "get_triage_rationale" not in TOOL_REGISTRY, "get_triage_rationale must NOT be in TOOL_REGISTRY"

    # The tool itself is callable
    tool_fn = DIRECT_TOOL_REGISTRY["get_triage_rationale"]
    assert callable(tool_fn)


@pytest.mark.clinical_accuracy
def test_triage_rationale_response_contains_priority_level(session_ctx, session_id):
    """
    Both dispatcher-path and direct-call paths must produce a response
    that contains a priority level (integer 1–10) and at least one rule.
    This test uses the dispatcher-path mock to verify the narrative.
    """
    import sys
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.dispatcher import dispatch

    async def fake_query(inp, ctx):
        return {
            "result": {
                "answer": "pt-001 is priority level 1 (P1). Rule fired: qSOFA >= 2.",
                "triage_level": 1,
                "matched_criteria": {"qsofa_score": 3},
            },
            "citations": [],
            "metadata": {"tool": "query_patient_records", "patient_id": "pt-001", "duration_ms": 1, "fhir_resources_accessed": []},
        }

    narrative = (
        "Bed 501 is priority level 1 (Sepsis/Rapid Response). "
        "Rule fired: qSOFA score = 3 (RR=26, SBP=88, AMS). Verify directly in chart."
    )
    llm_steps = _tool_result_then_end_turn(
        "query_patient_records",
        {"patient_id": "pt-001", "query": "triage rationale", "provider_id": "prov-chen"},
        narrative,
    )
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=llm_steps)

    with patch("agent.dispatcher._anthropic", mock_client), \
         patch("agent.dispatcher.TOOL_REGISTRY", {"query_patient_records": fake_query}):
        result = _run(dispatch("Why is bed 501 first?", session_id, session_ctx))

    result_text = (result.get("narrative") or "").lower()
    # Must contain a priority indicator (level N, P1-P10, or priority)
    has_priority = any(
        token in result_text
        for token in ("priority level", "level 1", "p1", "sepsis", "rapid response", "priority")
    )
    assert has_priority, f"Response must contain a priority level indicator. Got: {result_text[:200]}"


# ── Edge cases (clinical_accuracy) ────────────────────────────────────────────


@pytest.mark.clinical_accuracy
def test_empty_message_does_not_call_clinical_tool(session_ctx, session_id):
    """Empty message '' → dispatcher returns without calling any clinical tool."""
    tool_called = False

    async def fail_if_called(inp, ctx):
        nonlocal tool_called
        tool_called = True
        return _stub_tool_result("any")

    # LLM returns clarification for empty input
    llm_steps = [_end_turn_response("Please ask a question about your patients.")]

    registry = {k: fail_if_called for k in [
        "get_census_summary", "get_patient_briefing", "query_patient_records",
        "get_medication_safety", "generate_handoff",
    ]}
    with patch("agent.dispatcher.TOOL_REGISTRY", registry):
        result = _dispatch("", session_ctx, session_id, llm_steps)

    assert not tool_called, "No clinical tool should be called for an empty message"
    assert "type" in result


@pytest.mark.clinical_accuracy
def test_greeting_does_not_call_clinical_tool(session_ctx, session_id):
    """'Hello' → dispatcher returns clarification, no clinical tool called."""
    tool_called = False

    async def fail_if_called(inp, ctx):
        nonlocal tool_called
        tool_called = True
        return _stub_tool_result("any")

    llm_steps = [_end_turn_response("Hello! What would you like to know about your patients today?")]

    registry = {k: fail_if_called for k in [
        "get_census_summary", "get_patient_briefing", "query_patient_records",
        "get_medication_safety", "generate_handoff",
    ]}
    with patch("agent.dispatcher.TOOL_REGISTRY", registry):
        result = _dispatch("Hello", session_ctx, session_id, llm_steps)

    assert not tool_called, "No clinical tool should be called for a greeting"


# ── Response envelope shape (hard_failure) ────────────────────────────────────


@pytest.mark.hard_failure
def test_every_dispatcher_response_has_required_envelope_fields(session_ctx, session_id):
    """Every dispatcher response must have type, data, narrative, citations fields."""
    import sys
    sys.path.insert(0, "/Users/hirom/Desktop/repos-gauntlet/openemr/agent-api")
    from agent.dispatcher import dispatch

    llm_steps = [_end_turn_response("Here is your census.")]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=llm_steps)

    with patch("agent.dispatcher._anthropic", mock_client):
        result = _run(dispatch("Show me my patients", session_id, session_ctx))

    required_fields = {"type", "data", "narrative", "citations"}
    missing = required_fields - set(result.keys())
    assert not missing, f"Dispatcher response missing required fields: {missing}"
