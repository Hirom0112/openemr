"""Tests for ``POST /document/{document_reference_id}/chat``.

We patch ``anthropic.AsyncAnthropic`` so the route never makes a network
call. Mirrors patterns in ``test_evidence_search_route.py`` and
``test_post_ingest_context.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402


def _ensure_event_loop() -> None:
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _fake_anthropic_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text, type="text")])


def _make_canned_client(response_text: str) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_fake_anthropic_response(response_text)
    )
    return client


def _make_failing_client(exc: BaseException) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=exc)
    return client


def _basic_request_body() -> dict:
    return {
        "patient_id": "p-1",
        "question": "Is the lactate elevated and what does it suggest?",
        "extraction": {
            "kind": "lab_report",
            "values": [
                {
                    "test_name": "Lactate",
                    "normalized_test_name": "lactate",
                    "value": "4.2",
                    "abnormal_flag": "high",
                }
            ],
        },
        "guidelines": [
            {
                "chunk_id": "ssc-2021-p1-000",
                "content": "Elevated lactate suggests tissue hypoperfusion.",
            }
        ],
        "history": [],
    }


def test_chat_valid_question_returns_answer_and_citations() -> None:
    _ensure_event_loop()
    import main as main_mod

    answer = (
        "Yes, the lactate value is elevated [D:lactate] which per "
        "[G:ssc-2021-p1-000] suggests tissue hypoperfusion. Repeat citation "
        "[G:ssc-2021-p1-000] should not duplicate."
    )
    fake_client = _make_canned_client(answer)

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/doc-1/chat",
            json=_basic_request_body(),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == answer
    # Both citation tokens parsed, de-duplicated, in first-seen order.
    assert body["citations_used"] == ["D:lactate", "G:ssc-2021-p1-000"]
    assert body["metadata"]["model"] == "claude-haiku-4-5-20251001"
    assert body["metadata"]["n_guidelines_provided"] == 1
    assert "request_id" in body["metadata"]
    assert fake_client.messages.create.await_count == 1


def test_chat_empty_question_returns_400() -> None:
    _ensure_event_loop()
    import main as main_mod

    payload = _basic_request_body()
    payload["question"] = "   "

    client = TestClient(main_mod.app)
    resp = client.post("/document/doc-1/chat", json=payload)
    assert resp.status_code == 400
    assert "empty" in resp.json().get("detail", "").lower()


def test_chat_anthropic_failure_returns_502() -> None:
    _ensure_event_loop()
    import main as main_mod

    fake_client = _make_failing_client(RuntimeError("upstream boom"))
    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        client = TestClient(main_mod.app)
        resp = client.post(
            "/document/doc-1/chat",
            json=_basic_request_body(),
        )

    assert resp.status_code == 502, resp.text
    assert resp.json().get("detail") == "Chat unavailable"


def test_chat_route_registered() -> None:
    _ensure_event_loop()
    import main as main_mod
    paths = {r.path for r in main_mod.app.routes}
    assert "/document/{document_reference_id}/chat" in paths
