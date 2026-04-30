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

    async def answer(self, session_id: str, patient_id: str, query: str) -> dict[str, Any]:
        """Process one turn of a multi-turn conversation.

        Returns: {"answer": str, "sources": list, "route": str, "turn": int}
        """
        trace = self._langfuse.trace(name="uc3-query", session_id=session_id, user_id=patient_id) if self._langfuse else None

        # Route the query
        query_route = await route_query(query, patient_id)

        # Fetch FHIR records
        extended = bool(any(w in query.lower() for w in ("history", "last month", "last year", "prior", "trend", "over time")))
        fhir_records = await search_for_patient(patient_id, query_route, extended=extended)

        # Build conversation messages
        history = await self._load_history(session_id)

        fhir_context = json.dumps(fhir_records[:30], indent=2)  # cap at 30 records per turn
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
        answer_text: str
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
                answer_text = "I was unable to retrieve an answer from the records. Please review the chart directly."
            else:
                answer_text = tool_block.input.get("answer", "").strip()
                if not answer_text:
                    answer_text = "I was unable to retrieve an answer from the records. Please review the chart directly."
        except Exception as exc:
            logger.error("UC-3 LLM call failed", extra={"session_id": session_id, "error": str(exc)})
            answer_text = "I was unable to retrieve an answer from the records. Please review the chart directly."

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
