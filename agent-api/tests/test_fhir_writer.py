"""Tests for ``documents.fhir_writer`` (Slice 1.5, W2 §4.2 step 3).

The two FHIR POSTs and the REST fallback are stubbed via ``httpx.MockTransport``
so no real network IO happens.  We assert call ordering, payload shape,
LOINC selection, and the privacy invariant that no test record contains
the raw PDF bytes (or their base64 encoding).
"""

from __future__ import annotations

import base64
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.fhir_writer import (  # noqa: E402
    FhirWriteError,
    WriteResult,
    write_document,
)

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]


# Recognisable byte sequence so we can grep log records for any leak.
_SENTINEL = b"\x89PDF-SENTINEL-PRIVACY-CHECK-bytes-DO-NOT-LOG\x00"
_PDF_BYTES = _SENTINEL + b"\x00" * 64
_SENTINEL_B64 = base64.b64encode(_PDF_BYTES).decode("ascii")[:32]


# ── Transport helpers ───────────────────────────────────────────────────────


def _make_handler(
    *,
    binary_status: int = 201,
    binary_id: str = "bin-123",
    docref_status: int = 201,
    docref_id: str = "doc-456",
    binary_raise: bool = False,
    docref_raise: bool = False,
    rest_status: int = 200,
    rest_body: dict[str, Any] | None = None,
    rest_raise: bool = False,
    custom_status: int = 200,
    custom_body: dict[str, Any] | None = None,
    custom_raise: bool = False,
    captured: list[dict[str, Any]] | None = None,
):
    rest_body = rest_body if rest_body is not None else {"documentId": 9876}
    custom_body = custom_body if custom_body is not None else {"documentId": 12345}

    def _handler(request: httpx.Request) -> httpx.Response:
        # Token endpoint stub — get_access_token() may hit this.
        if request.url.path.endswith("/oauth2/default/token"):
            return httpx.Response(
                200,
                json={"access_token": "fake-bearer", "expires_in": 600},
            )

        body_text = request.content.decode("utf-8", errors="replace") if request.content else ""
        try:
            body_json = json.loads(body_text) if body_text.startswith("{") else None
        except Exception:
            body_json = None

        if captured is not None:
            captured.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "json": body_json,
                }
            )

        path = request.url.path
        if path.endswith("/fhir/Binary"):
            if binary_raise:
                raise httpx.ConnectError("simulated network error", request=request)
            if binary_status >= 400:
                return httpx.Response(binary_status, json={"error": "bad"})
            return httpx.Response(
                binary_status,
                json={"resourceType": "Binary", "id": binary_id},
            )
        if path.endswith("/fhir/DocumentReference"):
            if docref_raise:
                raise httpx.ConnectError("simulated network error", request=request)
            if docref_status >= 400:
                return httpx.Response(docref_status, json={"error": "bad"})
            return httpx.Response(
                docref_status,
                json={"resourceType": "DocumentReference", "id": docref_id},
            )
        if "/api/patient/" in path and path.endswith("/document"):
            if rest_raise:
                raise httpx.ConnectError("simulated REST error", request=request)
            return httpx.Response(rest_status, json=rest_body)
        if path.endswith("/oe-module-clinical-copilot/public/upload.php"):
            if custom_raise:
                raise httpx.ConnectError("simulated custom error", request=request)
            return httpx.Response(custom_status, json=custom_body)

        return httpx.Response(404, json={"error": f"unhandled {path}"})

    return _handler


class _PatchedAsyncClient(httpx.AsyncClient):
    """AsyncClient bound to our MockTransport at construction time."""

    _handler: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(type(self)._handler)
        super().__init__(*args, **kwargs)


def _patch_httpx(handler) -> Any:
    """Return a patcher that swaps httpx.AsyncClient for one backed by ``handler``."""
    cls = type("_BoundClient", (_PatchedAsyncClient,), {"_handler": staticmethod(handler)})
    return patch("documents.fhir_writer.httpx.AsyncClient", cls)


@pytest.fixture(autouse=True)
def _stub_token() -> Any:
    """Avoid any real password grant — get_access_token returns a fake token."""
    with patch(
        "documents.fhir_writer.get_access_token",
        new=AsyncMock(return_value="fake-bearer"),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _stub_fhir_client() -> Any:
    """Replace the imported fhir_client with an AsyncMock so nothing reaches the network."""
    with patch("documents.fhir_writer.fhir_client", new=AsyncMock()) as mock:
        yield mock


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_happy_path_writes_binary_then_documentref() -> None:
    captured: list[dict[str, Any]] = []
    handler = _make_handler(captured=captured)

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="pt-uuid-1",
            pdf_bytes=_PDF_BYTES,
            display="Lipid panel",
            doc_type_hint="lab_report",
        )

    assert isinstance(result, WriteResult)
    assert result.path == "fhir"
    assert result.binary_id == "bin-123"
    assert result.document_reference_id == "doc-456"

    # Two FHIR POSTs in order — Binary first, DocumentReference second.
    fhir_calls = [c for c in captured if "/fhir/" in c["url"]]
    assert len(fhir_calls) == 2
    assert fhir_calls[0]["url"].endswith("/fhir/Binary")
    assert fhir_calls[1]["url"].endswith("/fhir/DocumentReference")

    # Binary body shape
    bin_body = fhir_calls[0]["json"]
    assert bin_body["resourceType"] == "Binary"
    assert bin_body["contentType"] == "application/pdf"
    assert bin_body["data"] == base64.b64encode(_PDF_BYTES).decode("ascii")

    # DocumentReference references the Binary id we just minted.
    docref_body = fhir_calls[1]["json"]
    assert docref_body["resourceType"] == "DocumentReference"
    assert docref_body["status"] == "current"
    assert docref_body["subject"]["reference"] == "Patient/pt-uuid-1"
    assert docref_body["content"][0]["attachment"]["url"] == "Binary/bin-123"
    assert docref_body["content"][0]["attachment"]["title"] == "Lipid panel"


async def test_fallback_to_rest_when_binary_post_fails() -> None:
    captured: list[dict[str, Any]] = []
    handler = _make_handler(
        binary_raise=True,
        rest_body={"documentId": 9876},
        captured=captured,
    )

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="42",
            pdf_bytes=_PDF_BYTES,
        )

    assert result.path == "rest_fallback"
    assert result.document_reference_id == "rest:9876"
    assert result.binary_id == ""

    rest_calls = [c for c in captured if "/api/patient/" in c["url"]]
    assert len(rest_calls) == 1
    assert rest_calls[0]["url"].endswith("/api/patient/42/document")


async def test_falls_back_to_local_disk_when_fhir_and_rest_fail(tmp_path, monkeypatch) -> None:
    """Risk #1 fourth-tier fallback: when FHIR + REST + custom endpoint all
    4xx/5xx, the writer persists locally so the rest of the ingest
    pipeline still runs.

    On Railway this is the documented MVP behaviour for OpenEMR builds
    that advertise FHIR Binary as read-only and don't expose the
    legacy REST upload either.
    """
    import os
    # Custom endpoint also raises so the chain falls all the way through.
    handler = _make_handler(binary_raise=True, rest_raise=True, custom_raise=True)
    os.environ["LOCAL_DOC_FALLBACK_DIR"] = str(tmp_path)
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    try:
        with _patch_httpx(handler):
            result = await write_document(
                patient_id="42",
                pdf_bytes=_PDF_BYTES,
            )
    finally:
        os.environ.pop("LOCAL_DOC_FALLBACK_DIR", None)

    assert result.path == "local_disk_fallback"
    assert result.document_reference_id.startswith("local:")
    # Deterministic id from content hash → re-ingest of same bytes is idempotent.
    expected_id = result.document_reference_id
    with _patch_httpx(handler):
        result2 = await write_document(patient_id="99", pdf_bytes=_PDF_BYTES)
    assert result2.document_reference_id == expected_id


async def test_falls_back_to_custom_endpoint_when_fhir_and_rest_fail(monkeypatch) -> None:
    """Third-tier fallback: when FHIR Binary is read-only and the legacy
    REST /api/patient/.../document path 401s, the writer hits the custom
    Co-Pilot upload endpoint, which round-trips into OpenEMR's documents
    table via Document::createDocument.
    """
    captured: list[dict[str, Any]] = []
    handler = _make_handler(
        binary_raise=True,
        rest_raise=True,
        custom_status=200,
        custom_body={"documentId": 12345},
        captured=captured,
    )
    # Custom endpoint requires a JWT secret >=32 chars to mint a token.
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="42",
            pdf_bytes=_PDF_BYTES,
            doc_type_hint="lab_report",
        )

    assert result.path == "copilot_custom"
    assert result.document_reference_id == "copilot:12345"
    assert result.binary_id == ""

    custom_calls = [
        c for c in captured
        if c["url"].endswith("/oe-module-clinical-copilot/public/upload.php")
    ]
    assert len(custom_calls) == 1


async def test_raises_when_local_disk_also_fails(tmp_path, monkeypatch) -> None:
    handler = _make_handler(binary_raise=True, rest_raise=True, custom_raise=True)
    # Point the fallback at an unwritable path to force the third tier to fail.
    monkeypatch.setenv("LOCAL_DOC_FALLBACK_DIR", "/nonexistent/forbidden")
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    def _boom(*_a, **_kw):
        raise OSError("simulated unwritable")
    monkeypatch.setattr("os.makedirs", _boom)

    with _patch_httpx(handler):
        with pytest.raises(FhirWriteError) as excinfo:
            await write_document(patient_id="42", pdf_bytes=_PDF_BYTES)

    msg = str(excinfo.value)
    assert "all failed" in msg
    # Generic — must NOT leak the underlying error text.
    assert "simulated" not in msg
    assert "forbidden" not in msg


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("lab_report", "11502-2"),
        ("intake_form", "34105-7"),
        (None, "34108-1"),
    ],
)
async def test_loinc_code_picked_by_hint(hint: str | None, expected: str) -> None:
    captured: list[dict[str, Any]] = []
    handler = _make_handler(captured=captured)

    with _patch_httpx(handler):
        await write_document(
            patient_id="pt-uuid-1",
            pdf_bytes=_PDF_BYTES,
            doc_type_hint=hint,
        )

    docref_calls = [c for c in captured if c["url"].endswith("/DocumentReference")]
    assert len(docref_calls) == 1
    coding = docref_calls[0]["json"]["type"]["coding"]
    assert coding[0]["system"] == "http://loinc.org"
    assert coding[0]["code"] == expected


async def test_no_pdf_bytes_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="documents.fhir_writer")
    handler = _make_handler()

    with _patch_httpx(handler):
        await write_document(
            patient_id="pt-uuid-1",
            pdf_bytes=_PDF_BYTES,
        )

    sentinel_text = _SENTINEL.decode("latin-1")

    for record in caplog.records:
        rendered = record.getMessage()
        assert sentinel_text not in rendered
        assert _SENTINEL_B64 not in rendered
        # Inspect every extra value too.
        for key, value in record.__dict__.items():
            if key in {"args", "msg"}:
                continue
            try:
                as_text = str(value)
            except Exception:
                continue
            assert sentinel_text not in as_text, f"sentinel leaked via {key}"
            assert _SENTINEL_B64 not in as_text, f"base64 leaked via {key}"
