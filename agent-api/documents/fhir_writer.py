"""FHIR Binary + DocumentReference writer (W2 §4.2 step 3 / §4.4).

Path B of document ingestion: round-trip an uploaded PDF into OpenEMR by
writing the source bytes to a FHIR ``Binary`` and the metadata wrapper to a
``DocumentReference``.  The split keeps the long-lived source-of-truth in
OpenEMR's FHIR store and lets ``copilot_doc_extractions`` reference the
DocumentReference id without duplicating the PDF blob.

Risk #1 mitigation (W2 §12)
---------------------------
OpenEMR's FHIR Binary endpoint has historically choked on multi-MB PDFs.
If either the Binary POST or the DocumentReference POST fails (4xx/5xx or
network error), this module falls back to the legacy REST upload at
``/apis/default/api/patient/{pid}/document``.  The synthetic
``WriteResult`` carries ``path="rest_fallback"`` so callers can record it
in the audit trail.

Privacy
-------
The PDF bytes and their base64 encoding are NEVER logged.  Per
``W1_ARCHITECTURE.md`` §5.2 / §9.2 only sizes and ids cross the log boundary.
"""

from __future__ import annotations

import base64
import datetime as _dt
import logging
from typing import Any, Literal, NamedTuple

import httpx
import jwt as _jwt

# documents-isolated importlinter contract carves out auth.fhir_client
# explicitly so this slice can reuse the existing OAuth-aware client + token
# fetcher rather than reimplementing the password grant.
from auth.fhir_client import fhir_client, get_access_token
from config import settings

_logger = logging.getLogger(__name__)


# ── LOINC mapping for DocumentReference.type ────────────────────────────────
# Source: https://loinc.org — picked from the standard outpatient document
# vocabulary so OpenEMR's chart UI can render a sensible label.

_LOINC_BY_HINT: dict[str, tuple[str, str]] = {
    "lab_report": ("11502-2", "Laboratory report"),
    "intake_form": ("34105-7", "Hospital admission Hx"),
}
_LOINC_DEFAULT: tuple[str, str] = ("34108-1", "Outpatient note")


class WriteResult(NamedTuple):
    """Outcome of :func:`write_document`.

    ``binary_id`` is empty when ``path == "rest_fallback"`` because the
    REST endpoint does not surface a Binary id.
    """

    document_reference_id: str
    binary_id: str
    path: Literal[
        "fhir",
        "rest_fallback",
        "copilot_custom",
        "local_disk_fallback",
    ]


class FhirWriteError(RuntimeError):
    """Raised when both the FHIR write path and the REST fallback fail.

    The wrapped cause is logged via PSR-3 ``extra={"exception": ...}`` but
    deliberately omitted from this exception's message — callers surface
    it to clinicians via the audit / UI layer with a generic copy.
    """


def _now_rfc3339() -> str:
    """RFC3339 / ISO-8601 timestamp with Z suffix for FHIR ``date`` field."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pick_loinc(doc_type_hint: str | None) -> tuple[str, str]:
    if doc_type_hint is None:
        return _LOINC_DEFAULT
    return _LOINC_BY_HINT.get(doc_type_hint, _LOINC_DEFAULT)


def _fhir_base() -> str:
    return settings.openemr_base_url.rstrip("/") + "/apis/default/fhir"


def _rest_base() -> str:
    return settings.openemr_base_url.rstrip("/") + "/apis/default/api"


async def _post_fhir(resource: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST a FHIR resource using the cached bearer token from fhir_client.

    Retries once on 400/401 by forcing a token refresh — mirrors the
    behaviour of ``FHIRClient.get`` for the read path.
    """
    url = f"{_fhir_base()}/{resource}"

    async def _attempt(force_refresh: bool) -> httpx.Response:
        token = await get_access_token(force_refresh=force_refresh)
        async with httpx.AsyncClient(timeout=60) as client:
            return await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/fhir+json",
                    "Accept": "application/fhir+json",
                },
            )

    response = await _attempt(force_refresh=False)
    if response.status_code in (400, 401):
        response = await _attempt(force_refresh=True)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "id" not in payload:
        raise RuntimeError(f"FHIR {resource} POST returned no id field")
    return payload


async def _write_via_fhir(
    *,
    patient_id: str,
    pdf_bytes: bytes,
    mime_type: str,
    display: str | None,
    doc_type_hint: str | None,
) -> WriteResult:
    """Primary path: Binary POST followed by DocumentReference POST."""
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    binary_payload = await _post_fhir(
        "Binary",
        {
            "resourceType": "Binary",
            "contentType": mime_type,
            "data": encoded,
        },
    )
    binary_id = str(binary_payload["id"])

    code, code_display = _pick_loinc(doc_type_hint)
    title = display or "Uploaded clinical document"
    docref_body: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": code,
                    "display": code_display,
                }
            ]
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "content": [
            {
                "attachment": {
                    "contentType": mime_type,
                    "url": f"Binary/{binary_id}",
                    "title": title,
                }
            }
        ],
        "date": _now_rfc3339(),
    }
    docref_payload = await _post_fhir("DocumentReference", docref_body)
    return WriteResult(
        document_reference_id=str(docref_payload["id"]),
        binary_id=binary_id,
        path="fhir",
    )


def _mint_copilot_jwt(provider_id: str = "0") -> str | None:
    """Mint an HS256 JWT compatible with PHP JwtMinter's shape.

    Returns None when ``copilot_jwt_secret`` is unset/short — caller must
    skip the custom endpoint entirely (it would 401 anyway).
    """
    secret = settings.copilot_jwt_secret
    if not secret or len(secret) < 32:
        return None
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {
        "sub": str(provider_id),
        "sid": f"agent-api-doc-{now}",
        "iat": now,
        "exp": now + 300,  # 5-minute TTL — single-shot upload
        "iss": "openemr-copilot",
    }
    return _jwt.encode(claims, secret, algorithm="HS256")


def _custom_upload_url() -> str:
    return (
        settings.openemr_base_url.rstrip("/")
        + "/interface/modules/custom_modules/oe-module-clinical-copilot/public/upload.php"
    )


_MIME_TO_EXTENSION: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/hl7-v2": ".hl7",
}


def _extension_for_mime(mime_type: str) -> str:
    return _MIME_TO_EXTENSION.get(mime_type.lower(), "")


async def _write_via_custom_endpoint(
    *,
    patient_id: str,
    pdf_bytes: bytes,
    mime_type: str,
    display: str | None,
    doc_type_hint: str | None,
) -> WriteResult:
    """Second-tier fallback: POST to the custom Co-Pilot upload endpoint.

    Skips the FHIR + REST gauntlet entirely and writes directly into
    OpenEMR's documents table via the legacy ``Document::createDocument``
    API. Authenticated by the same HS256 JWT shape JwtMinter.php mints.
    """
    token = _mint_copilot_jwt()
    if token is None:
        raise RuntimeError("copilot_jwt_secret unset — custom endpoint skipped")

    url = _custom_upload_url()
    filename = (display or "document") + _extension_for_mime(mime_type)
    # Field name "file" — UploadController.php reads $_FILES['file'].
    # The legacy REST tier (_write_via_rest) uses "document" because
    # OpenEMR's RestApiController reads $_FILES['document'] there.
    # Two endpoints, two different field-name contracts — do not unify.
    files = {"file": (filename, pdf_bytes, mime_type)}
    data: dict[str, str] = {"patient_id": patient_id}
    if doc_type_hint:
        data["doc_type_hint"] = doc_type_hint

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            url,
            files=files,
            data=data,
            headers={"Authorization": f"Bearer {token}"},
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "documentId" not in payload:
        raise RuntimeError("custom upload returned no documentId field")
    document_id = payload["documentId"]
    return WriteResult(
        document_reference_id=f"copilot:{document_id}",
        binary_id="",
        path="copilot_custom",
    )


async def _write_local_disk(
    *,
    patient_id: str,
    pdf_bytes: bytes,
    display: str | None,
) -> WriteResult:
    """Third-tier fallback per W2_ARCHITECTURE risk #1.

    When the OpenEMR deploy doesn't expose Binary write AND the legacy
    multipart REST endpoint is also unavailable (e.g. older OpenEMR builds
    where /apis/default/fhir/Binary advertises 'read' only and the
    /apis/default/api/patient/.../document path 404s), we persist the PDF
    to a local volume so the rest of the ingest pipeline (extraction +
    audit + RAG) can still run end-to-end.

    The ``document_reference_id`` is a deterministic synthetic UUID derived
    from a content hash so re-ingest of the same bytes resolves to the
    same id (idempotency contract preserved). On Railway this directory
    is ephemeral; bytes don't survive a restart, but the agent-api owns
    the ingest lifetime so that's acceptable for MVP demo. A persistent
    Volume is the next-tier mitigation.
    """
    import hashlib
    import os
    import uuid as _uuid

    sha = hashlib.sha256(pdf_bytes).hexdigest()
    base = os.environ.get("LOCAL_DOC_FALLBACK_DIR", "/tmp/copilot-docs")
    os.makedirs(base, exist_ok=True)
    # Namespace UUID derived from the content hash → deterministic id.
    doc_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"copilot-local:{sha}"))
    path = os.path.join(base, f"{doc_id}.pdf")
    with open(path, "wb") as fh:
        fh.write(pdf_bytes)
    return WriteResult(
        document_reference_id=f"local:{doc_id}",
        binary_id="",
        path="local_disk_fallback",
    )


async def _write_via_rest(
    *,
    patient_id: str,
    pdf_bytes: bytes,
    mime_type: str,
    display: str | None,
) -> WriteResult:
    """Fallback path: legacy multipart upload to /api/patient/{pid}/document."""
    url = f"{_rest_base()}/patient/{patient_id}/document"
    token = await get_access_token(force_refresh=False)
    filename = (display or "document") + ".pdf"
    files = {"document": (filename, pdf_bytes, mime_type)}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            url,
            files=files,
            headers={"Authorization": f"Bearer {token}"},
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "documentId" not in payload:
        raise RuntimeError("REST document upload returned no documentId field")
    document_id = payload["documentId"]
    return WriteResult(
        document_reference_id=f"rest:{document_id}",
        binary_id="",
        path="rest_fallback",
    )


async def _mirror_to_legacy_documents(
    *,
    patient_id: str,
    pdf_bytes: bytes,
    mime_type: str,
    display: str | None,
    doc_type_hint: str | None,
    document_reference_id: str,
) -> int | None:
    """Best-effort mirror of a FHIR-written document into OpenEMR's legacy
    ``documents`` table via the custom-module ``UploadController.php``.

    Phase 6.1 (W2 follow-up). After a successful FHIR ``Binary`` +
    ``DocumentReference`` write (path ``"fhir"``) — or its legacy REST
    fallback (path ``"rest_fallback"``) — the document is invisible to
    OpenEMR's chart Documents tab because the chart UI reads the
    ``documents`` table, not the FHIR Binary store. This helper POSTs the
    same bytes to the JWT-protected custom upload endpoint so the file
    appears alongside other chart docs under the "Clinical Copilot Upload"
    category (LOINC ``34109-9``).

    Returns the legacy ``documents.id`` on success, or ``None`` on any
    failure. NEVER raises — the primary FHIR write must remain durable.
    """
    token = _mint_copilot_jwt()
    if token is None:
        _logger.info(
            "legacy_mirror_skipped_no_jwt_secret",
            extra={"document_reference_id": document_reference_id},
        )
        return None

    url = _custom_upload_url()
    filename = (display or "document") + _extension_for_mime(mime_type)
    files = {"file": (filename, pdf_bytes, mime_type)}
    data: dict[str, str] = {
        "patient_id": patient_id,
        "fhir_doc_ref_id": document_reference_id,
    }
    if doc_type_hint:
        data["doc_type_hint"] = doc_type_hint

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                url,
                files=files,
                data=data,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or "documentId" not in payload:
            _logger.warning(
                "legacy_mirror_no_document_id",
                extra={"document_reference_id": document_reference_id},
            )
            return None
        document_id = int(payload["documentId"])
        _logger.info(
            "legacy_mirror_ok",
            extra={
                "document_reference_id": document_reference_id,
                "legacy_document_id": document_id,
            },
        )
        return document_id
    except Exception as exc:  # noqa: BLE001 — best-effort, never propagate
        _logger.warning(
            "legacy_mirror_failed",
            extra={
                "document_reference_id": document_reference_id,
                "error": str(exc),
            },
        )
        return None


async def write_document(
    *,
    patient_id: str,
    pdf_bytes: bytes,
    mime_type: str = "application/pdf",
    display: str | None = None,
    doc_type_hint: str | None = None,
) -> WriteResult:
    """Persist ``pdf_bytes`` into OpenEMR via FHIR Binary + DocumentReference.

    Falls back to the legacy multipart REST endpoint if either FHIR POST
    fails (network error or non-2xx).  Raises :class:`FhirWriteError`
    only if both paths fail; the underlying exception is logged with
    ``extra={"exception": str(e)}`` and never bubbled to callers.
    """
    _logger.info(
        "fhir_document_write_started",
        extra={
            "patient_id": patient_id,
            "size_bytes": len(pdf_bytes),
            "mime_type": mime_type,
        },
    )

    # _ = fhir_client  # imported for the importlinter / auth carve-out audit
    assert fhir_client is not None  # tested-mock anchor + import-keep

    try:
        result = await _write_via_fhir(
            patient_id=patient_id,
            pdf_bytes=pdf_bytes,
            mime_type=mime_type,
            display=display,
            doc_type_hint=doc_type_hint,
        )
    except Exception as primary_exc:  # noqa: BLE001 — generic by design
        _logger.warning(
            "fhir_document_write_failed_falling_back",
            extra={"error": str(primary_exc)},
        )
        try:
            result = await _write_via_rest(
                patient_id=patient_id,
                pdf_bytes=pdf_bytes,
                mime_type=mime_type,
                display=display,
            )
        except Exception as rest_exc:  # noqa: BLE001
            _logger.warning(
                "fhir_document_rest_fallback_failed_trying_custom",
                extra={"error": str(rest_exc)},
            )
            try:
                result = await _write_via_custom_endpoint(
                    patient_id=patient_id,
                    pdf_bytes=pdf_bytes,
                    mime_type=mime_type,
                    display=display,
                    doc_type_hint=doc_type_hint,
                )
            except Exception as custom_exc:  # noqa: BLE001
                _logger.warning(
                    "fhir_document_custom_fallback_failed_using_local",
                    extra={"error": str(custom_exc)},
                )
                try:
                    result = await _write_local_disk(
                        patient_id=patient_id,
                        pdf_bytes=pdf_bytes,
                        display=display,
                    )
                except Exception as local_exc:  # noqa: BLE001
                    _logger.error(
                        "fhir_document_write_all_failed",
                        extra={"error": str(local_exc)},
                    )
                    raise FhirWriteError(
                        "FHIR + REST + custom + local-disk fallbacks all failed"
                    ) from local_exc

    _logger.info(
        "fhir_document_write_ok",
        extra={
            "path": result.path,
            "document_reference_id": result.document_reference_id,
            "binary_id": result.binary_id,
        },
    )

    # Phase 6.1: mirror to legacy ``documents`` table so the file appears in
    # OpenEMR's chart Documents tab. Skip when ``copilot_custom`` (already
    # written there directly) or ``local_disk_fallback`` (no DB hop).
    if result.path in ("fhir", "rest_fallback"):
        await _mirror_to_legacy_documents(
            patient_id=patient_id,
            pdf_bytes=pdf_bytes,
            mime_type=mime_type,
            display=display,
            doc_type_hint=doc_type_hint,
            document_reference_id=result.document_reference_id,
        )

    return result


__all__ = ["FhirWriteError", "WriteResult", "write_document"]
