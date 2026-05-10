"""Phase 4.8 — concurrent isolation test for ``rag.retrieve`` per-call stats.

Pre-fix (commit before Phase 4.8): ``_LAST_RETRIEVAL_STATS`` was a
module-level dict mutated by every ``search()`` call. Two ``asyncio.gather``
sibling tasks calling ``search()`` could clobber each other's stats between
the call's return and the retriever node's ``get_last_retrieval_stats()``
read — telemetry-only today (no rubric reads it) but a real bug.

Post-fix: the stats live in a ``ContextVar``. Each task runs in its own
copied context (asyncio.gather semantics), so each task's
``get_last_retrieval_stats()`` returns its own search's stats — never a
sibling's.

This test proves the fix by stubbing ``search()`` with a no-Postgres
implementation that writes a task-distinct stats payload, then firing 8
concurrent calls and asserting each task observes its own stats.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.retrieve import (  # noqa: E402
    _set_last_retrieval_stats,
    get_last_retrieval_stats,
)


async def _fake_search_and_read(idx: int) -> dict[str, Any]:
    """Mimic ``search()``'s contract: write per-call stats, then a downstream
    consumer (e.g. retriever node) reads them. Sleep between write and read
    to widen the race window — under the pre-fix dict implementation any
    sibling task that runs in between will overwrite the dict.
    """
    payload = {
        "sparse_hits": idx,
        "dense_hits": idx * 10,
        "after_rerank": idx * 100,
        "rerank_used": False,
        "sparse_seconds": 0.0,
        "dense_seconds": 0.0,
        "merge_seconds": 0.0,
        "rerank_seconds": 0.0,
        "query_prefix": f"q-{idx:03d}",
    }
    _set_last_retrieval_stats(payload)
    # Yield to let sibling tasks interleave their writes.
    await asyncio.sleep(0.01)
    return get_last_retrieval_stats()


async def test_concurrent_search_stats_are_task_isolated() -> None:
    """8 concurrent fake searches each see their own stats, not a sibling's."""
    results = await asyncio.gather(*(_fake_search_and_read(i) for i in range(8)))

    # Each result must be the stats THAT TASK wrote — query_prefix is the
    # easy distinguisher.
    for idx, stats in enumerate(results):
        assert stats["query_prefix"] == f"q-{idx:03d}", (
            f"task {idx} observed sibling's stats: {stats}"
        )
        assert stats["sparse_hits"] == idx, f"task {idx} sparse_hits clobbered: {stats}"
        assert stats["dense_hits"] == idx * 10
        assert stats["after_rerank"] == idx * 100
