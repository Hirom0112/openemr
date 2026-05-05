"""Golden-set prompt eval harness for the dispatcher / system prompt.

Why this exists
---------------
``agent/system_prompt.py`` is edited frequently to tune disambiguation,
list-selection and safety-canary behavior.  Each edit was previously a
"vibe check" — there was no automated way to know if the new prompt
regressed any earlier behavior or whether the new instruction worked.

This module runs the dispatcher against a fixed set of conversations and
asserts on (a) which tool the model called (or that it correctly chose
NOT to call one), (b) phrases that must appear in the response, and (c)
phrases that must NOT appear (e.g. "I don't have a list of options" — a
known regression mode).

Two execution modes
-------------------
* **Stubbed** (default; CI):
    The Anthropic client is patched so each model turn returns a canned
    response declared on the case (``stub_assistant_turns``).  Tools are
    patched to return canned payloads.  This verifies that the dispatcher
    *pipeline* still does the right thing with the response shapes our
    cases describe — and that the assertion harness itself works.

* **Live** (``EVAL_LIVE=1``):
    The real Anthropic client is used.  Tools are still stubbed (so we
    don't need a live OpenEMR), but the LLM actually generates responses
    against the real system prompt — this is where prompt regressions
    surface.  Run with::

        EVAL_LIVE=1 ANTHROPIC_API_KEY=sk-... \\
            python3 -m pytest tests/test_prompt_eval.py -v

Adding a case is one entry in ``tests/fixtures/prompt_eval_cases.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher
from agent.dispatcher import dispatch
from tests.fixtures.prompt_eval_cases import (
    CASES,
    PromptEvalCase,
    StubText,
    StubToolUse,
)

EVAL_LIVE = os.environ.get("EVAL_LIVE") == "1"


# ── Stubbed Anthropic response builders ───────────────────────────────────────

def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _mk_tool_use_response(tool_name: str, tool_input: dict[str, Any], idx: int) -> SimpleNamespace:
    block = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input,
        id=f"tu_eval_{idx}",
    )
    return SimpleNamespace(stop_reason="tool_use", content=[block], usage=_usage())


def _mk_text_response(text: str) -> SimpleNamespace:
    block = SimpleNamespace(text=text)
    block.type = "text"
    return SimpleNamespace(stop_reason="end_turn", content=[block], usage=_usage())


def _build_stub_responses(case: PromptEvalCase) -> list[SimpleNamespace]:
    out: list[SimpleNamespace] = []
    for i, turn in enumerate(case.stub_assistant_turns):
        if isinstance(turn, StubToolUse):
            out.append(_mk_tool_use_response(turn.tool_name, turn.tool_input, i))
        elif isinstance(turn, StubText):
            out.append(_mk_text_response(turn.text))
        else:  # pragma: no cover — defensive
            raise TypeError(f"Unknown stub turn type: {type(turn)!r}")
    return out


# ── Conversation seeding ─────────────────────────────────────────────────────

class _InMemorySaver:
    """Minimal saver for seeding history without touching Redis/SQLite."""

    def __init__(self, seed: list[dict[str, Any]]) -> None:
        # Stored shape mirrors the rest of the codebase: {"role", "content"}.
        self._turns: list[dict[str, Any]] = list(seed)

    async def load(self, _session_id: str) -> list[dict[str, Any]]:
        return list(self._turns)

    async def append(self, _session_id: str, role: str, content: Any) -> None:
        self._turns.append({"role": role, "content": content})


# ── Tool stubs ───────────────────────────────────────────────────────────────

def _build_tool_stubs(
    case: PromptEvalCase,
    tool_calls: list[dict[str, Any]],
) -> dict[str, AsyncMock]:
    """Patch dispatcher.TOOL_REGISTRY entries with AsyncMocks that record calls."""
    stubs: dict[str, AsyncMock] = {}
    for tool_name, payload in case.stub_tool_results.items():
        async def _stub(tool_input, session_context, _name=tool_name, _payload=payload):
            tool_calls.append({"name": _name, "input": dict(tool_input)})
            return {
                "result": _payload,
                "citations": [{"source": "fhir", "tool": _name, "patient_id": tool_input.get("patient_id")}],
            }
        stubs[tool_name] = AsyncMock(side_effect=_stub)
    # Also register a generic recording stub for any tool the case did NOT
    # declare a payload for, so that an unexpected tool call doesn't blow up
    # with a missing-tool error — we want it to register on tool_calls so the
    # assertion can fail cleanly with full diagnostic context.
    for tool_name in dispatcher.TOOL_REGISTRY:
        if tool_name in stubs:
            continue

        async def _generic(tool_input, session_context, _name=tool_name):
            tool_calls.append({"name": _name, "input": dict(tool_input)})
            return {"result": {}, "citations": []}

        stubs[tool_name] = AsyncMock(side_effect=_generic)
    return stubs


# ── Live-mode tool stubs ─────────────────────────────────────────────────────
# Even in live mode we don't want to hit a real OpenEMR.  Provide minimally
# realistic payloads so the model sees plausible tool_result content.

def _live_tool_payload(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    pid = tool_input.get("patient_id", "pt-001")
    if tool_name == "get_census_summary":
        return {
            "patients": [
                {"patient_id": "pt-001", "name": "Marcus Webb", "rank": 1},
                {"patient_id": "pt-002", "name": "Delia Fontaine", "rank": 2},
                {"patient_id": "pt-003", "name": "Raymond Park", "rank": 3},
                {"patient_id": "pt-004", "name": "James Whitfield", "rank": 4},
            ]
        }
    if tool_name == "get_patient_briefing":
        return {
            "patient_id": pid,
            "sections": [{"title": "Active problems", "bullets": ["CHF"]}],
            "code_status": None,
            "allergies_complete": False,
        }
    if tool_name == "get_medication_safety":
        return {
            "patient_id": pid,
            "interactions": [],
            "allergies": [{"substance": "penicillin", "reaction": "hives"}],
        }
    if tool_name == "query_patient_records":
        return {"answer": "see chart for details", "supporting_observations": [{"id": "obs-1"}]}
    if tool_name == "generate_handoff":
        return {"total": 3, "patients": [{"id": pid, "summary": "stable"}]}
    return {}


def _build_live_tool_stubs(tool_calls: list[dict[str, Any]]) -> dict[str, AsyncMock]:
    stubs: dict[str, AsyncMock] = {}
    for tool_name in dispatcher.TOOL_REGISTRY:
        async def _stub(tool_input, session_context, _name=tool_name):
            tool_calls.append({"name": _name, "input": dict(tool_input)})
            return {
                "result": _live_tool_payload(_name, tool_input),
                "citations": [{"source": "fhir", "tool": _name, "patient_id": tool_input.get("patient_id")}],
            }
        stubs[tool_name] = AsyncMock(side_effect=_stub)
    return stubs


# ── Assertion engine ─────────────────────────────────────────────────────────

def _check_assertions(
    case: PromptEvalCase,
    response: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> list[str]:
    """Return a list of failure messages.  Empty list means all assertions passed."""
    failures: list[str] = []
    exp = case.expected
    narrative = (response.get("narrative") or "").lower()
    called_names = [c["name"] for c in tool_calls]

    for name in exp.tool_called:
        if name not in called_names:
            failures.append(f"expected tool '{name}' to be called; called={called_names}")

    if exp.tool_called and not exp.allow_no_tool and not called_names:
        failures.append(f"expected at least one tool call; got none. expected={exp.tool_called}")

    if not exp.tool_called and not exp.allow_no_tool and not called_names:
        # Most cases don't allow zero tools unless explicitly allowed.
        failures.append("expected at least one tool call; got none")

    for name in exp.tool_not_called:
        if name in called_names:
            failures.append(f"expected tool '{name}' NOT to be called; called={called_names}")

    for tool_name, required_input in exp.tool_input_contains.items():
        matching = [c for c in tool_calls if c["name"] == tool_name]
        if not matching:
            failures.append(f"tool_input_contains: tool '{tool_name}' was never called")
            continue
        # Use the FIRST call to that tool.
        actual_input = matching[0]["input"]
        for k, v in required_input.items():
            if actual_input.get(k) != v:
                failures.append(
                    f"tool_input_contains: {tool_name}.{k} expected {v!r}, got {actual_input.get(k)!r}"
                )

    for needle in exp.narrative_contains:
        if needle.lower() not in narrative:
            failures.append(f"narrative_contains: missing {needle!r}")

    for needle in exp.narrative_excludes:
        if needle.lower() in narrative:
            failures.append(f"narrative_excludes: contains forbidden {needle!r}")

    if exp.data_patient_id is not None:
        data = response.get("data") or {}
        actual = data.get("patient_id") or data.get("patientId")
        if actual != exp.data_patient_id:
            failures.append(
                f"data_patient_id expected {exp.data_patient_id!r}, got {actual!r}"
            )

    if exp.min_citations:
        n = len(response.get("citations") or [])
        if n < exp.min_citations:
            failures.append(f"min_citations expected ≥{exp.min_citations}, got {n}")

    return failures


def _format_failure_report(
    case: PromptEvalCase,
    response: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    failures: list[str],
) -> str:
    return (
        f"\n=== prompt-eval case FAILED: {case.name} ===\n"
        f"user_message:\n  {case.user_message}\n"
        f"prior_conversation ({len(case.conversation)} turns):\n"
        + "".join(f"  - {t['role']}: {json.dumps(t['content'])[:200]}\n" for t in case.conversation)
        + f"tool_calls ({len(tool_calls)}):\n"
        + "".join(f"  - {c['name']}({json.dumps(c['input'])})\n" for c in tool_calls)
        + f"final response.narrative:\n  {response.get('narrative')!r}\n"
        + f"final response.type: {response.get('type')!r}\n"
        + f"final response.data: {json.dumps(response.get('data'))[:300]}\n"
        + "failures:\n"
        + "".join(f"  * {f}\n" for f in failures)
    )


# ── The test ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_prompt_eval_case(case: PromptEvalCase) -> None:
    """Run one golden-set case end-to-end through the dispatcher."""
    tool_calls: list[dict[str, Any]] = []

    # Seed conversation history via an in-memory saver so the dispatcher
    # actually loads the prior turns the case depends on.
    saver = _InMemorySaver(case.conversation)

    session_context: dict[str, Any] = {
        "provider_id": "prov-eval",
        "provider_name": "Eval",
        "patient_ids": [],
        "sqlite_saver": saver,
    }
    session_context.update(case.session_context)

    # ── Tool stubs ────────────────────────────────────────────────────────
    if EVAL_LIVE:
        tool_stubs = _build_live_tool_stubs(tool_calls)
    else:
        tool_stubs = _build_tool_stubs(case, tool_calls)

    # ── Anthropic stub (only in stubbed mode) ─────────────────────────────
    if EVAL_LIVE:
        anthropic_patch_ctx = _NullPatch()
    else:
        responses = _build_stub_responses(case)
        # Pad with extra end_turn responses so we never run out if dispatch
        # makes one more call than the case anticipated (e.g. framing pass
        # that the structured-skip shortcut might or might not cover).
        responses.extend([_mk_text_response("(end)") for _ in range(3)])
        anthropic_patch_ctx = patch.object(
            dispatcher._anthropic.messages,
            "create",
            AsyncMock(side_effect=responses),
        )

    with anthropic_patch_ctx, patch.dict(dispatcher.TOOL_REGISTRY, tool_stubs, clear=False):
        response = await dispatch(
            message=case.user_message,
            session_id=f"sess-eval-{case.name}",
            session_context=session_context,
        )

    failures = _check_assertions(case, response, tool_calls)
    if failures:
        pytest.fail(_format_failure_report(case, response, tool_calls, failures))


# ── Misc ─────────────────────────────────────────────────────────────────────

class _NullPatch:
    """No-op context manager used when live mode skips Anthropic patching."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None


def test_case_set_is_nonempty_and_unique() -> None:
    """Sanity: the case file ships ≥45 cases and case names are unique."""
    assert len(CASES) >= 45, f"expected ≥45 cases, got {len(CASES)}"
    names = [c.name for c in CASES]
    assert len(names) == len(set(names)), f"duplicate case names: {names}"


# Required-marker hook in conftest.py needs every test marked.
test_case_set_is_nonempty_and_unique = pytest.mark.hard_failure(test_case_set_is_nonempty_and_unique)
