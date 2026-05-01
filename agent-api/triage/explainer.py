"""LLM explainer for UC-1 triage list.

Calls Claude to generate a single-sentence clinical explanation for each
census entry AFTER the deterministic rules engine has already assigned
priority levels.  The LLM does not re-rank — it only writes prose.

Explanation format (enforced via tool_use schema):
  "<Patient> is priority <N> because <one clinical fact with value and source>."

If the LLM call fails, a fallback explanation is derived from matched_criteria
so the triage list is never blocked on an LLM response.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

import anthropic
import redis.asyncio as aioredis
from langfuse import Langfuse

from agent.response_schemas import PRODUCE_TRIAGE_EXPLANATION
from config import settings
from triage.census import _CACHE_TTL, CensusEntry

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"  # fast + cheap for bulk one-liners

_SYSTEM = (
    "You are a clinical documentation assistant. "
    "You will receive a single patient's triage data. "
    "Use the produce_triage_explanation tool to return exactly one sentence (≤20 words) "
    "explaining the most urgent clinical finding. "
    "State a specific value (e.g. K+ 6.4, RR 26) and its source (vital sign or lab). "
    "Never speculate. Never add clinical recommendations."
)


def _fallback_explanation(entry: CensusEntry) -> str:
    if entry.matched_criteria.get("critical_lab"):
        return f"{entry.name} has an unacknowledged critical lab value."
    if entry.matched_criteria.get("qsofa_score", 0) >= 2:
        score = entry.matched_criteria.get("qsofa_score", "≥2")
        return f"{entry.name} has a qSOFA score of {score}, indicating infection-related organ dysfunction risk."
    if entry.matched_criteria.get("critical_vital"):
        return f"{entry.name} has a critical vital sign requiring immediate review."
    return f"{entry.name} is priority {entry.triage_level}: {entry.triage_label}."


def _build_user_prompt(entry: CensusEntry) -> str:
    vitals_lines = ", ".join(
        f"{code}={val:.1f}" for code, val in entry.vitals_summary.items()
    )
    return (
        f"Patient: {entry.name}\n"
        f"Priority level: {entry.triage_level} ({entry.triage_label})\n"
        f"Matched criteria: {entry.matched_criteria}\n"
        f"Latest vitals (LOINC code=value): {vitals_lines or 'none'}\n"
        f"Call produce_triage_explanation with one sentence explaining the most urgent finding."
    )


async def explain_one(
    entry: CensusEntry,
    client: anthropic.AsyncAnthropic,
    langfuse: Langfuse | None = None,
) -> str:
    trace = langfuse.trace(name="triage-explainer", user_id=entry.patient_id) if langfuse else None
    user_prompt = _build_user_prompt(entry)

    try:
        generation = trace.generation(name="one-liner", model=_MODEL, input=user_prompt) if trace else None

        response = await client.messages.create(
            model=_MODEL,
            max_tokens=80,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[PRODUCE_TRIAGE_EXPLANATION],
            tool_choice={"type": "any"},
        )

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            logger.warning("No tool_use block in explainer response", extra={"patient_id": entry.patient_id})
            return _fallback_explanation(entry)

        text = tool_block.input.get("explanation", "").strip()
        if not text:
            return _fallback_explanation(entry)

        if generation:
            generation.end(output=text)

        return text

    except Exception as exc:
        logger.warning(
            "LLM explainer failed, using fallback",
            extra={"patient_id": entry.patient_id, "error": str(exc)},
        )
        return _fallback_explanation(entry)


def _explanation_cache_key(entry: CensusEntry) -> str:
    payload = json.dumps(
        {
            "pid": entry.patient_id,
            "level": entry.triage_level,
            "criteria": entry.matched_criteria,
            "vitals": entry.vitals_summary,
        },
        sort_keys=True,
    )
    return "copilot:explanation:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


async def _explain_with_cache(
    entry: CensusEntry,
    client: anthropic.AsyncAnthropic,
    langfuse: Langfuse | None,
    redis_client: aioredis.Redis | None,
) -> str:
    if redis_client is None:
        return await explain_one(entry, client, langfuse)

    cache_key = _explanation_cache_key(entry)
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            text = cached.decode() if isinstance(cached, bytes) else cached
            if text:
                return text
    except Exception as exc:
        logger.warning(
            "Explanation cache read failed",
            extra={"patient_id": entry.patient_id, "error": str(exc)},
        )

    explanation = await explain_one(entry, client, langfuse)

    try:
        await redis_client.setex(cache_key, _CACHE_TTL, explanation)
    except Exception as exc:
        logger.warning(
            "Explanation cache write failed",
            extra={"patient_id": entry.patient_id, "error": str(exc)},
        )

    return explanation


async def explain_census(
    entries: list[CensusEntry],
    langfuse: Langfuse | None = None,
    redis_client: aioredis.Redis | None = None,
) -> list[dict[str, Any]]:
    """Annotate each census entry with a one-line explanation.

    Calls are parallelised across entries.  The triage ranking is not
    altered — the LLM adds prose only.
    """
    anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    explanations = await asyncio.gather(
        *[_explain_with_cache(e, anthropic_client, langfuse, redis_client) for e in entries]
    )

    return [
        {
            "patient_id": e.patient_id,
            "name": e.name,
            "mrn": e.mrn,
            "openemr_pid": e.openemr_pid,
            "triage_level": e.triage_level,
            "triage_label": e.triage_label,
            "explanation": explanation,
            "matched_criteria": e.matched_criteria,
            "admit_date": e.admit_date,
            "days_since_admit": e.days_since_admit,
        }
        for e, explanation in zip(entries, explanations)
    ]
