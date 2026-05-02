"""SqliteSaver — durable fallback checkpointer backed by SQLite.

Used when Redis is unavailable (container restart, network partition).
Written with aiosqlite so it shares the FastAPI event loop.

Schema
------
Table: conversation_turns
  session_id TEXT
  turn_index INTEGER
  role       TEXT
  content    TEXT
  metadata   TEXT (JSON, nullable)
  PRIMARY KEY (session_id, turn_index)

Usage
-----
    saver = SqliteSaver(settings.sqlite_db_path)
    await saver.init()
    await saver.append(session_id, role="user", content="…")
    history = await saver.load(session_id)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

_BLOCKS_PREFIX = "__blocks__:"


def _block_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return str(obj)


def _encode_content(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, list):
        return _BLOCKS_PREFIX + json.dumps(content, default=_block_default)
    return content


def _decode_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str) and content.startswith(_BLOCKS_PREFIX):
        try:
            decoded = json.loads(content[len(_BLOCKS_PREFIX):])
            if isinstance(decoded, list):
                return decoded
        except (ValueError, TypeError):
            pass
    return content if isinstance(content, str) else str(content)


class SqliteSaver:
    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or settings.sqlite_db_path

    async def init(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    metadata   TEXT,
                    PRIMARY KEY (session_id, turn_index)
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_session ON conversation_turns (session_id)")
            await db.commit()

    async def append(
        self,
        session_id: str,
        role: str,
        content: str | list[dict[str, Any]],
        metadata: dict | None = None,
    ) -> int:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(turn_index) + 1, 0) FROM conversation_turns WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            turn_index: int = row[0] if row else 0

            encoded = _encode_content(content)
            await db.execute(
                "INSERT INTO conversation_turns (session_id, turn_index, role, content, metadata) VALUES (?, ?, ?, ?, ?)",
                (session_id, turn_index, role, encoded, json.dumps(metadata) if metadata else None),
            )
            await db.commit()

        logger.debug("SqliteSaver.append", extra={"session_id": session_id, "turn": turn_index, "role": role})
        return turn_index

    async def load(self, session_id: str) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT role, content, metadata FROM conversation_turns WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            )
            rows = await cursor.fetchall()

        return [
            {"role": role, "content": _decode_content(content), **(json.loads(meta) if meta else {})}
            for role, content, meta in rows
        ]

    async def clear(self, session_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM conversation_turns WHERE session_id = ?", (session_id,))
            await db.commit()
        logger.info("SqliteSaver.clear", extra={"session_id": session_id})

    async def exists(self, session_id: str) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM conversation_turns WHERE session_id = ? LIMIT 1",
                (session_id,),
            )
            return await cursor.fetchone() is not None
