"""Hybrid sparse+dense retrieval with optional Cohere rerank.

Pipeline (W2 §6.2):
    1. Embed the query (or hash-stub when VOYAGE_API_KEY is absent).
    2. In parallel: sparse ts_rank query + dense pgvector cosine query, each
       returning up to 20 chunk_ids with scores.
    3. Merge by chunk_id, keeping max(sparse_norm, dense_norm) per chunk.
    4. Hydrate the top ~30 candidates from the table.
    5. Pass through Cohere rerank; on RerankUnavailable, fall back to the
       merged top-N capped at ``fallback_top_n`` (per risk #9).
    6. Truncate to ``k`` and return.

Logging contract: one structured event per call, **no chunk content**.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from typing import Any, NamedTuple

from audit.writer import get_pool
from rag.embed import embed
from rag.rerank import RerankUnavailable, rerank as cohere_rerank

_logger = logging.getLogger(__name__)


class Snippet(NamedTuple):
    chunk_id: str
    source_id: str
    document_title: str
    section: str | None
    page_number: int | None
    indexed_version_date: _dt.date
    content: str
    relevance_score: float


_SPARSE_SQL = """
    SELECT chunk_id,
           ts_rank(content_tsv, websearch_to_tsquery('english', $1)) AS score
      FROM copilot_guideline_chunks
     WHERE content_tsv @@ websearch_to_tsquery('english', $1)
     ORDER BY score DESC
     LIMIT 20
"""

_DENSE_SQL = """
    SELECT chunk_id,
           1 - (embedding <=> $1::vector) AS score
      FROM copilot_guideline_chunks
     WHERE embedding IS NOT NULL
     ORDER BY embedding <=> $1::vector
     LIMIT 20
"""

_HYDRATE_SQL = """
    SELECT chunk_id, source_id, document_title, section, page_number,
           indexed_version_date, content
      FROM copilot_guideline_chunks
     WHERE chunk_id = ANY($1::text[])
"""


def _vector_literal(vec: list[float]) -> str:
    """pgvector accepts ``[1.0,2.0,...]`` text-cast — cheaper than asyncpg codec."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _normalize(scores: list[tuple[str, float]]) -> dict[str, float]:
    """Min-max normalise scores into [0, 1]; empty input → empty."""
    if not scores:
        return {}
    vals = [s for _, s in scores]
    lo = min(vals)
    hi = max(vals)
    span = hi - lo
    if span <= 1e-12:
        return {cid: 1.0 for cid, _ in scores}
    return {cid: (s - lo) / span for cid, s in scores}


_LAST_RETRIEVAL_STATS: dict[str, Any] = {}
"""Per-call telemetry from the most recent ``search()`` invocation in this
event-loop task. Populated unconditionally before each return path. Read by
``graph.nodes.retriever`` to emit Prometheus observations + the
``retrieval_completed`` audit row without forcing ``rag`` to import
``agent.metrics`` (would break the ``rag-isolated`` import-linter contract).
Per-call only — the dict is overwritten each call, never accumulated.
"""


def get_last_retrieval_stats() -> dict[str, Any]:
    """Return the per-call stats dict written by the most recent ``search()``.

    Returns an empty dict if ``search()`` has not been called yet on this
    interpreter. The shape is::

        {
            "sparse_hits": int,
            "dense_hits": int,
            "after_rerank": int,
            "rerank_used": bool,
            "sparse_seconds": float,
            "dense_seconds": float,
            "merge_seconds": float,
            "rerank_seconds": float,
            "query_prefix": str,  # first 30 chars only
        }
    """
    return dict(_LAST_RETRIEVAL_STATS)


async def search(
    query: str,
    *,
    k: int = 5,
    fallback_top_n: int = 8,
) -> list[Snippet]:
    """Sparse + dense + rerank. Returns up to ``k`` snippets (may be empty)."""
    t0 = time.monotonic()
    query = (query or "").strip()
    _LAST_RETRIEVAL_STATS.clear()
    _LAST_RETRIEVAL_STATS["query_prefix"] = query[:30]
    if not query:
        _LAST_RETRIEVAL_STATS.update({
            "sparse_hits": 0, "dense_hits": 0, "after_rerank": 0,
            "rerank_used": False,
            "sparse_seconds": 0.0, "dense_seconds": 0.0,
            "merge_seconds": 0.0, "rerank_seconds": 0.0,
        })
        return []

    pool = await get_pool()
    if pool is None:
        _logger.warning(
            "rag_search_pool_unavailable",
            extra={"query_prefix": query[:30]},
        )
        _LAST_RETRIEVAL_STATS.update({
            "sparse_hits": 0, "dense_hits": 0, "after_rerank": 0,
            "rerank_used": False,
            "sparse_seconds": 0.0, "dense_seconds": 0.0,
            "merge_seconds": 0.0, "rerank_seconds": 0.0,
        })
        return []

    # 1. Embed the query.
    query_vecs = await embed([query])
    qvec = query_vecs[0] if query_vecs else []
    qvec_literal = _vector_literal(qvec) if qvec else None

    # 2. Sparse + dense in parallel — capture per-phase timing.
    sparse_t0 = time.monotonic()

    async def _sparse() -> list[tuple[str, float]]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SPARSE_SQL, query)
        return [(str(r["chunk_id"]), float(r["score"])) for r in rows]

    async def _dense() -> list[tuple[str, float]]:
        if not qvec_literal:
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(_DENSE_SQL, qvec_literal)
        return [(str(r["chunk_id"]), float(r["score"])) for r in rows]

    sparse_hits, dense_hits = await asyncio.gather(_sparse(), _dense())
    parallel_seconds = max(0.0, time.monotonic() - sparse_t0)
    # Sparse and dense run concurrently; we attribute the gather wall-clock
    # to both so dashboards pick up the dominant phase. This is a deliberate
    # over-attribution rather than a more invasive per-coroutine timer.
    sparse_seconds = parallel_seconds
    dense_seconds = parallel_seconds if qvec_literal else 0.0

    # 3. Merge: max of normalised scores per chunk_id.
    merge_t0 = time.monotonic()
    sparse_norm = _normalize(sparse_hits)
    dense_norm = _normalize(dense_hits)
    merged: dict[str, float] = {}
    for cid, score in sparse_norm.items():
        merged[cid] = max(merged.get(cid, 0.0), score)
    for cid, score in dense_norm.items():
        merged[cid] = max(merged.get(cid, 0.0), score)
    merge_seconds = max(0.0, time.monotonic() - merge_t0)

    if not merged:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _logger.info(
            "rag_search_complete",
            extra={
                "sparse_hits": len(sparse_hits),
                "dense_hits": len(dense_hits),
                "rerank_used": False,
                "n_results": 0,
                "duration_ms": duration_ms,
                "query_prefix": query[:30],
            },
        )
        _LAST_RETRIEVAL_STATS.update({
            "sparse_hits": len(sparse_hits),
            "dense_hits": len(dense_hits),
            "after_rerank": 0,
            "rerank_used": False,
            "sparse_seconds": sparse_seconds,
            "dense_seconds": dense_seconds,
            "merge_seconds": merge_seconds,
            "rerank_seconds": 0.0,
        })
        return []

    candidate_ids = sorted(merged.keys(), key=lambda c: merged[c], reverse=True)[:30]

    # 4. Hydrate.
    async with pool.acquire() as conn:
        rows = await conn.fetch(_HYDRATE_SQL, candidate_ids)
    rows_by_id = {str(r["chunk_id"]): r for r in rows}
    # Preserve merged-score order while filtering hydration losers.
    ordered_candidates = [
        rows_by_id[cid] for cid in candidate_ids if cid in rows_by_id
    ]
    if not ordered_candidates:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _logger.info(
            "rag_search_complete",
            extra={
                "sparse_hits": len(sparse_hits),
                "dense_hits": len(dense_hits),
                "rerank_used": False,
                "n_results": 0,
                "duration_ms": duration_ms,
                "query_prefix": query[:30],
            },
        )
        _LAST_RETRIEVAL_STATS.update({
            "sparse_hits": len(sparse_hits),
            "dense_hits": len(dense_hits),
            "after_rerank": 0,
            "rerank_used": False,
            "sparse_seconds": sparse_seconds,
            "dense_seconds": dense_seconds,
            "merge_seconds": merge_seconds,
            "rerank_seconds": 0.0,
        })
        return []

    documents = [str(r["content"]) for r in ordered_candidates]

    # 5. Rerank or fallback.
    rerank_t0 = time.monotonic()
    rerank_used = False
    final_pairs: list[tuple[int, float]]
    try:
        pairs = await cohere_rerank(query, documents, top_n=max(k, 1))
        if pairs:
            rerank_used = True
            final_pairs = pairs
        else:
            # Empty-pair return (offline-mode + zero docs guard already
            # handled above; treat empty as "no rerank performed").
            final_pairs = [
                (i, merged.get(str(ordered_candidates[i]["chunk_id"]), 0.0))
                for i in range(min(fallback_top_n, len(ordered_candidates)))
            ]
    except RerankUnavailable as exc:
        _logger.warning(
            "rag_rerank_unavailable_falling_back",
            extra={"error": str(exc)},
        )
        final_pairs = [
            (i, merged.get(str(ordered_candidates[i]["chunk_id"]), 0.0))
            for i in range(min(fallback_top_n, len(ordered_candidates)))
        ]

    # 6. Truncate to k and assemble Snippets.
    final_pairs = final_pairs[:k]
    snippets: list[Snippet] = []
    for idx, score in final_pairs:
        if idx < 0 or idx >= len(ordered_candidates):
            continue
        row = ordered_candidates[idx]
        snippets.append(
            Snippet(
                chunk_id=str(row["chunk_id"]),
                source_id=str(row["source_id"]),
                document_title=str(row["document_title"]),
                section=row["section"],
                page_number=row["page_number"],
                indexed_version_date=row["indexed_version_date"],
                content=str(row["content"]),
                relevance_score=float(score),
            )
        )

    rerank_seconds = max(0.0, time.monotonic() - rerank_t0)
    duration_ms = int((time.monotonic() - t0) * 1000)
    _logger.info(
        "rag_search_complete",
        extra={
            "sparse_hits": len(sparse_hits),
            "dense_hits": len(dense_hits),
            "rerank_used": rerank_used,
            "n_results": len(snippets),
            "duration_ms": duration_ms,
            "query_prefix": query[:30],
        },
    )
    _LAST_RETRIEVAL_STATS.update({
        "sparse_hits": len(sparse_hits),
        "dense_hits": len(dense_hits),
        "after_rerank": len(snippets),
        "rerank_used": rerank_used,
        "sparse_seconds": sparse_seconds,
        "dense_seconds": dense_seconds,
        "merge_seconds": merge_seconds,
        "rerank_seconds": rerank_seconds,
    })
    return snippets


__all__ = ["Snippet", "search"]


def _coerce_dict(snippet: Snippet) -> dict[str, Any]:
    """Helper for JSON serialisation; ``date`` → ISO string."""
    d = snippet._asdict()
    if isinstance(d.get("indexed_version_date"), _dt.date):
        d["indexed_version_date"] = d["indexed_version_date"].isoformat()
    return d
