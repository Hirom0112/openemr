"""Tests for ``GET /document/{ref}/docx-paragraphs``.

Builds a tiny in-memory DOCX so the assertions don't depend on an external
fixture file, then patches the upstream PHP shim fetch (``httpx.AsyncClient``)
so the route can be exercised without OpenEMR running. Covers:

- 200 happy path: docx bytes round-trip through ``extract_docx_paragraphs``
  and the response carries the paragraph list + meta.
- 200 graceful fallback: non-DOCX bytes return ``paragraphs: []`` with
  ``meta.load_error: True`` so the UI can fall back to the synth preview.
- 400 missing id: a reference with no trailing integer rejects up front.
- 401 / 404 upstream propagation: matches ``/binary``'s contract so the UI
  reacts the same way.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.hard_failure

sys.path.insert(0, str(Path(__file__).parent.parent))


def _ensure_event_loop() -> None:
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _make_docx_bytes() -> bytes:
    """Build a minimal but real DOCX in memory, with one heading + a para
    under it + a table cell so all three loader code paths execute."""
    import docx as _docx  # type: ignore

    doc = _docx.Document()
    # Leading-bold short paragraph qualifies as a section heading.
    p1 = doc.add_paragraph()
    run = p1.add_run("Past Medical History:")
    run.bold = True
    doc.add_paragraph("Hyperlipidemia (E78.5)")

    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Total cholesterol"
    table.rows[0].cells[1].text = "218 mg/dL"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes, content_type: str) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    """Stand-in for ``httpx.AsyncClient`` used by the route. Only ``.get``
    and ``.aclose`` are exercised by the handler; we ignore everything else.
    """

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __call__(self, *args: Any, **kwargs: Any) -> "_FakeAsyncClient":
        return self

    async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        return self._response

    async def aclose(self) -> None:  # noqa: D401 — match real signature
        return None


def _client_factory(response: _FakeResponse):
    """Return a callable usable as a drop-in for ``httpx.AsyncClient(...)``."""
    fake = _FakeAsyncClient(response)
    return lambda *a, **kw: fake


def test_docx_paragraphs_happy_path() -> None:
    _ensure_event_loop()
    import httpx as _httpx
    import main as main_mod
    from fastapi.testclient import TestClient

    docx_bytes = _make_docx_bytes()
    response = _FakeResponse(
        200,
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    with (
        patch.object(main_mod, "settings", main_mod.settings),
        patch(
            "documents.fhir_writer._mint_copilot_jwt", return_value="fake-token"
        ),
        patch.object(_httpx, "AsyncClient", _client_factory(response)),
    ):
        client = TestClient(main_mod.app)
        resp = client.get("/document/copilot:42/docx-paragraphs")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["paragraphs"], list)
    # Heading + para + 2 table cells = 4 paragraphs minimum.
    assert len(body["paragraphs"]) >= 3
    # Heading is preserved verbatim and tagged as a section after stripping ":".
    heading = body["paragraphs"][0]
    assert heading["text"] == "Past Medical History:"
    # Subsequent paragraphs inherit the heading as their section.
    assert body["paragraphs"][1]["section"] == "Past Medical History"
    # Table-cell paragraphs come after body paragraphs and carry table_row/col.
    table_cells = [p for p in body["paragraphs"] if p["table_row"] is not None]
    assert len(table_cells) == 2
    assert {p["text"] for p in table_cells} == {"Total cholesterol", "218 mg/dL"}
    # Meta surfaces the loader's audit signals.
    assert body["meta"]["load_error"] is False
    assert body["meta"]["n_paragraphs"] == len(body["paragraphs"])


def test_docx_paragraphs_non_docx_returns_empty_with_load_error() -> None:
    _ensure_event_loop()
    import httpx as _httpx
    import main as main_mod
    from fastapi.testclient import TestClient

    response = _FakeResponse(200, b"%PDF-1.4 not really a docx", "application/pdf")

    with (
        patch(
            "documents.fhir_writer._mint_copilot_jwt", return_value="fake-token"
        ),
        patch.object(_httpx, "AsyncClient", _client_factory(response)),
    ):
        client = TestClient(main_mod.app)
        resp = client.get("/document/copilot:7/docx-paragraphs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["paragraphs"] == []
    assert body["meta"]["load_error"] is True
    assert body["meta"]["n_paragraphs"] == 0


def test_docx_paragraphs_missing_id_returns_400() -> None:
    _ensure_event_loop()
    import main as main_mod
    from fastapi.testclient import TestClient

    with patch(
        "documents.fhir_writer._mint_copilot_jwt", return_value="fake-token"
    ):
        client = TestClient(main_mod.app)
        # No trailing integer in the reference id → 400 missing_id.
        resp = client.get("/document/copilot:abc/docx-paragraphs")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing_id"


@pytest.mark.parametrize("upstream_status", [401, 404])
def test_docx_paragraphs_upstream_error_propagates(upstream_status: int) -> None:
    _ensure_event_loop()
    import httpx as _httpx
    import main as main_mod
    from fastapi.testclient import TestClient

    response = _FakeResponse(
        upstream_status, b'{"error":"upstream"}', "application/json"
    )

    with (
        patch(
            "documents.fhir_writer._mint_copilot_jwt", return_value="fake-token"
        ),
        patch.object(_httpx, "AsyncClient", _client_factory(response)),
    ):
        client = TestClient(main_mod.app)
        resp = client.get("/document/copilot:99/docx-paragraphs")

    assert resp.status_code == upstream_status
