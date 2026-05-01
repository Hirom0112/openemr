"""UC-5 Parallel Handoff Generation.

Generates I-PASS handoff summaries for multiple patients in parallel.
Each handoff covers: Illness severity, Patient summary, Action list,
Situation awareness, Synthesis by receiver (prompts only).

All handoffs are generated concurrently — the bottleneck is FHIR
fetch latency, not LLM throughput, so parallelism is the right strategy.

Output: list[HandoffSummary] sorted by triage level (most urgent first).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic
from langfuse import Langfuse

from agent.response_schemas import PRODUCE_HANDOFF
from auth.fhir_client import fhir_client
from briefing.context_builder import build as build_context
from config import settings
from triage.criteria import extract
from triage.rules_engine import rank

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SYSTEM = """You are generating an I-PASS handoff summary for a patient.
Use the produce_handoff tool to return your response.
Use only data from the patient context provided.
Do not speculate.  Do not make treatment recommendations."""


@dataclass
class HandoffSummary:
    patient_id: str
    name: str
    mrn: str
    triage_level: int
    illness_severity: str
    patient_summary: str
    action_list: list[str]
    situation_awareness: str
    contingency_plan: str
    generated_at: str
    error: str | None = None


async def _generate_one(
    patient_id: str,
    client: anthropic.AsyncAnthropic,
    langfuse: Langfuse | None = None,
) -> HandoffSummary:
    import json
    from dataclasses import asdict

    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        patient = await fhir_client.get_patient(patient_id)
        bundle = await fhir_client.get_bundle_for_patient(patient_id)
    except Exception as exc:
        logger.warning("FHIR fetch failed for handoff", extra={"patient_id": patient_id, "error": str(exc)})
        return HandoffSummary(
            patient_id=patient_id, name="Unknown", mrn="", triage_level=10,
            illness_severity="Unknown", patient_summary="FHIR data unavailable.",
            action_list=[], situation_awareness="", contingency_plan="",
            generated_at=generated_at, error=str(exc),
        )

    ctx = build_context(patient, bundle)
    criteria = extract(bundle)
    triage = rank(criteria)

    prompt = (
        f"Generate an I-PASS handoff for this patient.\n\n"
        f"<patient_data>\n"
        f"PATIENT CONTEXT:\n{json.dumps(asdict(ctx), indent=2)}\n\n"
        f"TRIAGE: Level {triage.level} — {triage.label}\n"
        f"</patient_data>\n\n"
        f"Call the produce_handoff tool with the complete I-PASS summary."
    )

    trace = langfuse.trace(name="handoff-generate", user_id=patient_id) if langfuse else None
    generation = trace.generation(name="handoff-llm", model=_MODEL, input=prompt) if trace else None

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[PRODUCE_HANDOFF],
            tool_choice={"type": "any"},
        )

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            raise ValueError("No tool_use block in handoff response")

        data = tool_block.input
        if generation:
            generation.end(output=data)

        return HandoffSummary(
            patient_id=patient_id,
            name=ctx.name,
            mrn=ctx.mrn,
            triage_level=triage.level,
            illness_severity=data.get("illness_severity", "Unknown"),
            patient_summary=data.get("patient_summary", ""),
            action_list=data.get("action_list", []),
            situation_awareness=data.get("situation_awareness", ""),
            contingency_plan=data.get("contingency_plan", ""),
            generated_at=generated_at,
        )
    except Exception as exc:
        logger.error("Handoff LLM failed", extra={"patient_id": patient_id, "error": str(exc)})
        return HandoffSummary(
            patient_id=patient_id, name=ctx.name, mrn=ctx.mrn,
            triage_level=triage.level, illness_severity="Unknown",
            patient_summary="Handoff generation failed — review chart directly.",
            action_list=[], situation_awareness="", contingency_plan="",
            generated_at=generated_at, error=str(exc),
        )


_CONCURRENCY = 4          # max parallel Anthropic calls
_PATIENT_TIMEOUT = 25.0   # seconds per patient before returning error stub


async def generate_handoffs(
    patient_ids: list[str],
    langfuse: Langfuse | None = None,
) -> list[HandoffSummary]:
    """Generate handoff summaries for all patients in parallel."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _bounded(pid: str) -> HandoffSummary:
        async with sem:
            try:
                return await asyncio.wait_for(
                    _generate_one(pid, client, langfuse),
                    timeout=_PATIENT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Handoff timed out", extra={"patient_id": pid})
                return HandoffSummary(
                    patient_id=pid, name="Unknown", mrn="", triage_level=10,
                    illness_severity="Unknown",
                    patient_summary="Handoff timed out — review chart directly.",
                    action_list=[], situation_awareness="", contingency_plan="",
                    generated_at=datetime.now(timezone.utc).isoformat(),
                    error="timeout",
                )

    summaries = await asyncio.gather(*[_bounded(pid) for pid in patient_ids])
    result = list(summaries)
    result.sort(key=lambda s: s.triage_level)
    return result
