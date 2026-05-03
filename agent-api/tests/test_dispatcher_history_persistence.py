"""Tests for conversation-history truncation and placeholder enrichment.

These tests pin down the regressions described in the
clinical-copilot incident where the agent forgot a patient briefed at the
start of the session ("can she have tylenol?" returned a generic census
list instead of resolving to the briefed patient).

Coverage:

* ``_truncate_history_if_needed`` produces a valid Anthropic message
  sequence — no orphan ``tool_result`` at the start, no orphan trailing
  assistant ``tool_use``.
* Truncation preserves earlier briefing / medication_safety tool turns
  (context-bearing) so pronoun references stay resolvable.
* Fast-path placeholder narrative includes patient name and key facts.
* Structured-skip census placeholder uses the correct ``census`` key.
* End-to-end: a briefing turn followed by an ambiguous follow-up loads
  history that contains the patient identity in a form the LLM can read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import dispatcher  # noqa: E402
from agent.dispatcher import (  # noqa: E402
    _briefing_identity_summary,
    _structured_skip_narrative,
    _truncate_history_if_needed,
    dispatch,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user_text(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant_tool_use(tool_name: str, tool_use_id: str, payload_input: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": payload_input or {},
            }
        ],
    }


def _user_tool_result(tool_use_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            }
        ],
    }


def _assistant_text(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


# ── Truncation: valid sequence ────────────────────────────────────────────────


@pytest.mark.hard_failure
def test_truncation_keeps_valid_anthropic_sequence_no_orphan_tool_result() -> None:
    """The kept slice must start with a real user text turn, never with a
    ``tool_result`` block (Anthropic rejects orphan tool_results).
    """
    # Build a long conversation: briefing + handoff with a huge tool_result
    # blob so we exceed the token-budget threshold.
    huge_blob = "x" * (dispatcher._MAX_HISTORY_TOKENS_EST * 5)  # > budget on its own
    messages: list[dict[str, Any]] = []
    messages.append(_user_text("brief Yvonne Castillo"))
    messages.append(_assistant_tool_use("get_patient_briefing", "tu_brief_1", {"patient_id": "pt-008"}))
    messages.append(_user_tool_result("tu_brief_1", json.dumps({"patient_id": "pt-008", "patient_name": "Yvonne Castillo"})))
    messages.append(_assistant_text("Briefing generated for Yvonne Castillo (patient_id=pt-008)."))
    # Pad with several normal turns
    for i in range(8):
        messages.append(_user_text(f"another question {i}"))
        messages.append(_assistant_text(f"answer {i}"))
    # Now the heavy handoff sequence
    messages.append(_user_text("handoff"))
    messages.append(_assistant_tool_use("generate_handoff", "tu_handoff_1"))
    messages.append(_user_tool_result("tu_handoff_1", huge_blob))
    messages.append(_assistant_text("Handoff generated for 10 patients."))
    messages.append(_user_text("can she have tylenol?"))

    truncated = _truncate_history_if_needed(messages, "sess-trunc-1")

    # Truncation must have fired.
    assert len(truncated) < len(messages), "Truncation did not fire on > budget history"

    # First message must be a user turn whose content is either a string or
    # a list of non-tool_result blocks.
    first = truncated[0]
    assert first.get("role") == "user", f"first message role={first.get('role')}"
    content = first.get("content")
    if isinstance(content, list):
        for block in content:
            assert block.get("type") != "tool_result", (
                "Kept slice starts with an orphan tool_result block — Anthropic will reject"
            )

    # The trailing assistant turn (if any) must not be a bare tool_use without
    # a matching following tool_result.
    last = truncated[-1]
    if last.get("role") == "assistant" and isinstance(last.get("content"), list):
        for block in last["content"]:
            assert block.get("type") != "tool_use", (
                "Kept slice ends with an orphan assistant tool_use — invalid sequence"
            )


@pytest.mark.hard_failure
def test_truncation_preserves_context_bearing_briefing_pair() -> None:
    """When a briefing tool_use+tool_result pair falls outside the recent
    window, truncation must preserve it so the LLM can still resolve "she".
    """
    huge_blob = "z" * (dispatcher._MAX_HISTORY_TOKENS_EST * 5)
    messages: list[dict[str, Any]] = []
    # Briefing pair early — these should be preserved.
    messages.append(_user_text("brief Yvonne Castillo"))
    messages.append(_assistant_tool_use("get_patient_briefing", "tu_brief_1", {"patient_id": "pt-008"}))
    briefing_payload = json.dumps({"patient_id": "pt-008", "patient_name": "Yvonne Castillo"})
    messages.append(_user_tool_result("tu_brief_1", briefing_payload))
    messages.append(_assistant_text("Briefing generated for Yvonne Castillo (patient_id=pt-008)."))
    # Lots of intervening filler
    for i in range(20):
        messages.append(_user_text(f"unrelated chitchat {i}"))
        messages.append(_assistant_text(f"reply {i}"))
    # Heavy handoff to push us over the budget
    messages.append(_user_text("handoff"))
    messages.append(_assistant_tool_use("generate_handoff", "tu_handoff_1"))
    messages.append(_user_tool_result("tu_handoff_1", huge_blob))
    messages.append(_assistant_text("Handoff generated for 10 patients."))
    messages.append(_user_text("can she have tylenol?"))

    truncated = _truncate_history_if_needed(messages, "sess-trunc-2")
    # Render to JSON and check the briefing context survives somewhere in
    # the kept slice.
    serialized = json.dumps(truncated)
    assert "Yvonne Castillo" in serialized, (
        "Briefing context (patient name) was dropped during truncation — "
        "pronoun resolution will fail"
    )
    assert "tu_brief_1" in serialized, (
        "Briefing tool_use_id was not preserved — context-bearing pair pruning regressed"
    )


@pytest.mark.hard_failure
def test_truncation_under_budget_returns_unchanged() -> None:
    """Small histories should pass through unchanged."""
    messages = [_user_text("hi"), _assistant_text("hello")]
    out = _truncate_history_if_needed(messages, "sess-small")
    assert out == messages


# ── Placeholder enrichment ────────────────────────────────────────────────────


@pytest.mark.hard_failure
def test_briefing_placeholder_includes_patient_name() -> None:
    """Fast-path / structured-skip placeholder must include patient name and
    a few facts so follow-up turns can resolve "she" / "him" / "this patient".
    """
    data = {
        "patient_id": "pt-008",
        "patient_name": "Yvonne Castillo",
        "alerts": ["alert one", "alert two"],
        "active_medications": [{"name": "lisinopril"}, {"name": "metformin"}],
        "code_status": "Full Code",
    }
    summary = _briefing_identity_summary(data)
    assert "Yvonne Castillo" in summary
    assert "pt-008" in summary
    # Key facts surfaced.
    assert "active alerts" in summary
    assert "active medications" in summary

    # Drives the structured-skip narrative end-to-end.
    narrative = _structured_skip_narrative("briefing", data)
    assert "Yvonne Castillo" in narrative


@pytest.mark.hard_failure
def test_briefing_placeholder_falls_back_to_id_when_name_missing() -> None:
    """No name in the briefing payload → still emit a usable identifier."""
    summary = _briefing_identity_summary({"patient_id": "pt-008"})
    assert "pt-008" in summary
    # Still a non-empty placeholder.
    assert summary.startswith("Briefing generated for")


@pytest.mark.hard_failure
def test_census_placeholder_counts_with_correct_key() -> None:
    """The census tool returns its patient list under the ``census`` key, not
    ``patients``.  The structured-skip narrative must read the right key.
    """
    data = {"census": [{"patient_id": "pt-001"}, {"patient_id": "pt-002"}, {"patient_id": "pt-003"}]}
    narrative = _structured_skip_narrative("census", data)
    assert "3 patients" in narrative, (
        f"Census placeholder counted wrong, got: {narrative!r}"
    )


@pytest.mark.hard_failure
def test_census_placeholder_legacy_key_still_works() -> None:
    """Older fixtures use ``patients`` — keep that path alive for compat."""
    data = {"patients": [{"patient_id": "pt-001"}, {"patient_id": "pt-002"}]}
    narrative = _structured_skip_narrative("census", data)
    assert "2 patients" in narrative


# ── End-to-end: fast-path persists rich identity into history ─────────────────


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_fast_path_saves_placeholder_with_patient_name() -> None:
    """After a fast-path briefing for Yvonne, the saved assistant turn must
    contain the patient name so the next turn's history load surfaces it
    in the message stream sent to Anthropic.
    """
    saved_turns: list[tuple[str, Any]] = []

    async def _capture_save(session_id: str, ctx: dict[str, Any], role: str, content: Any) -> None:
        saved_turns.append((role, content))

    fake_create = AsyncMock()
    fake_census = AsyncMock(
        return_value={
            "result": {
                "census": [{"patient_id": "pt-008", "name": "Yvonne Castillo"}],
                "total": 1,
            },
            "citations": [],
            "metadata": {},
        }
    )
    fake_briefing = AsyncMock(
        return_value={
            "result": {
                "patient_id": "pt-008",
                "patient_name": "Yvonne Castillo",
                "alerts": ["needs follow-up"],
            },
            "citations": [],
            "metadata": {"tool": "get_patient_briefing"},
        }
    )

    with patch.object(dispatcher._anthropic.messages, "create", fake_create), \
         patch.object(dispatcher, "_save_turn", _capture_save), \
         patch.dict(
             dispatcher.TOOL_REGISTRY,
             {
                 "get_census_summary": fake_census,
                 "get_patient_briefing": fake_briefing,
             },
             clear=False,
         ):
        result = await dispatch(
            message="brief Yvonne Castillo",
            session_id="sess-fp-yvonne",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    assert fake_create.await_count == 0, "Fast path must not call Anthropic"
    assert result["type"] == "briefing"
    assert "Yvonne Castillo" in result["narrative"], (
        f"Fast-path narrative dropped patient name: {result['narrative']!r}"
    )
    # Confirm the assistant turn we'll persist into history contains the name.
    assistant_saves = [c for r, c in saved_turns if r == "assistant"]
    assert assistant_saves, "Fast path did not save an assistant turn"
    # content is a list of blocks; the text block should contain the name.
    payload_text = json.dumps(assistant_saves[-1])
    assert "Yvonne Castillo" in payload_text, (
        "Fast-path saved assistant turn lacks the patient name — follow-up "
        "turns will not be able to resolve pronouns"
    )
