"""Strict request-shape regression tests for dispatcher → Anthropic calls.

These tests do NOT contact the live API.  They mock
``dispatcher._anthropic.messages.create`` and run every captured call's
kwargs through :mod:`tests._anthropic_validator`, which checks the documented
constraints whose violation produced today's three production 400s:

* orphan ``tool_use`` in saved history (no following ``tool_result``)
* legacy ``caller`` field on saved blocks (unknown field → 400)
* ``cache_control`` accumulation across loop iterations (>4 blocks → 400)

Plus the related shape rules Anthropic enforces strictly: first message must
be ``user``, role alternation, no orphan ``tool_result``, allow-listed block
fields only, non-empty content arrays, and well-formed system blocks.

The existing stub-based dispatcher tests only assert call counts / return
values — they did NOT assert the request was well-formed, which is how the
three bugs above shipped.  This file closes that gap.
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

from tests._anthropic_validator import (  # noqa: E402
    validate_anthropic_messages_create_kwargs,
)


# ── Fake Anthropic responses ──────────────────────────────────────────────────


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _end_turn_response(text: str = "Done.") -> SimpleNamespace:
    text_block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(stop_reason="end_turn", content=[text_block], usage=_usage())


def _tool_use_response(tool_name: str, tool_id: str, tool_input: dict[str, Any] | None = None) -> SimpleNamespace:
    tool_use = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input=tool_input or {},
        id=tool_id,
    )
    return SimpleNamespace(stop_reason="tool_use", content=[tool_use], usage=_usage())


def _capture_create(*responses: SimpleNamespace) -> tuple[AsyncMock, list[dict[str, Any]]]:
    """Return an AsyncMock that captures kwargs and yields the canned responses."""
    captured: list[dict[str, Any]] = []
    response_iter = iter(responses)
    last_response = responses[-1] if responses else _end_turn_response()

    async def _side_effect(*args: Any, **kwargs: Any) -> SimpleNamespace:
        captured.append(kwargs)
        try:
            return next(response_iter)
        except StopIteration:
            return last_response

    return AsyncMock(side_effect=_side_effect), captured


def _patch_history(history: list[dict[str, Any]]):
    """Patch ``_load_history`` to return the given preloaded history."""

    async def _fake_load(session_id: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        return list(history)

    return patch.object(dispatcher, "_load_history", _fake_load)


def _silence_save():
    async def _noop(session_id: str, ctx: dict[str, Any], role: str, content: Any) -> None:
        return None

    return patch.object(dispatcher, "_save_turn", _noop)


def _normalize_kwargs_for_validation(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Mirror what the Anthropic SDK does on the wire: serialize content
    blocks (which the dispatcher may append as SimpleNamespace / pydantic
    objects in-memory) to plain dicts so validator field-allow-list checks
    are meaningful.  System blocks are already plain dicts.
    """
    normalized = dict(kwargs)
    new_messages: list[dict[str, Any]] = []
    for msg in kwargs.get("messages", []) or []:
        if not isinstance(msg, dict):
            new_messages.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, list):
            new_content: list[Any] = []
            for block in content:
                if isinstance(block, dict):
                    new_content.append(block)
                else:
                    new_content.append(dispatcher._blocks_to_dicts([block])[0])
            new_messages.append({**msg, "content": new_content})
        else:
            new_messages.append(msg)
    normalized["messages"] = new_messages
    return normalized


def _assert_all_calls_valid(captured: list[dict[str, Any]]) -> None:
    assert captured, "no Anthropic calls were captured"
    for idx, kwargs in enumerate(captured):
        normalized = _normalize_kwargs_for_validation(kwargs)
        violations = validate_anthropic_messages_create_kwargs(normalized)
        assert not violations, (
            f"call #{idx} produced validation violations:\n" + "\n".join(violations)
        )


# ── Regression tests ──────────────────────────────────────────────────────────


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_orphan_tool_use_in_history_is_repaired() -> None:
    """Preloaded history ends with assistant ``tool_use`` (no tool_result).
    The dispatcher must scrub this before sending — every captured call must
    validate clean (no orphan tool_use violations).
    """
    # Mid-stream + trailing orphan tool_use blocks.  Both must be scrubbed
    # so the sent payload contains no orphan tool_use violations.  The
    # mid-stream orphan is followed by another assistant text turn so the
    # dispatcher's sweep cleanly drops the orphan without leaving two
    # consecutive same-role messages behind.
    history = [
        {"role": "user", "content": "first question"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_mid_orphan", "name": "get_census_summary", "input": {}}
            ],
        },
        # Mid-stream orphan: dispatcher must drop the assistant tool_use above.
        {"role": "assistant", "content": [{"type": "text", "text": "answer 1"}]},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer 2"}]},
        # Trailing orphan: dispatcher must drop this trailing assistant tool_use.
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_trail_orphan", "name": "get_census_summary", "input": {}}
            ],
        },
    ]
    fake_create, captured = _capture_create(_end_turn_response("ok"))

    with _patch_history(history), _silence_save(), patch.object(
        dispatcher._anthropic.messages, "create", fake_create
    ):
        await dispatch(
            message="anything else",
            session_id="sess-orphan",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    _assert_all_calls_valid(captured)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_legacy_caller_field_is_stripped() -> None:
    """Preloaded history has assistant blocks with the legacy ``caller`` field.
    The dispatcher's sanitizer must strip it before sending — no captured
    block may carry ``caller``.
    """
    history = [
        {"role": "user", "content": "brief Marcus"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_legacy_1",
                    "name": "get_patient_briefing",
                    "input": {"patient_id": "pt-001"},
                    "caller": {"type": "direct"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_legacy_1",
                    "content": "{\"patient_id\": \"pt-001\"}",
                    "caller": {"type": "direct"},
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "briefed.", "caller": {"type": "direct"}}
            ],
        },
    ]
    fake_create, captured = _capture_create(_end_turn_response("ok"))

    with _patch_history(history), _silence_save(), patch.object(
        dispatcher._anthropic.messages, "create", fake_create
    ):
        await dispatch(
            message="follow up",
            session_id="sess-caller",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    _assert_all_calls_valid(captured)
    # Belt-and-braces: scan every captured payload for the literal field name.
    for idx, kwargs in enumerate(captured):
        for midx, msg in enumerate(kwargs.get("messages", [])):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for bidx, block in enumerate(content):
                assert "caller" not in block, (
                    f"call #{idx} messages.{midx}.content.{bidx} still has 'caller': {block!r}"
                )


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_cache_control_does_not_overflow_in_tool_loop() -> None:
    """Multi-iteration tool loop must keep cache_control ≤ 4 across iterations.

    Earlier bug: each loop iteration appended a ``cache_control`` block to a
    new message without clearing the previous one — by turn 3 the payload
    carried 6+ ``cache_control`` blocks and Anthropic rejected with 400.
    """
    fake_tool_a = AsyncMock(return_value={"result": {"answer": "a"}, "citations": []})
    fake_tool_b = AsyncMock(return_value={"result": {"answer": "b"}, "citations": []})

    fake_create, captured = _capture_create(
        _tool_use_response("query_patient_records", "tu_loop_1", {"patient_id": "pt-001", "query": "q1"}),
        _tool_use_response("query_patient_records", "tu_loop_2", {"patient_id": "pt-001", "query": "q2"}),
        _end_turn_response("answered."),
    )

    with _patch_history([]), _silence_save(), patch.object(
        dispatcher._anthropic.messages, "create", fake_create
    ), patch.dict(
        dispatcher.TOOL_REGISTRY,
        {"query_patient_records": fake_tool_a},
        clear=False,
    ):
        # Second iteration's tool call routes through the same registry entry.
        fake_tool_a.side_effect = [
            {"result": {"answer": "a"}, "citations": []},
            {"result": {"answer": "b"}, "citations": []},
        ]
        await dispatch(
            message="what was the potassium?",
            session_id="sess-cc-loop",
            session_context={"provider_id": "prov-1", "patient_ids": ["pt-001"]},
        )

    assert len(captured) >= 2, (
        f"expected the loop to make ≥2 Anthropic calls, got {len(captured)}"
    )
    _assert_all_calls_valid(captured)

    # Explicit cache_control budget assertion — the failure mode this guards
    # is "validator passes for call #1 but call #N has 5+ cache_control blocks".
    for idx, kwargs in enumerate(captured):
        cc = 0
        for sblock in kwargs.get("system", []) or []:
            if isinstance(sblock, dict) and "cache_control" in sblock:
                cc += 1
        for msg in kwargs.get("messages", []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    cc += 1
        assert cc <= 4, f"call #{idx} has {cc} cache_control blocks (max 4)"


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_first_message_is_user_after_history_load() -> None:
    """If the loaded history starts with an assistant turn, the dispatcher's
    trim step must drop it so the sent payload still starts with a user turn.
    """
    history = [
        {"role": "assistant", "content": [{"type": "text", "text": "leftover"}]},
        {"role": "user", "content": "real question"},
        {"role": "assistant", "content": [{"type": "text", "text": "real answer"}]},
    ]
    fake_create, captured = _capture_create(_end_turn_response("ok"))

    with _patch_history(history), _silence_save(), patch.object(
        dispatcher._anthropic.messages, "create", fake_create
    ):
        await dispatch(
            message="next thing",
            session_id="sess-first-user",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    _assert_all_calls_valid(captured)
    for idx, kwargs in enumerate(captured):
        msgs = kwargs.get("messages", [])
        assert msgs and msgs[0].get("role") == "user", (
            f"call #{idx}: first message role is {msgs[0].get('role') if msgs else None!r}"
        )


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_no_role_alternation_violation() -> None:
    """Corrupt history with two consecutive assistant turns must be repaired
    so the sent payload alternates roles cleanly.
    """
    # Corrupt state: assistant with an orphan tool_use immediately followed
    # by another assistant text turn.  The dispatcher's mid-stream orphan
    # sweep must drop the first one, leaving a clean alternation.
    history = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_corrupt", "name": "get_census_summary", "input": {}}
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "a2 — corrupt second assistant"}]},
    ]
    fake_create, captured = _capture_create(_end_turn_response("ok"))

    with _patch_history(history), _silence_save(), patch.object(
        dispatcher._anthropic.messages, "create", fake_create
    ):
        await dispatch(
            message="next",
            session_id="sess-alt",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    _assert_all_calls_valid(captured)


@pytest.mark.hard_failure
@pytest.mark.asyncio
async def test_no_unknown_block_fields() -> None:
    """Preloaded history with several junk fields on various block types must
    be sanitized before sending.  Validator catches any leftovers.
    """
    # Junk fields on every block type.  The dispatcher's sanitizer must
    # strip them before sending.  History ends with assistant so that
    # appending the new user keeps clean alternation.
    history = [
        {"role": "user", "content": "kickoff"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "fine",
                    "junk_text_field": True,
                    "another_unknown": [1, 2, 3],
                }
            ],
        },
        {
            "role": "user",
            "content": "another",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_junk",
                    "name": "get_census_summary",
                    "input": {},
                    "caller": {"type": "direct"},
                    "extra": "nope",
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_junk",
                    "content": "{}",
                    "weird": 42,
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "all done",
                    "yet_another_junk": "x",
                }
            ],
        },
    ]
    fake_create, captured = _capture_create(_end_turn_response("ok"))

    with _patch_history(history), _silence_save(), patch.object(
        dispatcher._anthropic.messages, "create", fake_create
    ):
        await dispatch(
            message="continue",
            session_id="sess-junk",
            session_context={"provider_id": "prov-1", "patient_ids": []},
        )

    _assert_all_calls_valid(captured)
