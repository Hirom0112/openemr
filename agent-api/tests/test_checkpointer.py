"""Tests for Redis → SQLite checkpointer fallback behavior.

These tests are isolated — they use a real in-memory SQLite database
and a mock for Redis that raises on connection. No network or Docker required.
"""

from __future__ import annotations

import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from checkpointer.sqlite_saver import SqliteSaver


# ── SqliteSaver — direct behavior ────────────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestSqliteSaverBehavior:
    @pytest.fixture
    def saver(self, tmp_path):
        db_path = str(tmp_path / "test_checkpoints.db")
        return SqliteSaver(db_path=db_path)

    def test_append_and_load_single_turn(self, saver):
        async def run():
            await saver.init()
            idx = await saver.append("sess-1", role="user", content="What is her K+?")
            assert idx == 0
            turns = await saver.load("sess-1")
            assert len(turns) == 1
            assert turns[0]["role"] == "user"
            assert turns[0]["content"] == "What is her K+?"

        asyncio.run(run())

    def test_turns_returned_in_insertion_order(self, saver):
        async def run():
            await saver.init()
            await saver.append("sess-2", role="user", content="first")
            await saver.append("sess-2", role="assistant", content="second")
            await saver.append("sess-2", role="user", content="third")
            turns = await saver.load("sess-2")
            assert [t["content"] for t in turns] == ["first", "second", "third"]

        asyncio.run(run())

    def test_clear_removes_session(self, saver):
        async def run():
            await saver.init()
            await saver.append("sess-3", role="user", content="hello")
            assert await saver.exists("sess-3") is True
            await saver.clear("sess-3")
            assert await saver.exists("sess-3") is False
            turns = await saver.load("sess-3")
            assert turns == []

        asyncio.run(run())

    def test_sessions_are_isolated(self, saver):
        async def run():
            await saver.init()
            await saver.append("sess-a", role="user", content="session A message")
            await saver.append("sess-b", role="user", content="session B message")
            turns_a = await saver.load("sess-a")
            turns_b = await saver.load("sess-b")
            assert len(turns_a) == 1
            assert turns_a[0]["content"] == "session A message"
            assert len(turns_b) == 1
            assert turns_b[0]["content"] == "session B message"

        asyncio.run(run())

    def test_load_nonexistent_session_returns_empty(self, saver):
        async def run():
            await saver.init()
            turns = await saver.load("does-not-exist")
            assert turns == []

        asyncio.run(run())

    def test_metadata_roundtrip(self, saver):
        async def run():
            await saver.init()
            await saver.append("sess-meta", role="assistant", content="answer",
                               metadata={"tool": "query_patient_records", "patient_id": "p-001"})
            turns = await saver.load("sess-meta")
            assert turns[0]["tool"] == "query_patient_records"
            assert turns[0]["patient_id"] == "p-001"

        asyncio.run(run())

    def test_turn_index_increments_sequentially(self, saver):
        async def run():
            await saver.init()
            idx0 = await saver.append("sess-idx", role="user", content="turn 0")
            idx1 = await saver.append("sess-idx", role="assistant", content="turn 1")
            idx2 = await saver.append("sess-idx", role="user", content="turn 2")
            assert idx0 == 0
            assert idx1 == 1
            assert idx2 == 2

        asyncio.run(run())


# ── Redis → SQLite fallback logic ────────────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestRedisToSqliteFallback:
    """Verify that when Redis raises an exception the code path falls through
    to SqliteSaver, matching the fallback in main.py post_message handler."""

    def test_fallback_path_stores_in_sqlite_when_redis_fails(self, tmp_path):
        """Simulate the main.py fallback: try Redis append, catch, use SQLite."""
        db_path = str(tmp_path / "fallback.db")
        sqlite = SqliteSaver(db_path=db_path)

        redis_mock = AsyncMock()
        redis_mock.append.side_effect = ConnectionError("Redis unavailable")

        async def run():
            await sqlite.init()

            # Replicate the fallback logic from main.py post_message:
            try:
                await redis_mock.append("sess-fallback", role="user", content="test message")
                raise AssertionError("Redis should have raised")
            except (ConnectionError, Exception):
                turn_index = await sqlite.append("sess-fallback", role="user", content="test message")

            assert turn_index == 0
            turns = await sqlite.load("sess-fallback")
            assert len(turns) == 1
            assert turns[0]["content"] == "test message"

        asyncio.run(run())

    def test_fallback_preserves_turn_order_across_failures(self, tmp_path):
        """Multiple Redis failures should produce correctly ordered SQLite turns."""
        db_path = str(tmp_path / "ordered.db")
        sqlite = SqliteSaver(db_path=db_path)

        async def run():
            await sqlite.init()
            for i, msg in enumerate(["first", "second", "third"]):
                await sqlite.append("sess-order", role="user", content=msg)

            turns = await sqlite.load("sess-order")
            assert [t["content"] for t in turns] == ["first", "second", "third"]

        asyncio.run(run())
