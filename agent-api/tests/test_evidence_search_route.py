"""Tests for the ``POST /evidence/search`` route.

We patch ``rag.retrieve.search`` so the route can be exercised without
Postgres or Voyage in scope.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402


def _ensure_event_loop() -> None:
    """Py3.9: ``asyncio.Lock()`` at module-import time wants a running loop.

    Importing ``main`` triggers ``auth.fhir_client`` which constructs an
    ``asyncio.Lock`` at module scope. Newer pytest-asyncio versions don't
    set up a loop unless the test is asyncio-marked, so we install one
    eagerly when these sync tests run after async modules.
    """
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _fake_snippets() -> list:
    from rag.retrieve import Snippet
    return [
        Snippet(
            chunk_id="ssc-2021-p1-000",
            source_id="ssc-2021",
            document_title="Surviving Sepsis Campaign 2021",
            section="SEPSIS HOUR-1 BUNDLE",
            page_number=1,
            indexed_version_date=_dt.date(2021, 10, 1),
            content="Synthetic content about lactate measurement.",
            relevance_score=0.92,
        )
    ]


def test_evidence_search_returns_snippets() -> None:
    _ensure_event_loop()
    import main as main_mod
    fake = _fake_snippets()
    with patch("rag.retrieve.search", new=AsyncMock(return_value=fake)):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/evidence/search",
            json={"query": "hour-1 sepsis bundle", "k": 3},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "hour-1 sepsis bundle"
    assert isinstance(body["snippets"], list)
    assert len(body["snippets"]) == 1
    snip = body["snippets"][0]
    assert snip["chunk_id"] == "ssc-2021-p1-000"
    assert snip["source_id"] == "ssc-2021"
    assert snip["section"] == "SEPSIS HOUR-1 BUNDLE"
    assert snip["indexed_version_date"] == "2021-10-01"
    assert snip["relevance_score"] == pytest.approx(0.92)


def test_empty_query_returns_400() -> None:
    _ensure_event_loop()
    import main as main_mod
    client = TestClient(main_mod.app)
    resp = client.post("/evidence/search", json={"query": "   "})
    assert resp.status_code == 400
    assert "empty" in resp.json().get("detail", "").lower()


def test_evidence_search_route_registered() -> None:
    _ensure_event_loop()
    import main as main_mod
    paths = {r.path for r in main_mod.app.routes}
    assert "/evidence/search" in paths
