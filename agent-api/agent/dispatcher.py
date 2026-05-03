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

import re

from agent.metrics import (
    agent_checkpointer_op_duration_seconds,
    agent_checkpointer_ops_total,
    agent_dispatch_latency_seconds,
    agent_fast_path_hits_total,
    agent_prompt_cache_hits_total,
    agent_prompt_cache_misses_total,
    agent_tool_calls_total,
    agent_tool_misroute_total,
)
from agent.schemas import DISPATCHER_TOOLS
from agent.system_prompt import build_system_prompt
from agent.tool_registry import TOOL_REGISTRY
from audit.openemr_log import emit_audit_event
from config import settings
from verification.dispatcher_response import verify_dispatcher_response
from verification.domain_constraints import verify_conversation_answer

logger = logging.getLogger(__name__)

_anthropic = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

MAX_TOOL_TURNS = 5
_MODEL = getattr(settings, "anthropic_model", "claude-sonnet-4-6")

# Response types whose frontend renderers consume `response.data` and ignore
# the narrative.  For these we can skip the second (framing) Anthropic call
# entirely — a ~1.5–2 s latency win per turn — because the renderer never
# displays the narrative anyway.  Free-text response types (`text`,
# `query_answer`) still need the framing pass and are NOT included.
#
# Keep this set in sync with the structured renderer types in
# `agent-ui/src/types.ts` (ResponseType union) and the tool→response_type map
# in `_infer_response_type` below.
_STRUCTURED_RESPONSE_TYPES: frozenset[str] = frozenset({
    "census",
    "briefing",
    "medication_safety",
    "handoff",
})


# Canary phrases mandated verbatim by ``agent/system_prompt.py`` lines 143 and
# 146.  Kept as module-level constants so a repo-wide grep finds both the
# prompt rule and the runtime emission point.  When we skip the framing turn
# for structured-renderer types we lose the LLM's chance to surface these
# phrases in the narrative — so we synthesize them here from the structured
# data so they reach conversation history (and any future text-mode follow-up)
# in addition to the structured ``alerts`` array.
_CANARY_BLANK_ALLERGIES = "Allergy data is incomplete — verify in the chart."
_CANARY_BLANK_CODE_STATUS = "Code status not documented — verify before orders."


def _briefing_canaries(data: dict[str, Any]) -> tuple[bool, bool]:
    """Detect blank-allergies / blank-code-status flags in a briefing payload.

    Briefing alerts are emitted as strings (see ``briefing/generator.py``)
    using the ``BLANK_ALLERGY_SECTION`` / ``BLANK_CODE_STATUS`` tokens, but
    the LLM-shaped briefing may use plain English instead.  Match either.
    """
    alerts = data.get("alerts") or []
    blank_allergies = False
    blank_code_status = False
    for alert in alerts:
        if not isinstance(alert, str):
            continue
        lowered = alert.lower()
        if "blank_allergy" in lowered or "allerg" in lowered and (
            "blank" in lowered or "incomplete" in lowered or "empty" in lowered or "missing" in lowered
        ):
            blank_allergies = True
        if "blank_code_status" in lowered or "code status" in lowered and (
            "blank" in lowered or "not documented" in lowered or "missing" in lowered or "empty" in lowered
        ):
            blank_code_status = True
    return blank_allergies, blank_code_status


def _medication_safety_canaries(data: dict[str, Any]) -> tuple[bool, bool]:
    """Detect blank-allergies in a medication_safety payload.

    Medication safety responses do not surface code status, so only the
    allergy canary is in scope here.  An empty allergies list is treated
    as "data incomplete" because medication-vs-allergy checks cannot be
    asserted as safe with zero allergy entries.
    """
    if "allergies" not in data:
        return False, False
    allergies = data.get("allergies")
    if allergies is None or (isinstance(allergies, list) and len(allergies) == 0):
        return True, False
    return False, False


def _handoff_canaries(data: dict[str, Any]) -> tuple[bool, bool]:
    """Detect blank-allergies / blank-code-status across handoff patients.

    Aggregates per-patient blocks: if any patient block carries a blank
    flag (via an ``alerts`` list or explicit blank fields), surface the
    canary.  Handoff payload shapes vary across generator paths so we
    match defensively on whichever fields are present.
    """
    blank_allergies = False
    blank_code_status = False
    patients = data.get("patients") or []
    for patient in patients:
        if not isinstance(patient, dict):
            continue
        per_alerts = patient.get("alerts") or []
        for alert in per_alerts:
            if not isinstance(alert, str):
                continue
            lowered = alert.lower()
            if "blank_allergy" in lowered or "allergy data is incomplete" in lowered:
                blank_allergies = True
            if "blank_code_status" in lowered or "code status not documented" in lowered:
                blank_code_status = True
    return blank_allergies, blank_code_status


def _briefing_identity_summary(final_data: dict[str, Any]) -> str:
    """Build a short identity/fact line for a briefing payload.

    Used to enrich the placeholder narrative the LLM sees on follow-up turns
    so pronoun resolution ("can she have tylenol?") works against the saved
    conversation history.  Kept under ~300 chars and strictly factual — only
    fields that come straight from the briefing structured data, no
    inference, no clinical claims beyond simple counts.
    """
    patient_id = final_data.get("patient_id") or final_data.get("patientId") or "patient"
    # Patient name lives in a few possible spots depending on briefing shape.
    # The actual briefing tool returns `name` at the top level (per
    # /briefing/{pid} response) — that is the load-bearing key. The others
    # are defensive fallbacks for adjacent shapes.
    name = (
        final_data.get("name")
        or final_data.get("patient_name")
        or final_data.get("patientName")
        or (final_data.get("patient") or {}).get("name")
        or (final_data.get("demographics") or {}).get("name")
        or (final_data.get("header") or {}).get("name")
    )
    if isinstance(name, dict):
        # FHIR HumanName-ish dict.
        name = name.get("display") or " ".join(
            part for part in [name.get("given"), name.get("family")] if isinstance(part, str)
        ).strip() or None
    if not isinstance(name, str) or not name.strip():
        name = None

    if name:
        base = f"Briefing generated for {name} (patient_id={patient_id})."
    else:
        base = f"Briefing generated for {patient_id}."

    fact_parts: list[str] = []
    alerts = final_data.get("alerts") or []
    if isinstance(alerts, list) and alerts:
        fact_parts.append(f"{len(alerts)} active alerts")
    medications = (
        final_data.get("active_medications")
        or final_data.get("medications")
        or (final_data.get("sections") or {})
    )
    if isinstance(medications, list) and medications:
        fact_parts.append(f"{len(medications)} active medications")
    code_status = final_data.get("code_status") or final_data.get("codeStatus")
    if isinstance(code_status, str) and code_status.strip():
        fact_parts.append(f"code status {code_status.strip()}")

    if fact_parts:
        # Keep the line tight: cap at ~250 chars so it stays a placeholder.
        suffix = " " + ", ".join(fact_parts) + "."
        candidate = base + suffix
        if len(candidate) <= 280:
            return candidate
    return base


def _structured_skip_narrative(response_type: str, final_data: dict[str, Any]) -> str:
    """Build a one-line placeholder narrative for a structured-renderer response.

    The renderer ignores this string, but downstream verification, history,
    and observability all expect a non-None narrative.  When the structured
    data signals blank allergies or blank code status, the mandated canary
    phrases from ``system_prompt.py`` are appended verbatim so the prompt
    contract is preserved even though the framing turn is skipped.
    """
    if response_type == "handoff":
        # Handoff payloads use ``total`` and ``patients``.  Fall back to the
        # length of patients when ``total`` is absent.
        patients = final_data.get("patients") or []
        total = final_data.get("total", len(patients) if isinstance(patients, list) else 0)
        base = f"Handoff generated for {total} patients."
        blank_allergies, blank_code_status = _handoff_canaries(final_data)
    elif response_type == "census":
        # The census tool returns the patient list under the ``census`` key
        # (see ``triage/census.py``).  Older test fixtures used ``patients``;
        # check both so we stay compatible with either shape.
        entries = final_data.get("census")
        if not isinstance(entries, list):
            entries = final_data.get("patients") or []
        base = f"Census summary generated for {len(entries)} patients."
        # Census aggregates many patients; per-patient allergy / code-status
        # canaries belong on the briefing / medication_safety paths.
        blank_allergies, blank_code_status = False, False
    elif response_type == "briefing":
        base = _briefing_identity_summary(final_data)
        blank_allergies, blank_code_status = _briefing_canaries(final_data)
    elif response_type == "medication_safety":
        patient_id = final_data.get("patient_id") or final_data.get("patientId") or "patient"
        base = f"Medication safety check generated for patient_id={patient_id}."
        blank_allergies, blank_code_status = _medication_safety_canaries(final_data)
    else:
        return ""

    suffix_parts: list[str] = []
    if blank_allergies:
        suffix_parts.append(_CANARY_BLANK_ALLERGIES)
    if blank_code_status:
        suffix_parts.append(_CANARY_BLANK_CODE_STATUS)
    if not suffix_parts:
        return base
    return base + " " + " ".join(suffix_parts)


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

# ── User-facing error classification ─────────────────────────────────────────
# Maps internal :class:`ToolFailureClass` (technical) to a small set of
# error categories the UI can render distinctly.  Keep the set small and
# stable — the frontend switches on these strings.
#
#   transient    — likely to succeed on retry (timeouts, transient FHIR)
#   persistent   — auth/config/safety; retry won't help, contact IT
#   missing_data — no record exists; retry won't help (currently unused
#                  on the failure path; reserved for future "no patient"
#                  signalling from tools)
#   unknown      — default; offer retry but don't promise recovery
ERROR_CLASS_TRANSIENT = "transient"
ERROR_CLASS_PERSISTENT = "persistent"
ERROR_CLASS_MISSING_DATA = "missing_data"
ERROR_CLASS_UNKNOWN = "unknown"

_FAILURE_CLASS_TO_ERROR_CLASS: dict[ToolFailureClass, str] = {
    ToolFailureClass.FHIR_UNAVAILABLE: ERROR_CLASS_TRANSIENT,
    ToolFailureClass.LLM_TIMEOUT: ERROR_CLASS_TRANSIENT,
    ToolFailureClass.TOOL_VALIDATION: ERROR_CLASS_PERSISTENT,
    ToolFailureClass.VERIFICATION_BLOCK: ERROR_CLASS_PERSISTENT,
    ToolFailureClass.UNKNOWN: ERROR_CLASS_UNKNOWN,
}

_RETRY_BY_ERROR_CLASS: dict[str, bool] = {
    ERROR_CLASS_TRANSIENT: True,
    ERROR_CLASS_PERSISTENT: False,
    ERROR_CLASS_MISSING_DATA: False,
    # ``unknown`` by definition gives no signal that retry will help.
    # Defaulting to True invited pointless retry loops on persistent
    # bugs, so we surface no retry affordance and let the physician
    # fall back to the chart.
    ERROR_CLASS_UNKNOWN: False,
}


def _error_response(
    failure_class: ToolFailureClass,
    detail: str = "",
    *,
    retry_after_ms: int | None = None,
) -> dict[str, Any]:
    error_class = _FAILURE_CLASS_TO_ERROR_CLASS.get(failure_class, ERROR_CLASS_UNKNOWN)
    retry_suggested = _RETRY_BY_ERROR_CLASS.get(error_class, True)
    metadata: dict[str, Any] = {
        "error": True,
        "failure_class": failure_class.value,
        "error_class": error_class,
        "retry_suggested": retry_suggested,
        "detail": detail,
    }
    if retry_after_ms is not None:
        metadata["retry_after_ms"] = retry_after_ms
    return {
        "type": "error",
        "data": None,
        "narrative": PHYSICIAN_ERROR_MESSAGES[failure_class],
        "citations": [],
        "metadata": metadata,
    }


# ── Misroute detection ────────────────────────────────────────────────────────

_TOOL_INTENT_HINTS: dict[str, list[str]] = {
    "get_census_summary": ["census", "triage", "list", "all patients", "morning", "patients"],
    "get_patient_briefing": ["briefing", "brief me", "tell me about", "summary of", "before i see"],
    "query_patient_records": ["what is", "what was", "what did", "show me", "potassium", "echo", "labs", "vitals", "why is"],
    "get_medication_safety": ["allerg", "medication", "drug", "interaction", "safe", "lasix", "med"],
    "generate_handoff": ["handoff", "hand off", "sign out", "sign-out"],
}


# Keyword sets that signal the user's natural-language intent matches a given
# structured response_type.  Used to gate the structured-skip optimization:
# we only skip the framing turn when the response we're about to return
# matches what the user actually asked for.  When the model called a
# structured tool as a *resolution step* (e.g. census to look up a name
# before issuing a briefing), none of these will match and the dispatcher
# loop continues so the model can chain to the actually-requested tool.
#
# Style mirrors ``_TOOL_INTENT_HINTS`` above — simple substring heuristics on
# the lower-cased original user message.
_RESPONSE_TYPE_INTENT_HINTS: dict[str, list[str]] = {
    "census": [
        "census",
        "triage",
        "morning rounds",
        "rounds",
        "priority list",
        "show me the list",
        "go",
    ],
    "briefing": [
        "brief",
        "pre-encounter",
        "tell me about",
        "what's going on",
        "what happened",
        "summary of",
        "summarize",
        "give me the patient",
        "patient in bed",
    ],
    "medication_safety": [
        "med safety",
        "medication safety",
        "allergy",
        "allergies",
        "interaction",
        "drug safety",
        "contraindication",
        "concerns with",
        "concerns about",
        "any concerns",
        "is x safe",
        "check medication",
    ],
    "handoff": [
        "handoff",
        "sign-out",
        "signout",
        "sign out",
        "end of rounds",
        "end-of-rounds",
        "shift end",
    ],
}


def _user_intent_matches_response(message: str, response_type: str) -> bool:
    """Return True iff the user's original message reads like a request for
    the given structured response_type.

    When False, the model likely called a structured tool as a name/bed
    resolution step rather than as the final answer — the dispatcher loop
    should continue so the model can chain to the actually-requested tool.
    """
    hints = _RESPONSE_TYPE_INTENT_HINTS.get(response_type)
    if not hints:
        return False
    msg_lower = message.lower()
    return any(h in msg_lower for h in hints)


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
        t0 = time.monotonic()
        try:
            history = await redis_saver.load(session_id)
        except Exception as exc:
            agent_checkpointer_op_duration_seconds.labels(op="load", backend="redis").observe(time.monotonic() - t0)
            agent_checkpointer_ops_total.labels(op="load", backend="redis", outcome="error").inc()
            logger.warning("Redis load failed, falling back to SQLite", extra={"error": str(exc)})
        else:
            agent_checkpointer_op_duration_seconds.labels(op="load", backend="redis").observe(time.monotonic() - t0)
            outcome = "hit" if history else "miss"
            agent_checkpointer_ops_total.labels(op="load", backend="redis", outcome=outcome).inc()
            return history
    if sqlite_saver is not None:
        t0 = time.monotonic()
        try:
            history = await sqlite_saver.load(session_id)
        except Exception:
            agent_checkpointer_op_duration_seconds.labels(op="load", backend="sqlite").observe(time.monotonic() - t0)
            agent_checkpointer_ops_total.labels(op="load", backend="sqlite", outcome="error").inc()
            raise
        agent_checkpointer_op_duration_seconds.labels(op="load", backend="sqlite").observe(time.monotonic() - t0)
        outcome = "hit" if history else "miss"
        agent_checkpointer_ops_total.labels(op="load", backend="sqlite", outcome=outcome).inc()
        return history
    return []


async def _save_turn(
    session_id: str,
    session_context: dict[str, Any],
    role: str,
    content: str | list[dict[str, Any]],
) -> None:
    redis_saver = session_context.get("redis_saver")
    sqlite_saver = session_context.get("sqlite_saver")
    if redis_saver is not None:
        t0 = time.monotonic()
        try:
            await redis_saver.append(session_id, role=role, content=content)
        except Exception as exc:
            agent_checkpointer_op_duration_seconds.labels(op="save", backend="redis").observe(time.monotonic() - t0)
            agent_checkpointer_ops_total.labels(op="save", backend="redis", outcome="error").inc()
            logger.warning("Redis save failed, falling back to SQLite", extra={"error": str(exc)})
        else:
            agent_checkpointer_op_duration_seconds.labels(op="save", backend="redis").observe(time.monotonic() - t0)
            agent_checkpointer_ops_total.labels(op="save", backend="redis", outcome="hit").inc()
            return
    if sqlite_saver is not None:
        t0 = time.monotonic()
        try:
            await sqlite_saver.append(session_id, role=role, content=content)
        except Exception:
            agent_checkpointer_op_duration_seconds.labels(op="save", backend="sqlite").observe(time.monotonic() - t0)
            agent_checkpointer_ops_total.labels(op="save", backend="sqlite", outcome="error").inc()
            raise
        agent_checkpointer_op_duration_seconds.labels(op="save", backend="sqlite").observe(time.monotonic() - t0)
        agent_checkpointer_ops_total.labels(op="save", backend="sqlite", outcome="hit").inc()


# ── System blocks with cache_control ─────────────────────────────────────────

def _build_system_blocks(session_context: dict[str, Any]) -> list[dict[str, Any]]:
    provider_name = session_context.get("provider_name", "Provider")
    provider_id = session_context.get("provider_id", "system")
    patient_ids: list[str] = session_context.get("patient_ids", [])

    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": build_system_prompt(provider_name),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Inject session context so the LLM can call tools without asking for IDs.
    # This is cached per session (ephemeral) — patient list is stable per rounding session.
    if patient_ids:
        patient_ids_line = f"Active census patient IDs: {', '.join(patient_ids)}"
    else:
        patient_ids_line = (
            "Active census patient IDs: not yet loaded. "
            "When the physician requests a census or morning rounds, call get_census_summary "
            "with an empty patient_ids list — the tool will auto-discover all active patients from FHIR."
        )
    session_info_lines = [
        "## Active Session Context",
        "",
        f"Provider ID: {provider_id}",
        patient_ids_line,
        "",
        "When calling tools, use these values for provider_id and patient_ids unless the "
        "physician specifies different values explicitly.",
    ]
    blocks.append(
        {
            "type": "text",
            "text": "\n".join(session_info_lines),
            "cache_control": {"type": "ephemeral"},
        }
    )

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

# Note: cache_control intentionally excluded from these allow-lists — loaded
# historical blocks must NOT carry cache_control because the dispatcher
# applies its own breakpoint to the latest assistant message each turn, and
# Anthropic caps cache_control at 4 blocks total (3 system + 1 prefix).
# Preserving stale cache_control from history pushes us over the limit.
_ALLOWED_TEXT_KEYS = {"type", "text"}
_ALLOWED_TOOL_USE_KEYS = {"type", "id", "name", "input"}
_ALLOWED_TOOL_RESULT_KEYS = {"type", "tool_use_id", "content", "is_error"}


def _sanitize_block_for_anthropic(block: dict[str, Any]) -> dict[str, Any]:
    """Strip non-standard fields and stale cache_control from a content block.

    Older saved sessions and intermediate refactors stored extra metadata on
    blocks (e.g. ``caller={"type":"direct"}`` from a fast-path tag). Anthropic
    rejects unknown fields with HTTP 400, which surfaces as
    "An unexpected error occurred" to the physician. Whitelist the keys
    Anthropic actually accepts per block type.

    Also strips ``cache_control`` — see allow-list comment above.
    """
    btype = block.get("type")
    if btype == "text":
        allowed = _ALLOWED_TEXT_KEYS
    elif btype == "tool_use":
        allowed = _ALLOWED_TOOL_USE_KEYS
    elif btype == "tool_result":
        allowed = _ALLOWED_TOOL_RESULT_KEYS
    else:
        return block
    return {k: v for k, v in block.items() if k in allowed}


def _history_to_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert stored turns to Anthropic messages format.

    The current storage shape is ``{role, content}`` where ``content`` is
    either:

    * a list of Anthropic content blocks (text / tool_use / tool_result),
      already in the exact shape Anthropic's messages API expects, or
    * a plain string (legacy entries written before rich-message persistence,
      and the initial physician input).

    Legacy entries with ``role == "tool"`` (pre-rich-message schema) are
    still supported for backward compatibility with existing Redis/SQLite
    sessions.
    """
    messages: list[dict[str, Any]] = []
    for turn in history:
        role = turn["role"]
        content = turn["content"]
        if role == "tool":
            # Legacy schema: separate "tool" role with a sibling tool_use_id.
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": turn.get("tool_use_id", "unknown"),
                        "content": content if isinstance(content, str) else json.dumps(content),
                    }
                ],
            })
            continue

        if isinstance(content, list):
            # Sanitize each block to drop legacy metadata that Anthropic rejects.
            sanitized = [_sanitize_block_for_anthropic(b) if isinstance(b, dict) else b for b in content]
            messages.append({"role": role, "content": sanitized})
        else:
            # Plain string — wrap as a single text block for round-trip parity.
            messages.append({"role": role, "content": content})
    return messages


# Approximate token budget guard.  When loaded history exceeds this, drop the
# oldest turns (keeping the most recent _HISTORY_KEEP_TURNS) before sending.
_MAX_HISTORY_TOKENS_EST = 150_000
_HISTORY_KEEP_TURNS = 10

# Tool calls whose result is "context-bearing" — i.e. establishes which
# patient the rest of the conversation is talking about.  When truncating
# we preserve any prior assistant tool_use + matching user tool_result
# pair for these tools so pronoun resolution ("can she have tylenol?")
# survives even when the recent window has scrolled past the turn that
# named the patient.
_CONTEXT_BEARING_TOOLS: frozenset[str] = frozenset({
    "get_patient_briefing",
    "get_medication_safety",
})


def _message_block_iter(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the message's content as a list of block dicts (or [] if none)."""
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _is_user_text(message: dict[str, Any]) -> bool:
    """A 'real' user turn — plain string, or list of text blocks (no tool_result)."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        if not content:
            return False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
        return True
    return False


def _has_tool_result(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    for block in _message_block_iter(message):
        if block.get("type") == "tool_result":
            return True
    return False


def _has_tool_use(message: dict[str, Any]) -> bool:
    if message.get("role") != "assistant":
        return False
    for block in _message_block_iter(message):
        if block.get("type") == "tool_use":
            return True
    return False


def _assistant_tool_use_names(message: dict[str, Any]) -> list[str]:
    if message.get("role") != "assistant":
        return []
    names: list[str] = []
    for block in _message_block_iter(message):
        if block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _trim_to_valid_prefix(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk forward until the messages slice starts with a valid user turn,
    drop trailing orphan assistant tool_use turns, AND drop any mid-stream
    assistant tool_use whose ids lack matching tool_result blocks in the
    immediately-following user turn.

    Anthropic strictly enforces tool_use→tool_result pairing: every tool_use
    id in an assistant turn must appear as a tool_use_id in the next user
    turn's tool_result blocks. A mid-stream orphan (e.g. from a crashed
    earlier dispatch that saved tool_use but never wrote its tool_result)
    produces 400 invalid_request_error and surfaces as "An unexpected error"
    to the physician.
    """
    start = 0
    while start < len(messages) and not _is_user_text(messages[start]):
        start += 1
    trimmed = messages[start:]
    # Drop trailing orphan assistant tool_use.
    while trimmed and trimmed[-1].get("role") == "assistant" and _has_tool_use(trimmed[-1]):
        trimmed = trimmed[:-1]

    # Mid-stream validity sweep: for every assistant turn carrying tool_use
    # blocks, the next message must be a user turn whose tool_result blocks
    # cover every tool_use id. Drop any assistant turn that fails this check
    # (and any user tool_result turn left without a preceding tool_use).
    valid: list[dict[str, Any]] = []
    skip_next_orphan_result = False
    for i, msg in enumerate(trimmed):
        if skip_next_orphan_result:
            skip_next_orphan_result = False
            if msg.get("role") == "user" and _has_tool_result(msg) and not _has_user_text_block(msg):
                continue  # skip the orphan tool_result that followed the dropped tool_use
        if msg.get("role") == "assistant" and _has_tool_use(msg):
            tool_use_ids = _assistant_tool_use_ids(msg)
            nxt = trimmed[i + 1] if i + 1 < len(trimmed) else None
            result_ids = _user_tool_result_ids(nxt) if nxt else set()
            if not tool_use_ids.issubset(result_ids):
                # Orphan tool_use — drop this assistant turn, and skip the
                # next message if it's a half-matching tool_result.
                skip_next_orphan_result = True
                continue
        valid.append(msg)
    return valid


def _assistant_tool_use_ids(msg: dict[str, Any]) -> set[str]:
    content = msg.get("content")
    if not isinstance(content, list):
        return set()
    return {
        b["id"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str)
    }


def _user_tool_result_ids(msg: dict[str, Any] | None) -> set[str]:
    if not msg or msg.get("role") != "user":
        return set()
    content = msg.get("content")
    if not isinstance(content, list):
        return set()
    return {
        b["tool_use_id"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)
    }


def _has_user_text_block(msg: dict[str, Any]) -> bool:
    """True if a user message contains any text-typed block (vs purely tool_result)."""
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "text" for b in content)


def _select_context_bearing_indices(
    messages: list[dict[str, Any]],
    recent_start: int,
) -> set[int]:
    """Return the set of indices (before ``recent_start``) we want to keep
    so that earlier briefing / medication_safety tool calls stay anchored.

    For each qualifying assistant ``tool_use`` turn we keep a triple:

        (originating user_text)? + assistant tool_use + user tool_result

    The originating user_text immediately preceding the assistant turn is
    included so the kept slice is bracketed by a real user message and
    Anthropic's alternation rules are satisfied without inserting synthetic
    turns mid-stream.
    """
    keep: set[int] = set()
    for i in range(recent_start):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        names = _assistant_tool_use_names(msg)
        if not any(n in _CONTEXT_BEARING_TOOLS for n in names):
            continue
        # Find the matching tool_result turn that follows.
        result_idx: int | None = None
        for j in range(i + 1, len(messages)):
            nxt = messages[j]
            if nxt.get("role") == "user" and _has_tool_result(nxt):
                result_idx = j
                break
            if nxt.get("role") == "assistant":
                break
        if result_idx is None:
            continue
        keep.add(i)
        keep.add(result_idx)
        # Also pull in the originating user-text prompt directly before the
        # assistant turn (skip prior assistant tool_results) so the kept
        # block starts on a user turn.
        for k in range(i - 1, -1, -1):
            prev = messages[k]
            if _is_user_text(prev):
                keep.add(k)
                break
            if prev.get("role") == "assistant":
                # Hit another assistant — give up scanning further back.
                break
    return keep


def _truncate_history_if_needed(
    messages: list[dict[str, Any]],
    session_id: str,
) -> list[dict[str, Any]]:
    """Estimate token usage and trim history if over budget while preserving
    a valid Anthropic message sequence and any context-bearing tool turns.

    Strategy when over budget:
    1.  Compute the recent window (last ``_HISTORY_KEEP_TURNS`` messages).
    2.  Pull in earlier (assistant tool_use + user tool_result) pairs for
        context-bearing tools (briefing, medication_safety) so pronoun
        references stay resolvable.
    3.  Drop everything else.  Walk forward until the kept slice starts
        with a real user text turn (no orphan tool_result), and drop any
        trailing orphan assistant tool_use.
    4.  Insert a single synthetic summary turn at the front noting what was
        dropped, so the model knows context exists.
    """
    if not messages:
        return messages
    try:
        approx_tokens = len(json.dumps(messages, default=str)) // 4
    except (TypeError, ValueError):
        return messages
    if approx_tokens <= _MAX_HISTORY_TOKENS_EST:
        return messages

    total = len(messages)
    recent_start = max(0, total - _HISTORY_KEEP_TURNS)

    # Collect indices of context-bearing tool turns (briefing / med safety)
    # that fall outside the recent window, plus their originating user
    # prompt, so the kept slice is bracketed by real user turns.
    earlier_keep = _select_context_bearing_indices(messages, recent_start)
    keep_indices: set[int] = set(range(recent_start, total)) | earlier_keep

    kept = [messages[i] for i in sorted(keep_indices)]
    kept = _trim_to_valid_prefix(kept)

    logger.warning(
        "history_truncated_for_token_budget",
        extra={
            "session_id": session_id,
            "approx_tokens": approx_tokens,
            "original_turns": total,
            "kept_turns": len(kept),
            "preserved_earlier_turns": len(earlier_keep),
        },
    )
    return kept


def _apply_messages_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Pin a prompt-cache breakpoint at the longest stable messages prefix.

    Adds ``cache_control: {"type": "ephemeral"}`` to the LAST content block of
    the second-to-last message (the most recent assistant turn or tool_result
    from the prior turn) so the new user input can be appended without
    invalidating the cache.

    Only mutates list-shaped content (rich blocks); plain-string messages are
    left alone since string content does not support cache_control.
    """
    if len(messages) < 2:
        return
    target = messages[-2]
    content = target.get("content")
    if not isinstance(content, list) or not content:
        return
    last_block = content[-1]
    if isinstance(last_block, dict):
        # Don't double-up if a breakpoint already exists.
        if last_block.get("cache_control"):
            return
        last_block["cache_control"] = {"type": "ephemeral"}


# ── Deterministic briefing fast path ──────────────────────────────────────────
#
# A small set of free-text phrasings ("brief X", "brief me on X",
# "pre-encounter briefing for X", "give me a briefing on X", "summary of X")
# express an unambiguous request: run get_patient_briefing for a single named
# patient.  When we can resolve the captured reference to exactly one patient
# from the active census, we skip the LLM planner entirely and call the
# briefing tool directly.  Same shape as the iframe's "Brief" button — the LLM
# is purely a safety net here.
#
# Gate: cold start only.  Any prior history → fall through, because the
# user's intent might be a follow-up that depends on conversational context.
#
# All bail-outs are silent: any failure (no name match, multiple matches,
# census error, briefing error) falls through to the existing LLM dispatch
# loop without raising.

# Anchored to start of trimmed message.  Capture group is the patient
# reference; trailing ".?$" tolerates a single sentence-ending period.
_FAST_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^brief\s+(?:me\s+on\s+)?(.+?)\.?$", re.IGNORECASE),
    re.compile(r"^pre-?encounter\s+briefing\s+(?:for\s+)?(.+?)\.?$", re.IGNORECASE),
    re.compile(
        r"^give\s+me\s+(?:a\s+|the\s+)?briefing\s+(?:for\s+|on\s+)(.+?)\.?$",
        re.IGNORECASE,
    ),
    # Conservative: "summary of X" without a question mark.
    re.compile(r"^summary\s+(?:of\s+|for\s+)?(.+?)\.?$", re.IGNORECASE),
)

# Synthetic patient ID format used by the bundled test bundles
# (synthetic_data/bundles/pt-NNN.json).  When the user references a patient
# by ID directly, skip the census name-resolution step.
_PATIENT_ID_RE: re.Pattern[str] = re.compile(r"^pt-\d+$", re.IGNORECASE)

# Strip a leading "patient " from a captured reference so "brief patient
# pt-001" → "pt-001".
_LEADING_PATIENT_PREFIX_RE: re.Pattern[str] = re.compile(
    r"^patient\s+", re.IGNORECASE
)


def _detect_fast_path_reference(message: str) -> str | None:
    """Return the patient reference if ``message`` matches a fast-path pattern.

    Returns ``None`` when no pattern matches, when the captured reference is
    empty after trimming, or when the message looks like a question
    (heuristic: contains "?") for the conservative ``summary of`` pattern.
    """
    trimmed = message.strip()
    if not trimmed:
        return None
    for pattern in _FAST_PATH_PATTERNS:
        match = pattern.match(trimmed)
        if match is None:
            continue
        # Conservative carve-out: "summary ..." with a "?" reads as a
        # question, not a brief request.  Fall through to the LLM.
        if pattern.pattern.startswith("^summary") and "?" in trimmed:
            return None
        reference = match.group(1).strip()
        if not reference:
            return None
        # "brief patient pt-001" → "pt-001"
        reference = _LEADING_PATIENT_PREFIX_RE.sub("", reference).strip()
        if not reference:
            return None
        return reference
    return None


def _resolve_patient_from_census(
    reference: str,
    census_result: dict[str, Any],
) -> str | None:
    """Resolve a captured patient reference against a census payload.

    Returns the patient_id when the reference matches exactly one census
    entry by case-insensitive substring on the entry's display name, or
    None when zero or 2+ entries match.

    Census payload shape comes from ``get_census_summary``: result.census
    is a list of dicts with ``patient_id`` and ``name`` (display string).
    """
    entries = census_result.get("census") or census_result.get("patients") or []
    if not isinstance(entries, list) or not entries:
        return None
    needle = reference.lower().strip()
    if not needle:
        return None
    matches: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("patient_name") or ""
        if not isinstance(name, str):
            continue
        if needle in name.lower():
            patient_id = entry.get("patient_id") or entry.get("id")
            if isinstance(patient_id, str):
                matches.append(patient_id)
    if len(matches) == 1:
        return matches[0]
    return None


async def _try_briefing_fast_path(
    message: str,
    session_id: str,
    session_context: dict[str, Any],
    history: list[dict[str, Any]],
    t_start: float,
) -> dict[str, Any] | None:
    """Attempt the deterministic briefing fast path.

    Returns a fully-formed response envelope on success, or None to signal
    the caller should fall through to the LLM dispatch loop.  Never raises:
    any internal failure short-circuits to None.
    """
    # Cold-start gate: only fire when this is the first turn.  Follow-up
    # turns may depend on conversational context the LLM should weigh.
    if history:
        return None

    reference = _detect_fast_path_reference(message)
    if reference is None:
        return None

    # Direct synthetic-ID reference skips the census lookup.
    if _PATIENT_ID_RE.match(reference):
        resolved_id: str | None = reference.lower()
    else:
        census_tool = TOOL_REGISTRY.get("get_census_summary")
        if census_tool is None:
            return None
        try:
            census_payload = await census_tool(
                {
                    "provider_id": session_context.get("provider_id", "system"),
                    "patient_ids": session_context.get("patient_ids", []) or [],
                },
                session_context,
            )
        except Exception as exc:
            logger.info(
                "dispatcher.fast_path_bail_census_error",
                extra={
                    "session_id": session_id,
                    "error": str(exc),
                    "reference": reference,
                },
            )
            return None
        census_result = census_payload.get("result") if isinstance(census_payload, dict) else None
        if not isinstance(census_result, dict):
            return None
        resolved_id = _resolve_patient_from_census(reference, census_result)

    if resolved_id is None:
        logger.info(
            "dispatcher.fast_path_bail_no_match",
            extra={"session_id": session_id, "reference": reference},
        )
        return None

    briefing_tool = TOOL_REGISTRY.get("get_patient_briefing")
    if briefing_tool is None:
        return None

    fast_path_duration_ms = int((time.monotonic() - t_start) * 1000)
    logger.info(
        "dispatcher.fast_path_dispatch",
        extra={
            "session_id": session_id,
            "tool": "get_patient_briefing",
            "patient_id": resolved_id,
            "resolved_name": reference,
            "original_message": message[:120],
            "duration_ms_so_far": fast_path_duration_ms,
        },
    )

    _t_tool = time.monotonic()
    try:
        briefing_payload = await briefing_tool(
            {"patient_id": resolved_id}, session_context
        )
    except Exception as exc:
        logger.info(
            "dispatcher.fast_path_bail_briefing_error",
            extra={
                "session_id": session_id,
                "error": str(exc),
                "patient_id": resolved_id,
            },
        )
        return None

    if not isinstance(briefing_payload, dict):
        return None

    final_data = briefing_payload.get("result") or {}
    citations = briefing_payload.get("citations", []) or []
    response_type = "briefing"
    final_narrative = _structured_skip_narrative(response_type, final_data)
    tool_duration_ms = int((time.monotonic() - _t_tool) * 1000)

    # Counter + audit event mirror the LLM-path success branch.
    agent_fast_path_hits_total.labels(tool="get_patient_briefing").inc()
    agent_tool_calls_total.labels(tool="get_patient_briefing").inc()
    emit_audit_event(
        session_id=session_id,
        provider_id=session_context.get("provider_id"),
        tool_name="get_patient_briefing",
        outcome="ok",
        duration_ms=tool_duration_ms,
        patient_id=resolved_id,
    )

    # Persist user + synthetic assistant turn so conversation history stays
    # consistent with what the LLM path would have written: a user text turn
    # followed by an assistant text turn carrying the placeholder narrative
    # (mirrors the structured-skip branch in the main loop).
    await _save_turn(session_id, session_context, "user", message)
    await _save_turn(
        session_id,
        session_context,
        "assistant",
        [{"type": "text", "text": final_narrative}],
    )

    duration_s = time.monotonic() - t_start
    duration_ms = int(duration_s * 1000)
    agent_dispatch_latency_seconds.observe(duration_s)

    fast_path_metadata: dict[str, Any] = {
        "session_id": session_id,
        "duration_ms": duration_ms,
        "turn_count": 0,
        "misroute_detected": False,
        "self_corrected": False,
        "verification_violations": [],
        "fast_path": True,
        "patient_id": resolved_id,
    }
    _fp_name = final_data.get("name") if isinstance(final_data, dict) else None
    if isinstance(_fp_name, str) and _fp_name:
        fast_path_metadata["patient_name"] = _fp_name

    return {
        "type": response_type,
        "data": final_data,
        "narrative": final_narrative,
        "citations": citations,
        "metadata": fast_path_metadata,
    }


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

    langfuse_trace = None
    dispatch_span = None
    if langfuse is not None:
        langfuse_trace = langfuse.trace(
            name="dispatch",
            session_id=session_id,
            input={"message": message},
            metadata={"provider_id": session_context.get("provider_id")},
        )
        if langfuse_trace is not None:
            dispatch_span = langfuse_trace.span(
                name="dispatch",
                input={"message": message, "session_id": session_id},
                metadata={"provider_id": session_context.get("provider_id")},
            )

    # Load conversation history
    history = await _load_history(session_id, session_context)

    # Deterministic briefing fast path — short-circuits the LLM planner for
    # cold-start "brief X" / "summary of X" phrasings that resolve to a
    # single census patient.  Bails to the LLM path on any failure.
    fast_path_response = await _try_briefing_fast_path(
        message=message,
        session_id=session_id,
        session_context=session_context,
        history=history,
        t_start=t_start,
    )
    if fast_path_response is not None:
        _finalize_span(
            dispatch_span,
            error=False,
            output={
                "type": fast_path_response["type"],
                "duration_ms": fast_path_response["metadata"]["duration_ms"],
            },
            metadata={"fast_path": True},
        )
        return fast_path_response

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
    # Track the most recent tool failure so the LLM-narrated final envelope
    # can surface error_class / retry_suggested metadata for the UI.  Reset
    # on any subsequent tool success so a "failed then recovered" dispatch
    # returns a clean envelope.
    last_tool_failure_class: ToolFailureClass | None = None
    # Track the most recent patient_id any tool call referenced during this
    # dispatch so the response envelope can carry it. The UI uses this to
    # render "Verify in Chart ↗" on free-text responses (medication safety,
    # query answers) where response.data has no patient_id field.
    last_tool_patient_id: str | None = None
    # Track the most recent patient name surfaced by a successful tool call
    # so the UI can render a prominent patient banner on free-text answers
    # (e.g. medication safety, query answers) after pronoun resolution.
    last_tool_patient_name: str | None = None

    try:
        while turn_count < MAX_TOOL_TURNS:
            turn_count += 1

            generation_event = None
            if langfuse_trace is not None:
                generation_event = langfuse_trace.generation(
                    name=f"llm_call_turn_{turn_count}",
                    model=_MODEL,
                    input=messages,
                )

            # Trim oldest turns if approaching the model context limit, then
            # pin a prompt-cache breakpoint at the longest stable prefix.
            messages = _truncate_history_if_needed(messages, session_id)
            _apply_messages_cache_breakpoint(messages)

            response = await _anthropic.messages.create(
                model=_MODEL,
                max_tokens=16384,
                system=system_blocks,
                messages=messages,
                tools=DISPATCHER_TOOLS,
            )

            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            if cache_read > 0:
                agent_prompt_cache_hits_total.inc(cache_read)
            if cache_create > 0:
                agent_prompt_cache_misses_total.inc(cache_create)

            if generation_event is not None:
                generation_event.end(
                    output=response.content,
                    usage={
                        "input": response.usage.input_tokens,
                        "output": response.usage.output_tokens,
                        "total": response.usage.input_tokens + response.usage.output_tokens,
                        "unit": "TOKENS",
                    },
                    metadata={
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": cache_create,
                    },
                )

            if response.stop_reason == "max_tokens":
                logger.warning("LLM hit max_tokens", extra={"session_id": session_id})
                # If a tool already ran successfully, return its data with whatever partial narrative
                # was generated. This prevents large handoff / census responses from being discarded.
                if final_data is not None:
                    for block in response.content:
                        if hasattr(block, "text"):
                            final_narrative += block.text
                    logger.warning(
                        "Returning partial narrative after max_tokens; tool data intact",
                        extra={"session_id": session_id, "response_type": response_type},
                    )
                    break
                _finalize_span(dispatch_span, error=True)
                return _error_response(ToolFailureClass.LLM_TIMEOUT, "max_tokens exceeded")

            if response.stop_reason == "end_turn":
                # Extract text narrative from response
                for block in response.content:
                    if hasattr(block, "text"):
                        final_narrative += block.text
                # Persist the assistant's rich response so a follow-up turn
                # can replay the exact content the model produced.
                await _save_turn(
                    session_id,
                    session_context,
                    "assistant",
                    _blocks_to_dicts(response.content),
                )
                break

            if response.stop_reason == "tool_use":
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                if not tool_use_blocks:
                    break

                # Append assistant turn with tool_use block (in-flight + persisted).
                assistant_blocks = _blocks_to_dicts(response.content)
                messages.append({"role": "assistant", "content": response.content})
                await _save_turn(session_id, session_context, "assistant", assistant_blocks)

                tool_results: list[dict[str, Any]] = []
                for tool_use_block in tool_use_blocks:
                    tool_name: str = tool_use_block.name
                    tool_input: dict[str, Any] = tool_use_block.input
                    tool_use_id: str = tool_use_block.id

                    # Increment tool call counter
                    agent_tool_calls_total.labels(tool=tool_name).inc()

                    # Misroute detection (first tool call only, no correction yet attempted)
                    if turn_count == 1 and not correction_attempted:
                        if _detect_misroute(message, tool_name):
                            misroute_detected = True
                            agent_tool_misroute_total.inc()
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
                        emit_audit_event(
                            session_id=session_id,
                            provider_id=session_context.get("provider_id"),
                            tool_name=tool_name,
                            outcome="blocked",
                            duration_ms=0,
                            patient_id=requested_pid,
                            failure_class="census_scope_violation",
                        )
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
                        if langfuse_trace is not None:
                            tool_span = langfuse_trace.span(
                                name=f"tool_{tool_name}",
                                input=tool_input,
                                metadata={"tool_use_id": tool_use_id},
                            )

                        logger.info(
                            "tool_call_start",
                            extra={
                                "session_id": session_id,
                                "tool": tool_name,
                                "tool_use_id": tool_use_id,
                                "turn": turn_count,
                            },
                        )
                        _t_tool = time.monotonic()
                        try:
                            tool_result = await _call_tool_with_retry(tool_fn, tool_input, session_context, tool_name)
                            # Successful tool call clears any prior failure
                            # signal so a recovered dispatch returns clean.
                            last_tool_failure_class = None
                            all_citations.extend(tool_result.get("citations", []))
                            result_data = tool_result.get("result", {})
                            tool_result_content = json.dumps(result_data)

                            # Track the patient context established by this
                            # successful tool call so the response envelope
                            # can surface it for the UI banner / chart button.
                            if tool_input.get("patient_id"):
                                last_tool_patient_id = tool_input["patient_id"]
                            if isinstance(result_data, dict):
                                _name = result_data.get("name") or result_data.get("patient_name")
                                if isinstance(_name, str) and _name:
                                    last_tool_patient_name = _name

                            # Capture structured data for response envelope.
                            # First tool always seeds the envelope.  A later
                            # tool overrides only when the user's intent
                            # matches the later tool's response_type — i.e.
                            # the earlier tool was a resolution step and the
                            # later tool is the actual answer.
                            inferred_type = _infer_response_type(tool_name)
                            if final_data is None:
                                final_data = result_data
                                response_type = inferred_type
                            elif (
                                inferred_type in _STRUCTURED_RESPONSE_TYPES
                                and _user_intent_matches_response(message, inferred_type)
                            ):
                                final_data = result_data
                                response_type = inferred_type

                            if tool_span is not None:
                                tool_span.end(output=result_data)

                            _tool_duration_ms = int((time.monotonic() - _t_tool) * 1000)
                            logger.info(
                                "tool_call_end",
                                extra={
                                    "session_id": session_id,
                                    "tool": tool_name,
                                    "tool_use_id": tool_use_id,
                                    "turn": turn_count,
                                    "duration_ms": _tool_duration_ms,
                                    "outcome": "ok",
                                },
                            )
                            emit_audit_event(
                                session_id=session_id,
                                provider_id=session_context.get("provider_id"),
                                tool_name=tool_name,
                                outcome="ok",
                                duration_ms=_tool_duration_ms,
                                patient_id=tool_input.get("patient_id"),
                            )

                        except Exception as exc:
                            failure_class = _classify_failure(exc)
                            last_tool_failure_class = failure_class
                            tool_result_content = json.dumps(
                                {"error": PHYSICIAN_ERROR_MESSAGES[failure_class]}
                            )
                            if tool_span is not None:
                                tool_span.end(output={"error": str(exc)}, level="ERROR")
                            logger.error(
                                "Tool call failed",
                                extra={"tool": tool_name, "error": str(exc), "class": failure_class.value},
                            )
                            _tool_duration_ms = int((time.monotonic() - _t_tool) * 1000)
                            logger.info(
                                "tool_call_end",
                                extra={
                                    "session_id": session_id,
                                    "tool": tool_name,
                                    "tool_use_id": tool_use_id,
                                    "turn": turn_count,
                                    "duration_ms": _tool_duration_ms,
                                    "outcome": "error",
                                    "failure_class": failure_class.value,
                                },
                            )
                            emit_audit_event(
                                session_id=session_id,
                                provider_id=session_context.get("provider_id"),
                                tool_name=tool_name,
                                outcome="error",
                                duration_ms=_tool_duration_ms,
                                patient_id=tool_input.get("patient_id"),
                                failure_class=failure_class.value,
                            )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": tool_result_content,
                        }
                    )

                messages.append({"role": "user", "content": tool_results})
                # Persist the tool_result blocks so the model has access to the
                # structured FHIR data (census lists, briefing sections, etc.)
                # on the NEXT turn rather than only the framing narrative.
                await _save_turn(session_id, session_context, "user", tool_results)

                # Structured renderers consume `response.data` and ignore the
                # narrative — skip the second (framing) Anthropic call to save
                # ~1.5–2 s of latency per turn.  Free-text types (`text`,
                # `query_answer`) still fall through to the framing turn.
                #
                # Two-condition gate: (1) the response_type is structured AND
                # (2) the user's original intent matches that response_type.
                # The intent check prevents skipping when a structured tool
                # was called as a *resolution step* (e.g. "Brief Marcus Webb"
                # → census lookup → briefing).  Without it, the dispatcher
                # would return the resolution-step census as the final answer
                # and never let the model chain to the requested briefing.
                if (
                    response_type in _STRUCTURED_RESPONSE_TYPES
                    and final_data is not None
                    and _user_intent_matches_response(message, response_type)
                ):
                    final_narrative = _structured_skip_narrative(response_type, final_data)
                    # No framing call will run, so persist a synthetic assistant
                    # text block so subsequent turns see a complete user/assistant
                    # exchange rather than ending mid-tool_result.
                    await _save_turn(
                        session_id,
                        session_context,
                        "assistant",
                        [{"type": "text", "text": final_narrative}],
                    )
                    logger.info(
                        "structured_response_framing_skipped",
                        extra={
                            "session_id": session_id,
                            "response_type": response_type,
                            "turn": turn_count,
                        },
                    )
                    break

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
                "metadata": {
                    "verification_blocked": True,
                    "failure_class": ToolFailureClass.VERIFICATION_BLOCK.value,
                    "error_class": ERROR_CLASS_PERSISTENT,
                    "retry_suggested": False,
                },
            }

        verified_response = verification_result.modified_response
        final_narrative = verified_response.get("narrative", final_narrative)

        # NOTE: the assistant turn was already persisted in rich form above
        # (either via the end_turn branch, the structured-skip branch, or the
        # max_tokens-with-data branch).  No further save is needed here — the
        # next turn's _load_history will pick up the rich content blocks.

        duration_s = time.monotonic() - t_start
        duration_ms = int(duration_s * 1000)
        agent_dispatch_latency_seconds.observe(duration_s)

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

        metadata: dict[str, Any] = {
            "session_id": session_id,
            "duration_ms": duration_ms,
            "turn_count": turn_count,
            "misroute_detected": misroute_detected,
            "self_corrected": self_corrected,
            "verification_violations": verification_result.violations,
        }
        # If a tool failure occurred during this dispatch and was NOT cleared
        # by a subsequent successful call, surface the classification on the
        # final (LLM-narrated) envelope so the UI can render the right Retry
        # affordance.  The narrative still comes from the framing turn.
        if last_tool_failure_class is not None:
            error_class = _FAILURE_CLASS_TO_ERROR_CLASS.get(
                last_tool_failure_class, ERROR_CLASS_UNKNOWN
            )
            metadata["failure_class"] = last_tool_failure_class.value
            metadata["error_class"] = error_class
            metadata["retry_suggested"] = _RETRY_BY_ERROR_CLASS.get(error_class, False)

        # Surface patient identity for the UI: lets ChatSurface render the
        # "Verify in Chart" button on free-text responses whose `data` has
        # no patient_id, and lets renderers show a prominent patient banner.
        # When this turn made no fresh tool call (LLM answered from prior
        # context), scan the loaded conversation history backward for the
        # most recent assistant tool_use with a patient_id input and surface
        # that — so the chart button stays visible across follow-up turns.
        if not last_tool_patient_id:
            for msg in reversed(messages):
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    pid = (block.get("input") or {}).get("patient_id")
                    if isinstance(pid, str) and pid:
                        last_tool_patient_id = pid
                        break
                if last_tool_patient_id:
                    break
        if last_tool_patient_id:
            metadata["patient_id"] = last_tool_patient_id
        if last_tool_patient_name:
            metadata["patient_name"] = last_tool_patient_name

        return {
            "type": response_type,
            "data": final_data,
            "narrative": final_narrative,
            "citations": all_citations,
            "metadata": metadata,
        }

    except anthropic.RateLimitError as exc:
        # Capture ``retry-after`` (seconds) from the upstream 429 so the UI
        # can render an informed back-off.  Header may be absent; treat
        # parse failures as "unknown back-off" rather than failing the
        # request a second time.
        retry_after_ms: int | None = None
        try:
            response = getattr(exc, "response", None)
            header = response.headers.get("retry-after") if response is not None else None
            if header is not None:
                retry_after_ms = int(float(header) * 1000)
        except (AttributeError, TypeError, ValueError):
            retry_after_ms = None
        logger.error(
            "Dispatcher rate-limited",
            extra={"session_id": session_id, "retry_after_ms": retry_after_ms},
        )
        _finalize_span(dispatch_span, error=True)
        return _error_response(
            ToolFailureClass.LLM_TIMEOUT,
            "rate limited",
            retry_after_ms=retry_after_ms,
        )

    except Exception as exc:
        logger.error("Dispatcher error", extra={"session_id": session_id, "error": str(exc)})
        _finalize_span(dispatch_span, error=True)
        return _error_response(ToolFailureClass.UNKNOWN, str(exc))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _blocks_to_dicts(blocks: Any) -> list[dict[str, Any]]:
    """Convert Anthropic SDK response content blocks (pydantic) to plain dicts.

    The dicts round-trip safely through JSON storage and are accepted as
    input the next time the messages array is sent to the API.

    Tolerates: already-dict inputs (passed through), pydantic v1/v2 blocks,
    and test-time MagicMocks (whose ``model_dump`` would otherwise return
    another MagicMock and break JSON serialization).
    """
    out: list[dict[str, Any]] = []
    for b in blocks:
        if isinstance(b, dict):
            out.append(b)
            continue
        d: dict[str, Any] | None = None
        for attr in ("model_dump", "dict"):
            fn = getattr(type(b), attr, None)
            if fn is None:
                continue
            try:
                if attr == "model_dump":
                    candidate = fn(b, exclude_none=True)
                else:
                    candidate = fn(b)
            except Exception:
                continue
            if isinstance(candidate, dict):
                d = candidate
                break
        if d is None:
            block_type = getattr(b, "type", None)
            if not isinstance(block_type, str):
                block_type = "text"
            d = {"type": block_type}
            text = getattr(b, "text", None)
            if isinstance(text, str):
                d["text"] = text
            name = getattr(b, "name", None)
            if isinstance(name, str):
                d["name"] = name
            block_id = getattr(b, "id", None)
            if isinstance(block_id, str):
                d["id"] = block_id
            inp = getattr(b, "input", None)
            if isinstance(inp, dict):
                d["input"] = inp
        out.append(d)
    return out


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
