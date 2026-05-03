from __future__ import annotations

"""RedisSaver — conversation-state checkpointer backed by Redis.

Schema
------
Key  : copilot:checkpoint:{session_id}
Type : Redis Hash
TTL  : settings.redis_ttl_seconds (default 7200 s / 2 h)

Each hash field is a turn key ("t0", "t1", …) whose value is a JSON-encoded
ConversationTurn.  Appending a new turn is O(1) and does not require reading
the full history.

Usage
-----
    saver = RedisSaver(redis_client)
    await saver.append(session_id, role="user", content="What is her potassium?")
    history = await saver.load(session_id)
    await saver.clear(session_id)
"""

import json
import logging
from typing import Any, Literal

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

ConversationRole = Literal["user", "assistant", "tool"]

# Sentinel prefix for a stored content value that is a JSON-encoded list of
# Anthropic content blocks (text, tool_use, tool_result), as opposed to a
# plain narrative string.  Picked as something that cannot occur in normal
# free-text content.
_BLOCKS_PREFIX = "__blocks__:"


def _encode_content(content: str | list[dict[str, Any]]) -> str:
    """Serialize content for storage.  Lists are tagged with _BLOCKS_PREFIX."""
    if isinstance(content, list):
        return _BLOCKS_PREFIX + json.dumps(content, default=_block_default)
    return content


def _block_default(obj: Any) -> Any:
    # Anthropic SDK content blocks are pydantic models; fall back to .dict() / repr.
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return str(obj)


def _decode_content(content: Any) -> str | list[dict[str, Any]]:
    """Inverse of _encode_content; tolerates legacy plain-string entries."""
    if isinstance(content, str) and content.startswith(_BLOCKS_PREFIX):
        try:
            decoded = json.loads(content[len(_BLOCKS_PREFIX):])
            if isinstance(decoded, list):
                return decoded
        except (ValueError, TypeError):
            pass
    return content if isinstance(content, str) else str(content)


class RedisSaver:
    def __init__(self, client: aioredis.Redis) -> None:
        self._redis = client

    def _key(self, session_id: str) -> str:
        return f"copilot:checkpoint:{session_id}"

    async def append(
        self,
        session_id: str,
        role: ConversationRole,
        content: str | list[dict[str, Any]],
        metadata: dict | None = None,
    ) -> int:
        """Append one turn and return the new turn index.

        ``content`` may be a plain string (legacy / final narrative) or a list
        of Anthropic content blocks (text, tool_use, tool_result).  Lists are
        tagged with a sentinel prefix so ``load`` can return them verbatim.
        """
        key = self._key(session_id)

        turn_index: int = await self._redis.hlen(key)
        field = f"t{turn_index}"
        encoded = _encode_content(content)
        payload = json.dumps({"role": role, "content": encoded, **(metadata or {})})

        pipe = self._redis.pipeline()
        pipe.hset(key, field, payload)
        pipe.expire(key, settings.redis_ttl_seconds)
        await pipe.execute()

        logger.debug("RedisSaver.append", extra={"session_id": session_id, "turn": turn_index, "role": role})
        return turn_index

    async def load(self, session_id: str) -> list[dict]:
        """Return turns in insertion order.

        Tolerates both bytes and str responses from Redis: clients configured
        with ``decode_responses=True`` return str keys/values, otherwise bytes.
        Without this defence ``kv[0].decode()`` raises ``AttributeError`` on
        str — silently dropping the entire conversation history because the
        dispatcher catches the exception and falls back to an empty load.
        """
        key = self._key(session_id)
        raw = await self._redis.hgetall(key)
        if not raw:
            return []

        def _as_str(v: bytes | str) -> str:
            return v.decode() if isinstance(v, (bytes, bytearray)) else v

        turns = sorted(raw.items(), key=lambda kv: int(_as_str(kv[0]).lstrip("t")))
        result: list[dict] = []
        for _, v in turns:
            entry = json.loads(_as_str(v))
            if "content" in entry:
                entry["content"] = _decode_content(entry["content"])
            result.append(entry)
        return result

    async def clear(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))
        logger.info("RedisSaver.clear", extra={"session_id": session_id})

    async def exists(self, session_id: str) -> bool:
        return bool(await self._redis.exists(self._key(session_id)))

    async def refresh_ttl(self, session_id: str) -> None:
        """Extend TTL on an active session without modifying content."""
        await self._redis.expire(self._key(session_id), settings.redis_ttl_seconds)
