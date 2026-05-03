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
from typing import Any

import anthropic
from langfuse import Langfuse
from pydantic import ValidationError

from agent.response_schemas import PRODUCE_BRIEFING
from briefing.context_builder import BriefingContext
from briefing.schema import BriefingResponse, BriefingSection, ClinicalClaim
from config import settings

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"

_SYSTEM = """You are a clinical documentation assistant generating a pre-encounter briefing for a rounding hospitalist.

Rules you must follow without exception:
1. Every clinical claim in your output must include source_resource, source_code, source_value, and source_dt from the data provided.
2. Do not invent or extrapolate values. If a value is not in the data, omit it.
3. Never say "no known allergies" if the allergy section is flagged as blank.
4. Mark any critical value older than 30 minutes as potentially stale.
5. Use the produce_briefing tool to return your response.
6. Do not include clinical recommendations, treatment suggestions, or medication changes.
7. Observations about a section being blank, absent, or undocumented (e.g. "allergy section is blank", "no allergy entries on file") belong in the "alerts" array, NOT inside sections[*].claims. Claims are reserved for clinical facts derived from a specific FHIR resource entry; meta-observations about the absence of data are alerts.
8. Do NOT generate alerts about code status — code status is rendered in a dedicated chart panel and the briefing alerts array must not duplicate it. If has_blank_code_status is true in the input, ignore it; do not surface it as an alert or claim.

Sections requirement (do not skip):
- The "sections" array is mandatory and must be populated whenever any of the following are present in the input: active_conditions, active_medications, recent_vitals, recent_labs, allergies. Do not return only an "alerts" array.
- For each non-empty input category, emit a corresponding section (e.g. diagnosis, medications, vitals, labs, allergies) with at least one ClinicalClaim per fact, sourced from the input.
- The "alerts" array is for safety flags (blank allergies, stale critical values). It supplements sections; it does not replace them.
- If a category in the input is empty, omit that section — do not fabricate placeholder claims.

Summary requirement:
- Always populate the top-level "summary" field with a 2-3 sentence executive overview.
- The summary states WHAT IS DOCUMENTED, not what it means. Use raw values verbatim from the input data — do NOT add clinical interpretations, labels, or impressions. Examples of what to say vs not say:
  * Say "HR 118, RR 26, SpO2 88%, temp 38.9°F, lactate 4.2" — NOT "tachycardia, tachypnea, hypoxemia, fever, lactic acidosis."
  * Say "Active conditions: sepsis, pneumonia" — NOT "septic patient with pulmonary involvement."
  * Say "On norepinephrine infusion" — NOT "requires vasopressor support for hemodynamic instability."
  The clinician applies their own clinical interpretation; your job is to present documented values, not synthesize impressions.
- For age, use the pre-computed age_years field from the input. Do NOT calculate age from DOB yourself. If age_years is null, omit age from the summary entirely.
- Keep the summary specific to this patient and the values that are actually present in the input."""


def _render_prompt(ctx: BriefingContext) -> str:
    ctx_dict = asdict(ctx)

    # Tell the model exactly which sections it MUST produce based on the data
    # available, so it cannot collapse the response down to alerts only.
    required_sections: list[str] = []
    if ctx_dict.get("active_conditions"):
        required_sections.append('"diagnosis" — one ClinicalClaim per active_condition entry')
    if ctx_dict.get("active_medications"):
        required_sections.append('"medications" — one ClinicalClaim per active_medication entry')
    if ctx_dict.get("recent_vitals"):
        required_sections.append('"vitals" — one ClinicalClaim per recent_vital entry')
    if ctx_dict.get("recent_labs"):
        required_sections.append('"labs" — one ClinicalClaim per recent_lab entry')
    if ctx_dict.get("allergies"):
        required_sections.append('"allergies" — one ClinicalClaim per allergy entry')

    section_directive = (
        "The sections array MUST contain the following sections (do not omit any):\n  - "
        + "\n  - ".join(required_sections)
        if required_sections
        else "No clinical sections are derivable from the input. Return sections=[] and surface findings in alerts."
    )

    return (
        f"Generate a pre-encounter briefing for the following patient.\n\n"
        f"<patient_data>\n{json.dumps(ctx_dict, indent=2)}\n</patient_data>\n\n"
        f"{section_directive}\n\n"
        f"Call the produce_briefing tool with the complete structured briefing — "
        f"sections AND alerts. Returning only alerts is a hard error."
    )


async def generate_briefing(
    ctx: BriefingContext,
    langfuse: Langfuse | None = None,
) -> BriefingResponse:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    prompt = _render_prompt(ctx)

    trace = langfuse.trace(name="briefing-generate", user_id=ctx.patient_id) if langfuse else None
    generation = trace.generation(name="briefing-llm", model=_MODEL, input=prompt) if trace else None

    # WHY: mark the static system prompt as ephemeral so repeat Brief calls hit Anthropic's prompt cache.
    system_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": _SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    try:
        async def _call(messages: list[dict[str, Any]]) -> Any:
            return await client.messages.create(
                model=_MODEL,
                max_tokens=4096,
                system=system_blocks,
                messages=messages,
                tools=[PRODUCE_BRIEFING],
                tool_choice={"type": "any"},
            )

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        response = await _call(messages)

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            logger.error("No tool_use block in briefing response", extra={"patient_id": ctx.patient_id})
            return _fallback_briefing(ctx)

        # The model occasionally drops the `sections` key entirely when it has alerts to emit.
        # Detect that and retry once with an explicit instruction. Cheaper than a full fallback.
        ctx_dict_for_check = asdict(ctx)
        has_input_data = any(
            ctx_dict_for_check.get(k) for k in
            ("active_conditions", "active_medications", "recent_vitals", "recent_labs", "allergies")
        )
        sections_in_output = isinstance(tool_block.input.get("sections"), list) and tool_block.input.get("sections")
        if has_input_data and not sections_in_output:
            logger.warning(
                "Briefing missing sections — retrying patient_id=%s raw_keys=%s",
                ctx.patient_id, sorted(tool_block.input.keys()),
            )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": (
                "Your last response omitted the sections array. Call produce_briefing again. "
                "The sections array must contain a section for every populated input category "
                "(diagnosis, medications, vitals, labs, allergies). Each section must include "
                "ClinicalClaim entries with source_resource, source_code, source_value, and source_dt. "
                "Keep the alerts array as before. Returning sections as an empty array is not acceptable."
            )})
            response = await _call(messages)
            tool_block = next((b for b in response.content if b.type == "tool_use"), tool_block)

        if generation:
            generation.end(output=tool_block.input)

        briefing = BriefingResponse.model_validate(tool_block.input)
        logger.info(
            "Briefing model output patient_id=%s sections=%d alerts=%d raw_keys=%s",
            ctx.patient_id, len(briefing.sections), len(briefing.alerts),
            sorted(tool_block.input.keys()),
        )
        return briefing

    except ValidationError as exc:
        # Log the validation message inline (extra= dict is dropped by the default formatter
        # on this deployment) and dump the raw tool_use payload so we can see which fields
        # the model returned in a non-conforming shape.
        logger.error(
            "Briefing tool_use output failed schema validation patient_id=%s errors=%s payload=%s",
            ctx.patient_id, exc.errors(), tool_block.input if tool_block is not None else None,
        )
        return _fallback_briefing(ctx)
    except Exception as exc:
        logger.error("Briefing generation failed", extra={"patient_id": ctx.patient_id, "error": str(exc)})
        return _fallback_briefing(ctx)


def _fallback_briefing(ctx: BriefingContext) -> BriefingResponse:
    alerts: list[str] = []
    # BLANK_CODE_STATUS suppressed from briefing alerts: in this deployment
    # not every patient's OpenEMR record carries a LOINC 81638-3
    # observation, but the data is reliably present in the chart sidebar
    # the physician already sees. Leaving this as a banner alert produced
    # an "BLANK CODE STATUS IN CHART HEADER" line on most patients and
    # drowned out genuine signals. The triage / verification layers still
    # carry the underlying flag for downstream consumers that want it.
    if ctx.has_blank_allergy_section:
        alerts.append("BLANK_ALLERGY_SECTION: allergy section is empty — cannot assert NKDA")

    return BriefingResponse(
        patient_id=ctx.patient_id,
        name=ctx.name,
        sections=[],
        alerts=alerts,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
