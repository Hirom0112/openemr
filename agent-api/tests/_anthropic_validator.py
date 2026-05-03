"""Strict request-shape validator for Anthropic ``messages.create`` payloads.

This is a pure-function helper used by the dispatcher request-shape regression
suite (and any future test that wants to assert "the payload we shipped to
Anthropic would not have been rejected with HTTP 400").

It does NOT attempt to validate Anthropic's full API schema — only the
specific constraints whose violation produced real production bugs:

* tool_use → tool_result pairing (orphan tool_use kills the next request)
* cache_control accumulation across loop iterations (>4 blocks → 400)
* unknown block fields (e.g. legacy ``caller`` tag from a fast-path refactor)
* role alternation / first-message-must-be-user
* malformed system blocks
* empty content arrays

Usage::

    from tests._anthropic_validator import validate_anthropic_messages_create_kwargs
    violations = validate_anthropic_messages_create_kwargs(captured_kwargs)
    assert violations == []

The function is pure, deterministic, and side-effect-free.
"""

from __future__ import annotations

from typing import Any

__all__ = ["validate_anthropic_messages_create_kwargs"]


# Anthropic's documented per-block field allow-lists.  Keys outside these
# sets cause HTTP 400 invalid_request_error with messages like
# ``Unsupported field: caller``.
_ALLOWED_TEXT_FIELDS = {"type", "text", "cache_control"}
_ALLOWED_TOOL_USE_FIELDS = {"type", "id", "name", "input", "cache_control"}
_ALLOWED_TOOL_RESULT_FIELDS = {
    "type",
    "tool_use_id",
    "content",
    "is_error",
    "cache_control",
}

_MAX_CACHE_CONTROL_BLOCKS = 4


def _block_type(block: Any) -> str | None:
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    return btype if isinstance(btype, str) else None


def _has_cache_control(block: Any) -> bool:
    return isinstance(block, dict) and "cache_control" in block


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``message["content"]`` normalized to a list of dict blocks.

    Plain-string content is normalized to a single text block for iteration
    purposes — an empty list is returned for malformed content.
    """
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_use_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for block in _content_blocks(message):
        if block.get("type") == "tool_use":
            tu_id = block.get("id")
            if isinstance(tu_id, str):
                ids.append(tu_id)
    return ids


def _tool_result_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for block in _content_blocks(message):
        if block.get("type") == "tool_result":
            tr_id = block.get("tool_use_id")
            if isinstance(tr_id, str):
                ids.append(tr_id)
    return ids


def _validate_block_fields(
    block: dict[str, Any], path: str, violations: list[str]
) -> None:
    btype = block.get("type")
    if btype == "text":
        allowed = _ALLOWED_TEXT_FIELDS
    elif btype == "tool_use":
        allowed = _ALLOWED_TOOL_USE_FIELDS
    elif btype == "tool_result":
        allowed = _ALLOWED_TOOL_RESULT_FIELDS
    else:
        violations.append(f"{path}: unknown block type {btype!r}")
        return
    for key in block:
        if key not in allowed:
            violations.append(f"{path}: unknown field {key!r} on {btype} block")


def _validate_system(system: Any, violations: list[str]) -> int:
    """Validate the ``system`` kwarg shape and return its cache_control count."""
    if system is None:
        return 0
    if isinstance(system, str):
        return 0
    if not isinstance(system, list):
        violations.append(
            f"system: must be string or list of text blocks, got {type(system).__name__}"
        )
        return 0
    cc_count = 0
    for idx, block in enumerate(system):
        path = f"system.{idx}"
        if not isinstance(block, dict):
            violations.append(f"{path}: system block must be a dict")
            continue
        if block.get("type") != "text":
            violations.append(
                f"{path}: system block must have type='text', got {block.get('type')!r}"
            )
            continue
        if not isinstance(block.get("text"), str):
            violations.append(f"{path}: system block 'text' must be a string")
        for key in block:
            if key not in _ALLOWED_TEXT_FIELDS:
                violations.append(
                    f"{path}: unknown field {key!r} on system text block"
                )
        if _has_cache_control(block):
            cc_count += 1
    return cc_count


def validate_anthropic_messages_create_kwargs(kwargs: dict[str, Any]) -> list[str]:
    """Return a list of human-readable violation strings (empty if valid).

    The validator is intentionally narrow: it checks only the constraints
    whose violation has produced real production 400s in this codebase.  See
    the module docstring for the full list.
    """
    violations: list[str] = []

    cache_control_total = _validate_system(kwargs.get("system"), violations)

    messages = kwargs.get("messages")
    if not isinstance(messages, list) or not messages:
        violations.append("messages: must be a non-empty list")
        return violations

    # Per-message structural checks + first-message + alternation.
    prev_role: str | None = None
    for idx, msg in enumerate(messages):
        path = f"messages.{idx}"
        if not isinstance(msg, dict):
            violations.append(f"{path}: must be a dict")
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            violations.append(f"{path}: role must be 'user' or 'assistant', got {role!r}")
            continue
        if idx == 0 and role != "user":
            violations.append(
                f"{path}: first message must have role='user', got role={role!r}"
            )
        if prev_role is not None and prev_role == role:
            violations.append(
                f"{path}: role '{role}' repeats previous role — Anthropic requires alternation"
            )
        prev_role = role

        content = msg.get("content")
        if isinstance(content, list) and len(content) == 0:
            violations.append(f"{path}: content list is empty")
        if not isinstance(content, (str, list)):
            violations.append(
                f"{path}: content must be a string or list, got {type(content).__name__}"
            )
            continue

        # Per-block field allow-list + cache_control accounting.
        if isinstance(content, list):
            for bidx, block in enumerate(content):
                bpath = f"{path}.content.{bidx}"
                if not isinstance(block, dict):
                    violations.append(f"{bpath}: block must be a dict")
                    continue
                _validate_block_fields(block, bpath, violations)
                if _has_cache_control(block):
                    cache_control_total += 1

    # tool_use → tool_result pairing.  For every assistant message carrying
    # tool_use blocks, the very next message must be a user message whose
    # tool_result blocks cover every tool_use id.
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        tu_ids = _tool_use_ids(msg)
        if not tu_ids:
            continue
        next_msg = messages[idx + 1] if idx + 1 < len(messages) else None
        if next_msg is None or next_msg.get("role") != "user":
            for tu_id in tu_ids:
                violations.append(
                    f"messages.{idx}: assistant tool_use {tu_id!r} has no matching "
                    f"tool_result in next message"
                )
            continue
        tr_ids = set(_tool_result_ids(next_msg))
        for tu_id in tu_ids:
            if tu_id not in tr_ids:
                violations.append(
                    f"messages.{idx}: assistant tool_use {tu_id!r} has no matching "
                    f"tool_result in next message"
                )

    # Orphan tool_result: any user message containing tool_result blocks must
    # be immediately preceded by an assistant message whose tool_use ids cover
    # every tool_result id.
    for idx, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        tr_ids = _tool_result_ids(msg)
        if not tr_ids:
            continue
        prev_msg = messages[idx - 1] if idx > 0 else None
        if prev_msg is None or prev_msg.get("role") != "assistant":
            for tr_id in tr_ids:
                violations.append(
                    f"messages.{idx}: orphan tool_result {tr_id!r} — no assistant "
                    f"tool_use immediately precedes"
                )
            continue
        prev_tu_ids = set(_tool_use_ids(prev_msg))
        for tr_id in tr_ids:
            if tr_id not in prev_tu_ids:
                violations.append(
                    f"messages.{idx}: orphan tool_result {tr_id!r} — preceding "
                    f"assistant has no matching tool_use"
                )

    if cache_control_total > _MAX_CACHE_CONTROL_BLOCKS:
        violations.append(
            f"cache_control count is {cache_control_total}, max is "
            f"{_MAX_CACHE_CONTROL_BLOCKS}"
        )

    return violations
