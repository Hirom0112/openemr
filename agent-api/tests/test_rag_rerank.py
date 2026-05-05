"""Unit tests for ``rag.rerank``.

Covers:
* No-key offline mode → identity ordering.
* Cohere call raising → wrapped as ``RerankUnavailable``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.rerank import RerankUnavailable, rerank  # noqa: E402


async def test_rerank_no_key_identity() -> None:
    os.environ.pop("COHERE_API_KEY", None)
    docs = ["one", "two", "three"]
    out = await rerank("any query", docs, top_n=2)
    assert len(out) == 2
    assert [i for i, _ in out] == [0, 1]
    assert out[0][1] >= out[1][1]


async def test_rerank_empty_documents_returns_empty() -> None:
    os.environ.pop("COHERE_API_KEY", None)
    assert await rerank("q", [], top_n=5) == []


async def test_rerank_falls_back_on_cohere_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "test-key")

    class _FakeCohereModule:
        class AsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def rerank(self, **kwargs: object) -> object:
                raise RuntimeError("cohere upstream 503")

    with patch.dict("sys.modules", {"cohere": _FakeCohereModule}):
        with pytest.raises(RerankUnavailable):
            await rerank("q", ["a", "b"], top_n=2)


async def test_rerank_propagates_cohere_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "test-key")

    class _Result:
        def __init__(self, idx: int, score: float) -> None:
            self.index = idx
            self.relevance_score = score

    class _Response:
        def __init__(self) -> None:
            self.results = [_Result(2, 0.9), _Result(0, 0.7)]

    class _FakeCohereModule:
        class AsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.rerank = AsyncMock(return_value=_Response())

    with patch.dict("sys.modules", {"cohere": _FakeCohereModule}):
        out = await rerank("q", ["a", "b", "c"], top_n=2)
    assert out == [(2, 0.9), (0, 0.7)]
