"""Unit tests for ``rag.embed`` — covers the offline / hash-stub mode."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

sys.path.insert(0, str(Path(__file__).parent.parent))

# Force offline mode for these tests; restored at module teardown by pytest.
os.environ.pop("VOYAGE_API_KEY", None)

import rag.embed as embed_mod  # noqa: E402
from rag.embed import _BATCH_SIZE, embed  # noqa: E402


async def test_embed_returns_correct_dimensions() -> None:
    out = await embed(["a sample chunk of text", "another chunk"])
    assert len(out) == 2
    for vec in out:
        assert len(vec) == 1024
        assert all(isinstance(v, float) for v in vec)


async def test_embed_deterministic_without_key() -> None:
    a = await embed(["sepsis hour 1 bundle"])
    b = await embed(["sepsis hour 1 bundle"])
    assert a == b
    # Different text → different vector.
    c = await embed(["completely unrelated phrase"])
    assert a != c


async def test_embed_batches_into_groups_of_64() -> None:
    """Patch the underlying offline call; assert it's invoked in 64-sized batches."""
    n = _BATCH_SIZE * 2 + 5  # 133
    inputs = [f"chunk-{i}" for i in range(n)]

    real_offline = embed_mod._embed_offline
    call_sizes: list[int] = []

    async def _spy(texts: list[str]) -> list[list[float]]:
        call_sizes.append(len(texts))
        return await real_offline(texts)

    with patch.object(embed_mod, "_embed_offline", side_effect=_spy):
        out = await embed(inputs)

    assert len(out) == n
    # Should split into [64, 64, 5]
    assert call_sizes == [_BATCH_SIZE, _BATCH_SIZE, 5]


async def test_embed_empty_input() -> None:
    assert await embed([]) == []
