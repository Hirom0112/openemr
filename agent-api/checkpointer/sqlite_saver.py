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

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)


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

    async def append(self, session_id: str, role: str, content: str, metadata: dict | None = None) -> int:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(turn_index) + 1, 0) FROM conversation_turns WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            turn_index: int = row[0] if row else 0

            await db.execute(
                "INSERT INTO conversation_turns (session_id, turn_index, role, content, metadata) VALUES (?, ?, ?, ?, ?)",
                (session_id, turn_index, role, content, json.dumps(metadata) if metadata else None),
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
            {"role": role, "content": content, **(json.loads(meta) if meta else {})}
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
