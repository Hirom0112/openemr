"""Multi-turn conversation handler for UC-3 Targeted Record Query.

Maintains conversation continuity across turns using RedisSaver (primary)
with SqliteSaver fallback.  Each turn:
  1. Routes the query to the right FHIR resource(s).
  2. Fetches records (from Redis cache or live FHIR).
  3. Calls Claude via tool_use with full conversation history + new FHIR context.
  4. Appends both user turn and assistant response to the checkpointer.
  5. Returns a cited answer with source attribution.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
import redis.asyncio as aioredis
from langfuse import Langfuse

from agent.response_schemas import PRODUCE_QUERY_ANSWER
from checkpointer.redis_saver import RedisSaver
from checkpointer.sqlite_saver import SqliteSaver
from config import settings
from query.fhir_search import search_for_patient
from query.router import route as route_query
from verification.domain_constraints import verify_conversation_answer

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_EMPTY_NARRATIVES: dict[str, str] = {
    "Condition": "No active or documented conditions are on file in the chart.",
    "MedicationRequest": "No active or documented medication orders are on file in the chart.",
    "AllergyIntolerance": "No documented allergies or intolerances are on file in the chart.",
    "Observation": "No matching observations are on file for the time window searched.",
    "Procedure": "No documented procedures are on file in the chart.",
    "DiagnosticReport": "No diagnostic reports are on file for the time window searched.",
    "Encounter": "No encounter records are documented in the chart for the requested timeframe.",
    "Patient": "No patient demographics could be retrieved.",
}


def _empty_records_narrative(resource: str) -> str:
    """Honest, action-oriented message when a query returns zero records.

    Replaces the prior generic "I was unable to retrieve an answer" string,
    which read as a tool failure and prompted the user to retry instead of
    moving on. Falls back to a clear no-match message for unknown resources.
    """
    return _EMPTY_NARRATIVES.get(
        resource,
        f"No matching {resource} records found in the chart.",
    )


def _simplify_for_llm(res: dict, resource_type: str) -> dict:
    """Reduce a raw FHIR resource to the fields a clinician would read.

    Raw FHIR is deeply nested (code.coding[0].display, clinicalStatus.coding[0].code,
    valueQuantity.value/unit, etc.). The query LLM kept failing to extract
    answers because it couldn't reliably navigate the nesting, then defaulted
    to "I was unable to retrieve". Simplified flat dicts are unambiguous and
    ~10x smaller per record (more records fit per turn budget too).
    """
    if not isinstance(res, dict):
        return res

    def _coding_display(field: dict | None) -> str:
        if not isinstance(field, dict):
            return ""
        coding = (field.get("coding") or [{}])[0]
        return coding.get("display") or field.get("text") or ""

    def _status(field: dict | str | None) -> str:
        if isinstance(field, str):
            return field
        if not isinstance(field, dict):
            return ""
        coding = (field.get("coding") or [{}])[0]
        return coding.get("code") or coding.get("display") or ""

    out: dict[str, str] = {"resource_type": resource_type}
    if resource_type == "Condition":
        out["name"] = _coding_display(res.get("code"))
        out["clinical_status"] = _status(res.get("clinicalStatus"))
        out["verification_status"] = _status(res.get("verificationStatus"))
        out["onset"] = res.get("onsetDateTime") or res.get("recordedDate") or ""
        out["category"] = _coding_display((res.get("category") or [{}])[0]) if isinstance(res.get("category"), list) and res.get("category") else ""
    elif resource_type == "MedicationRequest":
        med = res.get("medicationCodeableConcept") or {}
        out["medication"] = _coding_display(med)
        out["status"] = res.get("status", "")
        out["intent"] = res.get("intent", "")
        # Dosage instruction text is the most physician-readable form.
        di = (res.get("dosageInstruction") or [{}])[0]
        out["dosage"] = di.get("text", "")
    elif resource_type == "Observation":
        out["name"] = _coding_display(res.get("code"))
        vq = res.get("valueQuantity") or {}
        if vq:
            out["value"] = f"{vq.get('value', '')} {vq.get('unit', '')}".strip()
        elif res.get("valueString"):
            out["value"] = res["valueString"]
        elif res.get("valueCodeableConcept"):
            out["value"] = _coding_display(res["valueCodeableConcept"])
        out["effective"] = res.get("effectiveDateTime") or ""
        # Reference range + interpretation flags help the LLM contextualize.
        rr = (res.get("referenceRange") or [{}])[0]
        if rr:
            low = (rr.get("low") or {}).get("value")
            high = (rr.get("high") or {}).get("value")
            if low is not None or high is not None:
                out["reference_range"] = f"{low or ''}-{high or ''}".strip("-")
        interp = (res.get("interpretation") or [{}])
        if interp and isinstance(interp, list):
            out["interpretation"] = _coding_display(interp[0])
    elif resource_type == "AllergyIntolerance":
        out["allergen"] = _coding_display(res.get("code"))
        out["clinical_status"] = _status(res.get("clinicalStatus"))
        out["criticality"] = res.get("criticality", "")
        rxn = (res.get("reaction") or [{}])[0]
        if rxn:
            out["reaction"] = _coding_display((rxn.get("manifestation") or [{}])[0])
    elif resource_type == "Encounter":
        out["status"] = res.get("status", "")
        out["class"] = _coding_display(res.get("class"))
        out["type"] = _coding_display((res.get("type") or [{}])[0]) if isinstance(res.get("type"), list) else ""
        period = res.get("period") or {}
        out["start"] = period.get("start", "")
        out["end"] = period.get("end", "")
    else:
        # Unknown resource type — return as-is so the LLM has SOMETHING.
        return res
    # Drop empty values to keep the JSON tight.
    return {k: v for k, v in out.items() if v}


def _format_records_fallback(resource_type: str, simplified: list[dict], query: str) -> str:
    """Deterministic answer when the LLM fails to summarise non-empty records.

    Better to give the physician an honest structured listing than the
    misleading "I was unable to retrieve" string. Format depends on
    resource type — conditions get name + status, meds get name + dosage,
    observations get name + value, etc.
    """
    if not simplified:
        return ""
    n = len(simplified)
    if resource_type == "Condition":
        lines = []
        for r in simplified[:10]:
            name = r.get("name") or "Unnamed condition"
            status = r.get("clinical_status") or ""
            onset = r.get("onset", "")[:10]
            tail = []
            if status:
                tail.append(status)
            if onset:
                tail.append(f"onset {onset}")
            suffix = f" ({', '.join(tail)})" if tail else ""
            lines.append(f"- **{name}**{suffix}")
        return f"{n} condition{'s' if n != 1 else ''} on file:\n" + "\n".join(lines)
    if resource_type == "MedicationRequest":
        lines = []
        for r in simplified[:15]:
            med = r.get("medication") or "Unnamed medication"
            dosage = r.get("dosage") or ""
            status = r.get("status") or ""
            tail = []
            if dosage:
                tail.append(dosage)
            if status and status != "active":
                tail.append(status)
            suffix = f" — {', '.join(tail)}" if tail else ""
            lines.append(f"- **{med}**{suffix}")
        return f"{n} medication{'s' if n != 1 else ''} on file:\n" + "\n".join(lines)
    if resource_type == "Observation":
        lines = []
        for r in simplified[:15]:
            name = r.get("name") or "Observation"
            val = r.get("value") or ""
            eff = r.get("effective", "")[:10]
            interp = r.get("interpretation") or ""
            tail = []
            if val:
                tail.append(val)
            if eff:
                tail.append(eff)
            if interp:
                tail.append(f"flag: {interp}")
            suffix = f" — {', '.join(tail)}" if tail else ""
            lines.append(f"- **{name}**{suffix}")
        return f"{n} observation{'s' if n != 1 else ''} on file:\n" + "\n".join(lines)
    if resource_type == "AllergyIntolerance":
        lines = []
        for r in simplified[:10]:
            allergen = r.get("allergen") or "Unnamed allergen"
            criticality = r.get("criticality") or ""
            reaction = r.get("reaction") or ""
            tail = []
            if criticality:
                tail.append(criticality)
            if reaction:
                tail.append(reaction)
            suffix = f" ({', '.join(tail)})" if tail else ""
            lines.append(f"- **{allergen}**{suffix}")
        return f"{n} allerg{'ies' if n != 1 else 'y'} on file:\n" + "\n".join(lines)
    if resource_type == "Encounter":
        lines = []
        for r in simplified[:10]:
            etype = r.get("type") or r.get("class") or "Encounter"
            start = r.get("start", "")[:10]
            status = r.get("status", "")
            tail = []
            if start:
                tail.append(start)
            if status:
                tail.append(status)
            suffix = f" — {', '.join(tail)}" if tail else ""
            lines.append(f"- **{etype}**{suffix}")
        return f"{n} encounter{'s' if n != 1 else ''} on file:\n" + "\n".join(lines)
    return f"{n} {resource_type} record{'s' if n != 1 else ''} on file (raw extract — verify in chart)."


_SYSTEM = """You are a clinical record assistant for a rounding hospitalist.
You have access to a patient's FHIR records.  Use the produce_query_answer tool
to answer the physician's question using only the records provided.
Cite the source of every clinical value: state the value, the record type,
and the date/time.  Never speculate or extrapolate.
If the data doesn't contain an answer, state what was searched and the search window used.
Do not make treatment recommendations.
Keep answers under 100 words."""


class ConversationHandler:
    def __init__(
        self,
        redis_saver: RedisSaver,
        sqlite_saver: SqliteSaver,
        redis_client: aioredis.Redis | None = None,
        langfuse: Langfuse | None = None,
    ) -> None:
        self._redis_saver = redis_saver
        self._sqlite_saver = sqlite_saver
        self._redis = redis_client
        self._langfuse = langfuse
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def _load_history(self, session_id: str) -> list[dict]:
        try:
            return await self._redis_saver.load(session_id)
        except Exception:
            return await self._sqlite_saver.load(session_id)

    async def _save_turn(self, session_id: str, role: str, content: str) -> None:
        try:
            await self._redis_saver.append(session_id, role=role, content=content)
        except Exception as exc:
            logger.warning("Redis save failed, using SQLite", extra={"error": str(exc)})
            await self._sqlite_saver.append(session_id, role=role, content=content)

    async def answer(
        self,
        session_id: str,
        patient_id: str,
        query: str,
        records_override: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Process one turn of a multi-turn conversation.

        Returns: {"answer": str, "sources": list, "route": str, "turn": int}

        ``records_override`` lets the caller hand in pre-fetched FHIR resources
        (e.g. sliced from the cached patient bundle).  When supplied the live
        FHIR search is skipped — this is the preferred path because OpenEMR's
        FHIR endpoint (a) requires a UUID for the ``patient`` parameter (the
        bundle cache layer already resolved it) and (b) returns 0 results when
        ``clinical-status=active`` / ``status=active`` filters are applied to
        Condition / MedicationRequest searches even when matching resources
        exist.  See ``auth/fhir_client.get_bundle_for_patient`` for the same
        workaround on the briefing path.
        """
        trace = self._langfuse.trace(name="uc3-query", session_id=session_id, user_id=patient_id) if self._langfuse else None

        # Route the query
        query_route = await route_query(query, patient_id)

        # Fetch FHIR records — prefer the caller-supplied bundle slice so we
        # don't re-hit the FHIR endpoint with filters that are known to break.
        if records_override is not None:
            fhir_records = records_override
        else:
            extended = bool(any(w in query.lower() for w in ("history", "last month", "last year", "prior", "trend", "over time")))
            fhir_records = await search_for_patient(patient_id, query_route, extended=extended)

        # Build conversation messages
        history = await self._load_history(session_id)
        generation = None

        # Short-circuit when the search returned nothing — calling the LLM
        # only burns tokens to produce a generic "no records" string and tends
        # to phrase it as a tool failure ("I was unable to retrieve…") instead
        # of an honest "no matching records on file".
        if not fhir_records:
            answer_text = _empty_records_narrative(query_route.resource)
        else:
            # Unwrap bundle entries: records_override comes from bundle slicing
            # where each entry is {"fullUrl": "...", "resource": {...}}. The
            # LLM needs the actual FHIR resource at the top level — passing
            # the wrapper confuses it (it sees {fullUrl, resource} and can't
            # extract clinical fields, then says "unable to retrieve"). Same
            # unwrap pattern as agent/tools/__init__.py for medications/labs.
            unwrapped = [r.get("resource", r) if isinstance(r, dict) else r for r in fhir_records[:30]]
            # Simplify each resource to the handful of fields a clinician would
            # read (name, status, dates, value if Observation). Raw FHIR is
            # deeply nested and the LLM kept returning "unable to retrieve"
            # because it couldn't reliably parse code.coding[0].display etc.
            # The simplified records are ~10x smaller and unambiguous.
            simplified = [_simplify_for_llm(r, query_route.resource) for r in unwrapped]
            fhir_context = json.dumps(simplified, indent=2)  # cap at 30 records per turn
            user_content = (
                f"<patient_data>\n"
                f"FHIR {query_route.resource} records (patient {patient_id}):\n"
                f"{fhir_context}\n"
                f"</patient_data>\n\n"
                f"Question: {query}"
            )

            messages = [{"role": t["role"], "content": t["content"]} for t in history if t["role"] in ("user", "assistant")]
            messages.append({"role": "user", "content": user_content})

            # LLM call via tool_use
            generation = trace.generation(name="uc3-llm", model=_MODEL, input=user_content) if trace else None
            try:
                response = await self._client.messages.create(
                    model=_MODEL,
                    max_tokens=300,
                    system=_SYSTEM,
                    messages=messages,
                    tools=[PRODUCE_QUERY_ANSWER],
                    tool_choice={"type": "any"},
                )
                tool_block = next((b for b in response.content if b.type == "tool_use"), None)
                if tool_block is None:
                    logger.error("No tool_use block in UC-3 response", extra={"session_id": session_id})
                    answer_text = ""
                else:
                    answer_text = tool_block.input.get("answer", "").strip()
            except Exception as exc:
                logger.error("UC-3 LLM call failed", extra={"session_id": session_id, "error": str(exc)})
                answer_text = ""

            # Deterministic fallback: when the LLM returns empty (which it
            # has been doing intermittently even with simplified records),
            # build the answer ourselves from the simplified records the LLM
            # already saw. The user gets useful structured info instead of
            # the misleading "I was unable to retrieve" string.
            if not answer_text and simplified:
                answer_text = _format_records_fallback(query_route.resource, simplified, query)

        answer_text = verify_conversation_answer(answer_text, patient_id)

        if generation:
            generation.end(output=answer_text)

        # Persist turns
        await self._save_turn(session_id, role="user", content=query)
        await self._save_turn(session_id, role="assistant", content=answer_text)

        # Refresh TTL
        try:
            await self._redis_saver.refresh_ttl(session_id)
        except Exception:
            pass

        history_after = await self._load_history(session_id)

        return {
            "answer": answer_text,
            "route": {"resource": query_route.resource, "confidence": query_route.confidence, "source": query_route.source},
            "records_fetched": len(fhir_records),
            "turn": len(history_after) // 2,
            "session_id": session_id,
        }
