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
from typing import Literal

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

ConversationRole = Literal["user", "assistant", "tool"]


class RedisSaver:
    def __init__(self, client: aioredis.Redis) -> None:
        self._redis = client

    def _key(self, session_id: str) -> str:
        return f"copilot:checkpoint:{session_id}"

    async def append(self, session_id: str, role: ConversationRole, content: str, metadata: dict | None = None) -> int:
        """Append one turn and return the new turn index."""
        key = self._key(session_id)

        turn_index: int = await self._redis.hlen(key)
        field = f"t{turn_index}"
        payload = json.dumps({"role": role, "content": content, **(metadata or {})})

        pipe = self._redis.pipeline()
        pipe.hset(key, field, payload)
        pipe.expire(key, settings.redis_ttl_seconds)
        await pipe.execute()

        logger.debug("RedisSaver.append", extra={"session_id": session_id, "turn": turn_index, "role": role})
        return turn_index

    async def load(self, session_id: str) -> list[dict]:
        """Return turns in insertion order."""
        key = self._key(session_id)
        raw: dict[bytes, bytes] = await self._redis.hgetall(key)
        if not raw:
            return []

        turns = sorted(raw.items(), key=lambda kv: int(kv[0].decode().lstrip("t")))
        return [json.loads(v) for _, v in turns]

    async def clear(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))
        logger.info("RedisSaver.clear", extra={"session_id": session_id})

    async def exists(self, session_id: str) -> bool:
        return bool(await self._redis.exists(self._key(session_id)))

    async def refresh_ttl(self, session_id: str) -> None:
        """Extend TTL on an active session without modifying content."""
        await self._redis.expire(self._key(session_id), settings.redis_ttl_seconds)
