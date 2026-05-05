"""Cohere rerank wrapper with a graceful identity fallback.

Per W2_ARCHITECTURE §6.2 / risk #9: when Cohere is unavailable (no key,
network failure, transient 5xx) the retriever falls back to merged-top-N
without re-ranking. We surface that decision via :class:`RerankUnavailable`
so the caller can observe it explicitly.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)


class RerankUnavailable(RuntimeError):
    """Raised when Cohere is reachable but the rerank call fails.

    Caller should fall back to merged-top-N. Distinct from "no key
    configured", which is an *expected* offline mode and returns identity
    ordering rather than raising.
    """


_offline_warning_emitted = False


def _ensure_offline_warning() -> None:
    global _offline_warning_emitted
    if _offline_warning_emitted:
        return
    _offline_warning_emitted = True
    _logger.warning(
        "rag_rerank_offline_mode",
        extra={
            "reason": "COHERE_API_KEY not set; identity ordering used",
        },
    )


async def rerank(
    query: str,
    documents: list[str],
    *,
    top_n: int = 5,
) -> list[tuple[int, float]]:
    """Return ``(original_index, relevance_score)`` pairs ranked best→worst.

    * No ``COHERE_API_KEY``: returns identity ordering (1.0 down by 0.01)
      truncated to ``top_n``. Caller should treat this as "rerank ran
      cheaply" rather than "rerank failed".
    * Cohere call raises any exception: re-raised as :class:`RerankUnavailable`
      so the caller can record the fallback path explicitly.
    """
    if not documents:
        return []

    if not os.environ.get("COHERE_API_KEY"):
        _ensure_offline_warning()
        n = min(top_n, len(documents))
        return [(i, max(0.0, 1.0 - (i * 0.01))) for i in range(n)]

    try:
        import cohere  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover — env-dependent
        raise RerankUnavailable(f"cohere import failed: {exc}") from exc

    try:
        client = cohere.AsyncClient()
        result: Any = await client.rerank(
            query=query,
            documents=documents,
            top_n=top_n,
            model="rerank-english-v3.0",
        )
    except Exception as exc:
        raise RerankUnavailable(f"cohere rerank failed: {exc}") from exc

    pairs: list[tuple[int, float]] = []
    for entry in getattr(result, "results", []) or []:
        idx = int(getattr(entry, "index", -1))
        score = float(getattr(entry, "relevance_score", 0.0))
        if idx >= 0:
            pairs.append((idx, score))
    return pairs


__all__ = ["rerank", "RerankUnavailable"]
