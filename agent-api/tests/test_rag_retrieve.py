"""Integration tests for ``rag.retrieve.search`` against a real Postgres.

Skips cleanly when no Postgres is reachable — same pattern as
``test_documents_store.py``. Seeds 6 synthetic chunks, then asserts:

* lactate-sepsis query returns one of the seeded sepsis chunks at the top.
* When rerank raises ``RerankUnavailable``, the merged-top-N fallback path
  is exercised and still returns a non-empty result.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

sys.path.insert(0, str(Path(__file__).parent.parent))


_DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
_TEST_DSN = os.environ.get("COPILOT_TEST_PG_DSN", _DEFAULT_DSN)


def _probe_postgres(dsn: str) -> str | None:
    try:
        import asyncpg  # type: ignore[import-not-found]
    except Exception as exc:
        return f"asyncpg unavailable: {exc}"

    async def _try() -> bool:
        conn = await asyncpg.connect(dsn, timeout=2.0)
        try:
            # Confirm pgvector is available; without it the DDL/queries fail.
            row = await conn.fetchrow(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            )
            if not row:
                return False
        finally:
            await conn.close()
        return True

    try:
        ok = asyncio.run(_try())
    except Exception as exc:
        return f"postgres unavailable at {dsn}: {type(exc).__name__}"
    if not ok:
        return "pgvector extension not installed in test database"
    return None


_SKIP_REASON = _probe_postgres(_TEST_DSN)
pytestmark.append(
    pytest.mark.skipif(
        _SKIP_REASON is not None,
        reason=_SKIP_REASON or "postgres unavailable",
    )
)


if _SKIP_REASON is None:  # pragma: no branch — covered by gate
    os.environ.setdefault("COPILOT_AUDIT_DB_URL", _TEST_DSN)
    # Hard-set so config.settings picks it up regardless of which env name
    # the runtime expects (settings reads AUDIT_DB_URL).
    os.environ["AUDIT_DB_URL"] = _TEST_DSN

    from audit import writer as audit_writer  # noqa: E402
    from config import settings  # noqa: E402

    settings.audit_db_url = _TEST_DSN  # type: ignore[attr-defined]

    from rag import embed as embed_mod  # noqa: E402
    from rag import retrieve as retrieve_mod  # noqa: E402
    from rag.rerank import RerankUnavailable  # noqa: E402


_SEED_ROWS: list[tuple[str, str, str]] = [
    ("test-rag-sepsis-1", "sepsis-test", "Lactate is the early marker for sepsis bundle resuscitation initiation."),
    ("test-rag-sepsis-2", "sepsis-test", "The hour-1 sepsis bundle requires lactate measurement and prompt antibiotics."),
    ("test-rag-sepsis-3", "sepsis-test", "Septic shock resuscitation depends on serial lactate clearance to bundle goals."),
    ("test-rag-aki-1", "aki-test", "Stage 2 acute kidney injury is defined by creatinine multiples of baseline."),
    ("test-rag-glucose-1", "glucose-test", "Inpatient insulin infusion targets a glucose range during hospitalization."),
    ("test-rag-glucose-2", "glucose-test", "Basal bolus regimen in non critical adults uses divided units per kilogram."),
]


async def _seed(pool: Any) -> None:
    # Use the same offline embedder as the search path so deterministic
    # vectors line up between seed and query.
    contents = [r[2] for r in _SEED_ROWS]
    vectors = await embed_mod.embed(contents)
    rows = []
    for (chunk_id, source_id, content), vec in zip(_SEED_ROWS, vectors):
        vec_lit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        rows.append((chunk_id, source_id, "Test Doc", "TEST SECTION", 1, "2024-01-01", content, vec_lit))
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO copilot_guideline_chunks
                (chunk_id, source_id, document_title, section, page_number,
                 indexed_version_date, content, embedding)
            VALUES ($1, $2, $3, $4, $5, $6::date, $7, $8::vector)
            ON CONFLICT (chunk_id) DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding
            """,
            rows,
        )


async def _cleanup(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM copilot_guideline_chunks WHERE chunk_id LIKE 'test-rag-%'"
        )


async def _ensure_schema(pool: Any) -> None:
    """Apply the schema bits this test needs — idempotent."""
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS copilot_guideline_chunks (
                chunk_id              TEXT PRIMARY KEY,
                source_id             TEXT NOT NULL,
                document_title        TEXT NOT NULL,
                section               TEXT,
                page_number           INTEGER,
                indexed_version_date  DATE NOT NULL,
                content               TEXT NOT NULL,
                content_tsv           TSVECTOR
                                        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
                embedding             vector(1024),
                indexed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS copilot_guideline_chunks_tsv_idx "
            "ON copilot_guideline_chunks USING GIN (content_tsv)"
        )


@pytest.fixture(autouse=True)
async def _seeded_pool() -> Any:
    pool = await audit_writer.get_pool()
    assert pool is not None, "audit pool failed to initialise"
    await _ensure_schema(pool)
    await _cleanup(pool)
    await _seed(pool)
    yield pool
    await _cleanup(pool)


async def test_search_surfaces_relevant_chunk() -> None:
    snippets = await retrieve_mod.search("lactate sepsis bundle", k=3)
    assert snippets, "expected at least one snippet"
    top_ids = [s.chunk_id for s in snippets]
    sepsis_ids = {"test-rag-sepsis-1", "test-rag-sepsis-2", "test-rag-sepsis-3"}
    assert any(cid in sepsis_ids for cid in top_ids[:1]), (
        f"expected a sepsis chunk in top result, got {top_ids}"
    )


async def test_search_uses_fallback_when_cohere_unavailable() -> None:
    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RerankUnavailable("simulated outage")

    with patch.object(retrieve_mod, "cohere_rerank", side_effect=_boom):
        snippets = await retrieve_mod.search("lactate sepsis bundle", k=4)
    assert snippets, "fallback path should still return results"
    assert len(snippets) <= 4
