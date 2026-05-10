"""Tests for the Phase 6.1 legacy-documents mirror in ``documents.fhir_writer``.

After agent-api writes a Co-Pilot document via FHIR ``Binary`` +
``DocumentReference`` (or its legacy REST fallback), it must mirror the
same bytes into OpenEMR's ``documents`` table via the JWT-protected
custom-module ``UploadController.php``. This is what makes the document
visible in the chart Documents tab.

Contract:

1. On the ``"fhir"`` path with a configured JWT secret, the mirror call
   is made — exactly once — and the FHIR write result is returned
   unchanged.
2. If the mirror call fails (network, 5xx, malformed JSON), the primary
   FHIR write result is still returned and no exception escapes.
3. On the ``"copilot_custom"`` path, the mirror is skipped entirely (the
   custom endpoint already wrote to ``documents`` directly).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.fhir_writer import write_document  # noqa: E402

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]


_PDF_BYTES = b"%PDF-1.4\nMIRROR-FIXTURE\n%%EOF\n"


# ── Transport helpers ───────────────────────────────────────────────────────


def _make_handler(
    *,
    binary_raise: bool = False,
    rest_raise: bool = False,
    mirror_status: int = 200,
    mirror_body: dict[str, Any] | None = None,
    mirror_raise: bool = False,
    captured: list[dict[str, Any]] | None = None,
):
    mirror_body = mirror_body if mirror_body is not None else {"documentId": 99001}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/default/token"):
            return httpx.Response(
                200,
                json={"access_token": "fake-bearer", "expires_in": 600},
            )

        body_text = (
            request.content.decode("utf-8", errors="replace") if request.content else ""
        )
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
            return httpx.Response(
                201, json={"resourceType": "Binary", "id": "bin-mirror-1"}
            )
        if path.endswith("/fhir/DocumentReference"):
            return httpx.Response(
                201, json={"resourceType": "DocumentReference", "id": "doc-mirror-1"}
            )
        if "/api/patient/" in path and path.endswith("/document"):
            if rest_raise:
                raise httpx.ConnectError("simulated REST error", request=request)
            return httpx.Response(200, json={"documentId": 7777})
        if path.endswith("/oe-module-clinical-copilot/public/upload.php"):
            if mirror_raise:
                raise httpx.ConnectError("simulated mirror error", request=request)
            return httpx.Response(mirror_status, json=mirror_body)

        return httpx.Response(404, json={"error": f"unhandled {path}"})

    return _handler


class _PatchedAsyncClient(httpx.AsyncClient):
    _handler: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(type(self)._handler)
        super().__init__(*args, **kwargs)


def _patch_httpx(handler) -> Any:
    cls = type(
        "_BoundClient",
        (_PatchedAsyncClient,),
        {"_handler": staticmethod(handler)},
    )
    return patch("documents.fhir_writer.httpx.AsyncClient", cls)


@pytest.fixture(autouse=True)
def _stub_token() -> Any:
    with patch(
        "documents.fhir_writer.get_access_token",
        new=AsyncMock(return_value="fake-bearer"),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _stub_fhir_client() -> Any:
    with patch("documents.fhir_writer.fhir_client", new=AsyncMock()) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _set_jwt_secret(monkeypatch) -> None:
    """Most mirror tests need a configured JWT secret. Tests that need it
    unset can override via monkeypatch in the body.
    """
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_fhir_success_mirrors_to_legacy_documents() -> None:
    """Happy path: FHIR write succeeds → mirror call is made exactly once
    with patient_id, file part, and fhir_doc_ref_id in the form data.
    """
    captured: list[dict[str, Any]] = []
    handler = _make_handler(captured=captured)

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="pt-mirror-1",
            pdf_bytes=_PDF_BYTES,
            display="Lipid panel",
            doc_type_hint="lab_report",
        )

    assert result.path == "fhir"
    assert result.document_reference_id == "doc-mirror-1"

    mirror_calls = [
        c for c in captured
        if c["url"].endswith("/oe-module-clinical-copilot/public/upload.php")
    ]
    assert len(mirror_calls) == 1, (
        f"expected exactly one mirror call, got {len(mirror_calls)}: {mirror_calls}"
    )


async def test_mirror_failure_does_not_break_primary_write() -> None:
    """Mirror endpoint errors must NOT propagate. The FHIR write result
    is still returned; the primary path stays durable.
    """
    captured: list[dict[str, Any]] = []
    handler = _make_handler(mirror_raise=True, captured=captured)

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="pt-mirror-2",
            pdf_bytes=_PDF_BYTES,
        )

    # FHIR primary write still succeeded.
    assert result.path == "fhir"
    assert result.document_reference_id == "doc-mirror-1"
    assert result.binary_id == "bin-mirror-1"

    # We did attempt the mirror — once.
    mirror_calls = [
        c for c in captured
        if c["url"].endswith("/oe-module-clinical-copilot/public/upload.php")
    ]
    assert len(mirror_calls) == 1


async def test_mirror_5xx_does_not_break_primary_write() -> None:
    """Same as failure case but for an HTTP 5xx response (raise_for_status
    branch) rather than a connect error.
    """
    handler = _make_handler(mirror_status=500, mirror_body={"error": "boom"})

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="pt-mirror-3",
            pdf_bytes=_PDF_BYTES,
        )

    assert result.path == "fhir"
    assert result.document_reference_id == "doc-mirror-1"


async def test_copilot_custom_path_does_not_double_mirror() -> None:
    """When FHIR + REST fail and the custom upload endpoint absorbs the
    write directly (path == ``copilot_custom``), the mirror hook MUST be
    skipped — otherwise we'd write the same bytes twice.

    The single ``upload.php`` call we observe is the ``_write_via_custom_endpoint``
    fallback, not the mirror.
    """
    captured: list[dict[str, Any]] = []
    handler = _make_handler(
        binary_raise=True,
        rest_raise=True,
        mirror_status=200,
        mirror_body={"documentId": 12345},
        captured=captured,
    )

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="pt-mirror-4",
            pdf_bytes=_PDF_BYTES,
        )

    assert result.path == "copilot_custom"
    assert result.document_reference_id == "copilot:12345"

    # Exactly one upload.php call — the fallback itself, not a second mirror.
    custom_calls = [
        c for c in captured
        if c["url"].endswith("/oe-module-clinical-copilot/public/upload.php")
    ]
    assert len(custom_calls) == 1


async def test_rest_fallback_path_also_mirrors() -> None:
    """The REST fallback path still produces a FHIR-DocumentReference-shaped
    record (``rest:<id>``) but the chart Documents tab won't see it
    without the legacy mirror — so this path MUST mirror too.
    """
    captured: list[dict[str, Any]] = []
    handler = _make_handler(binary_raise=True, captured=captured)

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="pt-mirror-5",
            pdf_bytes=_PDF_BYTES,
        )

    assert result.path == "rest_fallback"
    mirror_calls = [
        c for c in captured
        if c["url"].endswith("/oe-module-clinical-copilot/public/upload.php")
    ]
    assert len(mirror_calls) == 1


async def test_mirror_skipped_when_jwt_secret_unset(monkeypatch) -> None:
    """When ``copilot_jwt_secret`` is empty/short, the custom endpoint
    would 401 anyway — skip the call entirely instead of wasting a round
    trip and a log line.
    """
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "",  # short → JWT minter returns None
    )
    captured: list[dict[str, Any]] = []
    handler = _make_handler(captured=captured)

    with _patch_httpx(handler):
        result = await write_document(
            patient_id="pt-mirror-6",
            pdf_bytes=_PDF_BYTES,
        )

    assert result.path == "fhir"
    mirror_calls = [
        c for c in captured
        if c["url"].endswith("/oe-module-clinical-copilot/public/upload.php")
    ]
    assert mirror_calls == []
