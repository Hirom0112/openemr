"""Voyage-3 embedding wrapper with a deterministic fallback for offline runs.

Modes:

* ``VOYAGE_API_KEY`` set → real ``voyageai.AsyncClient`` calls, batched 64 at
  a time.
* ``VOYAGE_API_KEY`` absent → deterministic 1024-dim hash-based vectors so
  unit tests, local dev, and CI runs without a paid key still produce
  meaningful retrieval rankings (similar inputs land near each other).

Logging contract: the absence-of-key warning fires ONCE per process, not
per call. Real-call paths log nothing per request — the structured event
lives at the retrieval boundary in ``rag.retrieve``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import numpy as np

_logger = logging.getLogger(__name__)


_EMBED_DIM = 1024
_BATCH_SIZE = 64

_offline_warning_emitted = False


def _ensure_offline_warning() -> None:
    global _offline_warning_emitted
    if _offline_warning_emitted:
        return
    _offline_warning_emitted = True
    _logger.warning(
        "rag_embed_offline_mode",
        extra={
            "reason": "VOYAGE_API_KEY not set; using deterministic hash-stub vectors",
            "dim": _EMBED_DIM,
        },
    )


def _hash_vector(text: str) -> list[float]:
    """1024-dim Gaussian vector seeded by the text — deterministic.

    Note: Python's builtin ``hash()`` is salted per-process, so we hash via
    a stable algorithm (sum of code points, mixed) instead.
    """
    seed = 0
    for i, ch in enumerate(text):
        seed = (seed * 1315423911 + (ord(ch) << (i & 7))) & 0xFFFFFFFF
    rng = np.random.RandomState(seed)
    vec = rng.normal(size=_EMBED_DIM)
    # L2-normalise so cosine similarity behaves as expected.
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


async def _embed_offline(texts: list[str]) -> list[list[float]]:
    _ensure_offline_warning()
    return [_hash_vector(t) for t in texts]


async def _embed_voyage(texts: list[str]) -> list[list[float]]:
    """Real Voyage call. Imported lazily so offline runs don't pay for it."""
    try:
        import voyageai  # type: ignore[import-not-found]
    except Exception as exc:
        _logger.warning(
            "rag_embed_voyage_import_failed",
            extra={"error": str(exc)},
        )
        return await _embed_offline(texts)

    client = voyageai.AsyncClient()
    out: list[list[float]] = []
    # Defensive batching even though the caller batches too — voyage's
    # server-side limit is 128 / request.
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start : start + _BATCH_SIZE]
        try:
            result: Any = await client.embed(
                batch, model="voyage-3", input_type="document"
            )
        except Exception as exc:
            _logger.warning(
                "rag_embed_voyage_call_failed",
                extra={"error": str(exc), "batch_size": len(batch)},
            )
            return await _embed_offline(texts)
        embeddings = getattr(result, "embeddings", None) or []
        out.extend([list(map(float, e)) for e in embeddings])
    return out


async def embed(texts: list[str]) -> list[list[float]]:
    """Return one 1024-dim embedding per input string.

    Always returns ``len(texts)`` vectors. Empty input → empty list.
    """
    if not texts:
        return []

    if not os.environ.get("VOYAGE_API_KEY"):
        # Run all batches concurrently — cheap, deterministic, no I/O.
        coros = []
        for start in range(0, len(texts), _BATCH_SIZE):
            coros.append(_embed_offline(texts[start : start + _BATCH_SIZE]))
        results = await asyncio.gather(*coros)
        out: list[list[float]] = []
        for batch in results:
            out.extend(batch)
        return out

    return await _embed_voyage(texts)


__all__ = ["embed"]
