"""Dispatcher — tool_use / tool_result agentic loop.

Single entry point for all conversational physician queries:
    POST /agent/query → dispatch(message, session_id, session_context, checkpointer)

Design constraints (from ARCHITECTURE.md §4 and CLAUDE.md):
- cache_control: {"type": "ephemeral"} on system prompt (block 1) and census
  context (block 2).  Pass criterion: cache-hit input tokens ≥70% of total
  input tokens for UC-2/3/4 calls within a session.
- Max 5 tool-use turns before returning an UNKNOWN error.
- One self-correction attempt on misroute detection.
- Langfuse spans on every dispatch call: parent + per-tool child + generation.
- Conversation history loaded from checkpointer at start, saved at end.
- Final-response verification applied before return (Phase 5 will add
  dispatcher_response.py; until then domain_constraints + source_attribution
  are called here directly).
"""

from __future__ import annotations  # enables X | Y union syntax on Python 3.9

import asyncio
import enum
import json
import logging
import time
from typing import Any

import anthropic

from agent.schemas import DISPATCHER_TOOLS
from agent.system_prompt import build_system_prompt
from agent.tool_registry import TOOL_REGISTRY
from config import settings
from verification.dispatcher_response import verify_dispatcher_response
from verification.domain_constraints import verify_conversation_answer

logger = logging.getLogger(__name__)

_anthropic = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

MAX_TOOL_TURNS = 5
_MODEL = getattr(settings, "anthropic_model", "claude-sonnet-4-6")


# ── Error contract ────────────────────────────────────────────────────────────

class ToolFailureClass(enum.Enum):
    FHIR_UNAVAILABLE = "fhir_unavailable"
    TOOL_VALIDATION = "tool_validation"
    LLM_TIMEOUT = "llm_timeout"
    VERIFICATION_BLOCK = "verification_block"
    UNKNOWN = "unknown"


PHYSICIAN_ERROR_MESSAGES: dict[ToolFailureClass, str] = {
    ToolFailureClass.FHIR_UNAVAILABLE: "Patient data temporarily unavailable. Please view the chart directly.",
    ToolFailureClass.TOOL_VALIDATION: "I couldn't process that request. Please rephrase.",
    ToolFailureClass.LLM_TIMEOUT: "Response timed out. Please try again.",
    ToolFailureClass.VERIFICATION_BLOCK: "This response was blocked by a safety check. Please view the chart directly.",
    ToolFailureClass.UNKNOWN: "An unexpected error occurred. Please view the chart directly.",
}


def _error_response(failure_class: ToolFailureClass, detail: str = "") -> dict[str, Any]:
    return {
        "type": "error",
        "data": None,
        "narrative": PHYSICIAN_ERROR_MESSAGES[failure_class],
        "citations": [],
        "metadata": {
            "error": True,
            "failure_class": failure_class.value,
            "detail": detail,
        },
    }


# ── Misroute detection ────────────────────────────────────────────────────────

_TOOL_INTENT_HINTS: dict[str, list[str]] = {
    "get_census_summary": ["census", "triage", "list", "all patients", "morning", "patients"],
    "get_patient_briefing": ["briefing", "brief me", "tell me about", "summary of", "before i see"],
    "query_patient_records": ["what is", "what was", "what did", "show me", "potassium", "echo", "labs", "vitals", "why is"],
    "get_medication_safety": ["allerg", "medication", "drug", "interaction", "safe", "lasix", "med"],
    "generate_handoff": ["handoff", "hand off", "sign out", "sign-out"],
}


def _detect_misroute(message: str, tool_called: str) -> bool:
    msg_lower = message.lower()
    for tool_name, hints in _TOOL_INTENT_HINTS.items():
        if tool_name == tool_called:
            continue
        if any(h in msg_lower for h in hints):
            expected_hints = _TOOL_INTENT_HINTS.get(tool_called, [])
            if not any(h in msg_lower for h in expected_hints):
                return True
    return False


# ── Checkpointer helpers ──────────────────────────────────────────────────────

async def _load_history(session_id: str, session_context: dict[str, Any]) -> list[dict[str, Any]]:
    redis_saver = session_context.get("redis_saver")
    sqlite_saver = session_context.get("sqlite_saver")
    if redis_saver is not None:
        try:
            return await redis_saver.load(session_id)
        except Exception as exc:
            logger.warning("Redis load failed, falling back to SQLite", extra={"error": str(exc)})
    if sqlite_saver is not None:
        return await sqlite_saver.load(session_id)
    return []


async def _save_turn(
    session_id: str,
    session_context: dict[str, Any],
    role: str,
    content: str,
) -> None:
    redis_saver = session_context.get("redis_saver")
    sqlite_saver = session_context.get("sqlite_saver")
    try:
        if redis_saver is not None:
            await redis_saver.append(session_id, role=role, content=content)
            return
    except Exception as exc:
        logger.warning("Redis save failed, falling back to SQLite", extra={"error": str(exc)})
    if sqlite_saver is not None:
        await sqlite_saver.append(session_id, role=role, content=content)


# ── System blocks with cache_control ─────────────────────────────────────────

def _build_system_blocks(session_context: dict[str, Any]) -> list[dict[str, Any]]:
    provider_name = session_context.get("provider_name", "Provider")
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": build_system_prompt(provider_name),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    census_context = session_context.get("census_context")
    if census_context:
        blocks.append(
            {
                "type": "text",
                "text": census_context,
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks


# ── History → messages ────────────────────────────────────────────────────────

def _history_to_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert stored turns to Anthropic messages format.

    Stored turns have {role, content}; tool result turns have
    {role: "tool", content, tool_use_id}.  The Anthropic API expects
    tool results as user messages with content type "tool_result".
    """
    messages: list[dict[str, Any]] = []
    for turn in history:
        role = turn["role"]
        content = turn["content"]
        if role == "tool":
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": turn.get("tool_use_id", "unknown"),
                        "content": content,
                    }
                ],
            })
        else:
            messages.append({"role": role, "content": content})
    return messages


# ── Main dispatcher ───────────────────────────────────────────────────────────

async def dispatch(
    message: str,
    session_id: str,
    session_context: dict[str, Any],
    checkpointer: Any | None = None,
) -> dict[str, Any]:
    """Run the tool_use / tool_result loop and return a typed response envelope.

    Parameters
    ----------
    message:         The physician's natural-language message.
    session_id:      Stable identifier for the conversation session.
    session_context: Dict with provider_id, patient_ids, provider_name,
                     optional census_context, redis_client, langfuse, etc.
    checkpointer:    Unused in the call signature (kept for interface compat);
                     savers are read from session_context.
    """
    langfuse = session_context.get("langfuse")
    t_start = time.monotonic()

    dispatch_span = None
    if langfuse is not None:
        dispatch_span = langfuse.span(
            name="dispatch",
            input={"message": message, "session_id": session_id},
            metadata={"provider_id": session_context.get("provider_id")},
        )

    # Load conversation history
    history = await _load_history(session_id, session_context)
    await _save_turn(session_id, session_context, "user", message)

    messages = _history_to_messages(history)
    messages.append({"role": "user", "content": message})

    system_blocks = _build_system_blocks(session_context)

    all_citations: list[dict[str, Any]] = []
    misroute_detected = False
    self_corrected = False
    turn_count = 0
    correction_attempted = False
    final_narrative: str = ""
    final_data: dict[str, Any] | None = None
    response_type: str = "text"

    try:
        while turn_count < MAX_TOOL_TURNS:
            turn_count += 1

            generation_event = None
            if langfuse is not None:
                generation_event = langfuse.generation(
                    name=f"llm_call_turn_{turn_count}",
                    model=_MODEL,
                    input=messages,
                )

            response = await _anthropic.messages.create(
                model=_MODEL,
                max_tokens=4096,
                system=system_blocks,
                messages=messages,
                tools=DISPATCHER_TOOLS,
            )

            if generation_event is not None:
                generation_event.end(
                    output=response.content,
                    usage={
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                    },
                )

            if response.stop_reason == "max_tokens":
                logger.warning("LLM hit max_tokens", extra={"session_id": session_id})
                _finalize_span(dispatch_span, error=True)
                return _error_response(ToolFailureClass.LLM_TIMEOUT, "max_tokens exceeded")

            if response.stop_reason == "end_turn":
                # Extract text narrative from response
                for block in response.content:
                    if hasattr(block, "text"):
                        final_narrative += block.text
                break

            if response.stop_reason == "tool_use":
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                if not tool_use_blocks:
                    break

                # Append assistant turn with tool_use block
                messages.append({"role": "assistant", "content": response.content})

                tool_results: list[dict[str, Any]] = []
                for tool_use_block in tool_use_blocks:
                    tool_name: str = tool_use_block.name
                    tool_input: dict[str, Any] = tool_use_block.input
                    tool_use_id: str = tool_use_block.id

                    # Misroute detection (first tool call only, no correction yet attempted)
                    if turn_count == 1 and not correction_attempted:
                        if _detect_misroute(message, tool_name):
                            misroute_detected = True
                            logger.warning(
                                "Misroute detected",
                                extra={"session_id": session_id, "tool": tool_name, "physician_query": message[:80]},
                            )

                    # Census scope enforcement — block FHIR tool calls for out-of-census patients.
                    # Compensating control for AUDIT finding #6 (ARCHITECTURE.md §6.1).
                    census_ids: list[str] = session_context.get("patient_ids", [])
                    requested_pid: str | None = tool_input.get("patient_id")
                    if (
                        requested_pid is not None
                        and census_ids
                        and requested_pid not in census_ids
                    ):
                        logger.warning(
                            "Census scope violation blocked",
                            extra={"session_id": session_id, "requested": requested_pid},
                        )
                        tool_result_content = json.dumps({
                            "error": "Patient not on active census. Please confirm patient identity before accessing records.",
                            "scope_enforcement": True,
                            "requested_patient_id": requested_pid,
                        })
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": tool_result_content,
                        })
                        continue

                    # Call tool
                    tool_fn = TOOL_REGISTRY.get(tool_name)
                    if tool_fn is None:
                        tool_result_content = json.dumps({"error": f"Unknown tool: {tool_name}"})
                    else:
                        tool_span = None
                        if langfuse is not None:
                            tool_span = langfuse.span(
                                name=f"tool_{tool_name}",
                                input=tool_input,
                                metadata={"tool_use_id": tool_use_id},
                            )

                        try:
                            tool_result = await _call_tool_with_retry(tool_fn, tool_input, session_context, tool_name)
                            all_citations.extend(tool_result.get("citations", []))
                            result_data = tool_result.get("result", {})
                            tool_result_content = json.dumps(result_data)

                            # Capture structured data for response envelope
                            if final_data is None:
                                final_data = result_data
                                response_type = _infer_response_type(tool_name)

                            if tool_span is not None:
                                tool_span.end(output=result_data)

                        except Exception as exc:
                            failure_class = _classify_failure(exc)
                            tool_result_content = json.dumps(
                                {"error": PHYSICIAN_ERROR_MESSAGES[failure_class]}
                            )
                            if tool_span is not None:
                                tool_span.end(output={"error": str(exc)}, level="ERROR")
                            logger.error(
                                "Tool call failed",
                                extra={"tool": tool_name, "error": str(exc), "class": failure_class.value},
                            )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": tool_result_content,
                        }
                    )

                messages.append({"role": "user", "content": tool_results})

                # Self-correction: if misroute detected and not yet corrected, inject hint
                if misroute_detected and not correction_attempted:
                    correction_attempted = True
                    self_corrected = True
                    correction_hint = (
                        "The previous tool selection may not match the physician's intent. "
                        "Please re-read the original question and select the most appropriate tool."
                    )
                    messages.append({"role": "user", "content": correction_hint})
                    logger.info("Self-correction injected", extra={"session_id": session_id})

                continue

            # Unexpected stop_reason
            break

        else:
            # Exceeded MAX_TOOL_TURNS
            logger.error("Exceeded MAX_TOOL_TURNS", extra={"session_id": session_id, "turns": turn_count})
            _finalize_span(dispatch_span, error=True)
            return _error_response(ToolFailureClass.UNKNOWN, f"exceeded {MAX_TOOL_TURNS} tool turns")

        # Apply final-response verification (all 7 hard rules)
        active_patient_id: str | None = None
        if session_context.get("patient_ids"):
            active_patient_id = session_context["patient_ids"][0]
        elif session_context.get("last_viewed_patient_id"):
            active_patient_id = session_context["last_viewed_patient_id"]

        pre_verify_response: dict[str, Any] = {
            "type": response_type,
            "data": final_data,
            "narrative": final_narrative,
            "citations": all_citations,
            "metadata": {},
        }
        fhir_context = session_context.get("fhir_context", {})
        verification_result = verify_dispatcher_response(
            pre_verify_response, fhir_context, active_patient_id
        )

        if verification_result.blocked:
            logger.error(
                "Dispatcher response blocked by verification",
                extra={"session_id": session_id, "violations": verification_result.violations},
            )
            _finalize_span(dispatch_span, error=True)
            return {
                "type": "error",
                "data": None,
                "narrative": verification_result.physician_message,
                "citations": [],
                "metadata": {"verification_blocked": True},
            }

        verified_response = verification_result.modified_response
        final_narrative = verified_response.get("narrative", final_narrative)

        # Save assistant response to history
        await _save_turn(session_id, session_context, "assistant", final_narrative or json.dumps(final_data or {}))

        duration_ms = int((time.monotonic() - t_start) * 1000)

        _finalize_span(
            dispatch_span,
            error=False,
            output={"type": response_type, "duration_ms": duration_ms},
            metadata={
                "misroute_detected": misroute_detected,
                "self_corrected": self_corrected,
                "verification_violations": verification_result.violations,
            },
        )

        return {
            "type": response_type,
            "data": final_data,
            "narrative": final_narrative,
            "citations": all_citations,
            "metadata": {
                "session_id": session_id,
                "duration_ms": duration_ms,
                "turn_count": turn_count,
                "misroute_detected": misroute_detected,
                "self_corrected": self_corrected,
                "verification_violations": verification_result.violations,
            },
        }

    except Exception as exc:
        logger.error("Dispatcher error", extra={"session_id": session_id, "error": str(exc)})
        _finalize_span(dispatch_span, error=True)
        return _error_response(ToolFailureClass.UNKNOWN, str(exc))


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _call_tool_with_retry(
    tool_fn: Any,
    tool_input: dict[str, Any],
    session_context: dict[str, Any],
    tool_name: str,
) -> dict[str, Any]:
    """Call a tool with retry logic per the error contract."""
    max_retries = 2 if "fhir" in tool_name.lower() or tool_name == "get_census_summary" else 1
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await tool_fn(tool_input, session_context)
        except TimeoutError as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0)
        except Exception as exc:
            raise exc
    raise last_exc or RuntimeError(f"Tool {tool_name} failed after {max_retries} attempts")


def _classify_failure(exc: Exception) -> ToolFailureClass:
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return ToolFailureClass.LLM_TIMEOUT
    if "fhir" in msg or "502" in msg or "upstream" in msg or "connection" in msg:
        return ToolFailureClass.FHIR_UNAVAILABLE
    if "validation" in msg or "invalid input" in msg:
        return ToolFailureClass.TOOL_VALIDATION
    if "verification" in msg or "safety" in msg or "blocked" in msg:
        return ToolFailureClass.VERIFICATION_BLOCK
    return ToolFailureClass.UNKNOWN


def _infer_response_type(tool_name: str) -> str:
    return {
        "get_census_summary": "census",
        "get_patient_briefing": "briefing",
        "query_patient_records": "query_answer",
        "get_medication_safety": "medication_safety",
        "generate_handoff": "handoff",
    }.get(tool_name, "text")


def _finalize_span(
    span: Any | None,
    *,
    error: bool,
    output: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if span is None:
        return
    try:
        kwargs: dict[str, Any] = {"level": "ERROR" if error else "DEFAULT"}
        if output:
            kwargs["output"] = output
        if metadata:
            kwargs["metadata"] = metadata
        span.end(**kwargs)
    except Exception:
        pass
