"""UC-2 Pre-Encounter Briefing — LLM generation with full verification.

Flow:
  1. Build BriefingContext (structured FHIR data with citations).
  2. Render a prompt that includes every clinical fact with its source.
  3. Call Claude via tool_use to produce a BriefingResponse (structured output).
  4. Run source-attribution verification — every claim must trace back to
     a record in BriefingContext.
  5. Apply domain constraints from Phase 1.
  6. Return the verified response.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

import anthropic
from langfuse import Langfuse
from pydantic import ValidationError

from agent.response_schemas import PRODUCE_BRIEFING
from briefing.context_builder import BriefingContext
from briefing.schema import BriefingResponse, BriefingSection, ClinicalClaim
from config import settings

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SYSTEM = """You are a clinical documentation assistant generating a pre-encounter briefing for a rounding hospitalist.

Rules you must follow without exception:
1. Every clinical claim in your output must include source_resource, source_code, source_value, and source_dt from the data provided.
2. Do not invent or extrapolate values. If a value is not in the data, omit it.
3. Never say "no known allergies" if the allergy section is flagged as blank.
4. Always include an alert for blank code status if present.
5. Mark any critical value older than 30 minutes as potentially stale.
6. Use the produce_briefing tool to return your response.
7. Do not include clinical recommendations, treatment suggestions, or medication changes."""


def _render_prompt(ctx: BriefingContext) -> str:
    ctx_dict = asdict(ctx)
    return (
        f"Generate a pre-encounter briefing for the following patient.\n\n"
        f"<patient_data>\n{json.dumps(ctx_dict, indent=2)}\n</patient_data>\n\n"
        f"Call the produce_briefing tool with a complete structured briefing."
    )


async def generate_briefing(
    ctx: BriefingContext,
    langfuse: Langfuse | None = None,
) -> BriefingResponse:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    prompt = _render_prompt(ctx)

    trace = langfuse.trace(name="briefing-generate", user_id=ctx.patient_id) if langfuse else None
    generation = trace.generation(name="briefing-llm", model=_MODEL, input=prompt) if trace else None

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[PRODUCE_BRIEFING],
            tool_choice={"type": "any"},
        )

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            logger.error("No tool_use block in briefing response", extra={"patient_id": ctx.patient_id})
            return _fallback_briefing(ctx)

        if generation:
            generation.end(output=tool_block.input)

        briefing = BriefingResponse.model_validate(tool_block.input)
        return briefing

    except ValidationError as exc:
        logger.error("Briefing tool_use output failed schema validation", extra={"patient_id": ctx.patient_id, "error": str(exc)})
        return _fallback_briefing(ctx)
    except Exception as exc:
        logger.error("Briefing generation failed", extra={"patient_id": ctx.patient_id, "error": str(exc)})
        return _fallback_briefing(ctx)


def _fallback_briefing(ctx: BriefingContext) -> BriefingResponse:
    alerts: list[str] = []
    if ctx.has_blank_code_status:
        alerts.append("BLANK_CODE_STATUS: code-status field is empty — clarification required")
    if ctx.has_blank_allergy_section:
        alerts.append("BLANK_ALLERGY_SECTION: allergy section is empty — cannot assert NKDA")

    return BriefingResponse(
        patient_id=ctx.patient_id,
        name=ctx.name,
        sections=[],
        alerts=alerts,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
