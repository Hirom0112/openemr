"""Tests for POST /document/ingest (W2 Slices 1.6 + 1.7).

The endpoint composes fhir_writer, documents.store, and the lab extractor.
Each is monkeypatched with an AsyncMock so the test never makes a real
network or DB call. JWT is bypassed by clearing COPILOT_JWT_SECRET before
``main`` is imported (the existing middleware no-ops with an empty secret —
see auth/jwt_middleware.py).
"""

from __future__ import annotations

import datetime as _dt
import io
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Bypass JWT before importing main: the middleware short-circuits on empty
# secret and lets every request through with principal=None — exactly what
# we want for these tests.
os.environ["COPILOT_JWT_SECRET"] = ""
# audit_db_url empty so audit_writer.emit() is a hard no-op for any path
# that doesn't explicitly capture it.
os.environ.setdefault("AUDIT_DB_URL", "")

import pymupdf  # noqa: E402

import main as main_module  # noqa: E402
from documents.fhir_writer import FhirWriteError, WriteResult  # noqa: E402
from documents.store import ClaimResult  # noqa: E402
from extractors.lab import ExtractionFailed  # noqa: E402
from extractors.schemas import (  # noqa: E402
    Citation,
    KeyFact,
    LabReport,
    LabValue,
    UnknownDocument,
)

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_pdf_bytes(*, pages: int = 1) -> bytes:
    """Build a minimal in-memory PDF with the requested number of pages.

    PyMuPDF can synthesise blank pages — keeps the fixtures self-contained
    so the page-count guard test can build a 51-page payload without
    shipping a 51-page binary in the repo.
    """
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


def _make_lab_report(
    *,
    document_reference_id: str = "doc-test-1",
    classifier_confidence: float = 0.95,
    ocr_confidence_range: tuple[float, float] = (0.85, 0.99),
    value_text: str = "4.2 mmol/L",
) -> LabReport:
    cit = Citation(
        source_type="document",
        source_id=document_reference_id,
        page_or_section="p1",
        field_or_chunk_id="p1-b001",
        quote_or_value=value_text,
    )
    val = LabValue(
        test_name="Lactate",
        normalized_test_name="lactate",
        value=value_text,
        unit="mmol/L",
        normalized_unit="mmol/L",
        reference_range="0.5-2.2",
        collection_date=None,
        abnormal_flag="high",
        citations=[cit],
    )
    return LabReport(
        kind="lab_report",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id=document_reference_id,
        collection_facility="Memorial OSH",
        values=[val],
        classifier_confidence=classifier_confidence,
        ocr_confidence_range=ocr_confidence_range,
        extracted_at=_dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )


def _make_unknown_doc(
    *,
    document_reference_id: str = "doc-test-1",
    ocr_confidence_range: tuple[float, float] = (0.85, 0.99),
) -> UnknownDocument:
    cit = Citation(
        source_type="document",
        source_id=document_reference_id,
        page_or_section="p1",
        field_or_chunk_id="p1-b001",
        quote_or_value="some snippet",
    )
    return UnknownDocument(
        kind="unknown",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id=document_reference_id,
        document_kind_guess="discharge_summary",
        summary="Unknown document.",
        key_facts=[KeyFact(text="Some fact.", citations=[cit])],
        classifier_confidence=0.55,
        ocr_confidence_range=ocr_confidence_range,
        extracted_at=_dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_path: str = "fhir",
    document_reference_id: str = "doc-test-1",
    claim: ClaimResult | None = None,
    extraction: Any | None = None,
    extract_raises: Exception | None = None,
    write_raises: Exception | None = None,
) -> dict[str, AsyncMock]:
    """Patch fhir_writer, store, and lab.extract with AsyncMocks.

    Returns the mock handles so tests can assert call counts / arguments.
    """
    from documents import fhir_writer as _fhir_writer
    from documents import store as _store
    from extractors import lab as _lab

    if claim is None:
        claim = ClaimResult(extraction_id=12345, owns_claim=True, cached_payload=None)
    if extraction is None:
        extraction = _make_lab_report(document_reference_id=document_reference_id)

    write_mock = AsyncMock()
    if write_raises is not None:
        write_mock.side_effect = write_raises
    else:
        write_mock.return_value = WriteResult(
            document_reference_id=document_reference_id,
            binary_id="bin-1",
            path=write_path,  # type: ignore[arg-type]
        )

    claim_mock = AsyncMock(return_value=claim)
    complete_mock = AsyncMock(return_value=None)
    fail_mock = AsyncMock(return_value=None)

    extract_mock = AsyncMock()
    if extract_raises is not None:
        extract_mock.side_effect = extract_raises
    else:
        extract_mock.return_value = extraction

    audit_emit_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(_fhir_writer, "write_document", write_mock)
    monkeypatch.setattr(_store, "claim_or_get", claim_mock)
    monkeypatch.setattr(_store, "complete", complete_mock)
    monkeypatch.setattr(_store, "fail", fail_mock)
    monkeypatch.setattr(_lab, "extract", extract_mock)
    monkeypatch.setattr(main_module.audit_writer, "emit", audit_emit_mock)

    return {
        "write_document": write_mock,
        "claim_or_get": claim_mock,
        "complete": complete_mock,
        "fail": fail_mock,
        "extract": extract_mock,
        "audit_emit": audit_emit_mock,
    }


async def _post_ingest(
    pdf_bytes: bytes,
    *,
    patient_id: str = "pt-1",
    doc_type_hint: str | None = "lab_report",
) -> httpx.Response:
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("upload.pdf", pdf_bytes, "application/pdf")}
        data: dict[str, str] = {"patient_id": patient_id}
        if doc_type_hint is not None:
            data["doc_type_hint"] = doc_type_hint
        return await client.post("/document/ingest", files=files, data=data)


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_ingest_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_path = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"
    pdf_bytes = pdf_path.read_bytes()

    mocks = _patch_pipeline(monkeypatch)

    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["document_reference_id"] == "doc-test-1"
    assert body["extraction_id"] == 12345
    assert body["extraction"]["kind"] == "lab_report"
    assert body["metadata"]["cached"] is False
    assert body["metadata"]["fhir_write_path"] == "fhir"
    assert body["metadata"]["size_bytes"] == len(pdf_bytes)
    assert body["metadata"]["page_count"] >= 1
    assert body["soft_warns"] == []
    # Citations were flattened from values[*].citations.
    assert len(body["citations"]) == 1
    assert body["citations"][0]["source_id"] == "doc-test-1"

    # Pipeline was driven once.
    assert mocks["write_document"].await_count == 1
    assert mocks["claim_or_get"].await_count == 1
    assert mocks["extract"].await_count == 1
    assert mocks["complete"].await_count == 1
    assert mocks["fail"].await_count == 0


async def test_ingest_oversized_returns_413(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the limit constant down rather than crafting a 25 MB payload.
    monkeypatch.setattr(main_module, "_DOC_INGEST_MAX_BYTES", 100)
    monkeypatch.setattr(main_module, "_DOC_INGEST_HARD_READ_CAP", 100)

    pdf_bytes = _make_pdf_bytes(pages=1)
    assert len(pdf_bytes) > 100  # sanity — fixture is bigger than the patched cap

    _patch_pipeline(monkeypatch)
    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 413, resp.text
    detail = resp.json().get("detail", "")
    assert "Documents tab" in detail


async def test_ingest_too_many_pages_returns_413(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_bytes = _make_pdf_bytes(pages=51)
    _patch_pipeline(monkeypatch)

    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 413, resp.text
    assert "Documents tab" in resp.json().get("detail", "")


async def test_ingest_low_ocr_confidence_emits_softwarn(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = _make_lab_report(ocr_confidence_range=(0.55, 0.95))
    _patch_pipeline(monkeypatch, extraction=extraction)

    pdf_bytes = _make_pdf_bytes(pages=1)
    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text
    codes = [w["code"] for w in resp.json()["soft_warns"]]
    assert "ocr_confidence_low" in codes


async def test_ingest_unknown_document_emits_softwarn(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = _make_unknown_doc()
    _patch_pipeline(monkeypatch, extraction=extraction)

    pdf_bytes = _make_pdf_bytes(pages=1)
    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["extraction"]["kind"] == "unknown"
    codes = [w["code"] for w in body["soft_warns"]]
    assert "unknown_document_class" in codes


async def test_ingest_low_classifier_confidence_emits_softwarn(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = _make_lab_report(classifier_confidence=0.55)
    _patch_pipeline(monkeypatch, extraction=extraction)

    pdf_bytes = _make_pdf_bytes(pages=1)
    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text
    codes = [w["code"] for w in resp.json()["soft_warns"]]
    assert "classifier_low_confidence" in codes


async def test_ingest_idempotent_returns_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = _make_lab_report()
    cached_payload = extraction.model_dump(mode="json")

    # First call: fresh claim, extract runs.
    fresh_claim = ClaimResult(extraction_id=42, owns_claim=True, cached_payload=None)
    # Second call: cache hit; extract MUST NOT run.
    cached_claim = ClaimResult(extraction_id=42, owns_claim=False, cached_payload=cached_payload)

    from documents import fhir_writer as _fhir_writer
    from documents import store as _store
    from extractors import lab as _lab

    write_mock = AsyncMock(return_value=WriteResult(
        document_reference_id="doc-test-1", binary_id="bin-1", path="fhir",
    ))
    claim_mock = AsyncMock(side_effect=[fresh_claim, cached_claim])
    extract_mock = AsyncMock(return_value=extraction)
    complete_mock = AsyncMock(return_value=None)
    fail_mock = AsyncMock(return_value=None)
    audit_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(_fhir_writer, "write_document", write_mock)
    monkeypatch.setattr(_store, "claim_or_get", claim_mock)
    monkeypatch.setattr(_store, "complete", complete_mock)
    monkeypatch.setattr(_store, "fail", fail_mock)
    monkeypatch.setattr(_lab, "extract", extract_mock)
    monkeypatch.setattr(main_module.audit_writer, "emit", audit_mock)

    pdf_bytes = _make_pdf_bytes(pages=1)
    resp1 = await _post_ingest(pdf_bytes)
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["metadata"]["cached"] is False

    resp2 = await _post_ingest(pdf_bytes)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["metadata"]["cached"] is True

    # Extract called exactly once across both ingests.
    assert extract_mock.await_count == 1
    # Citations still populated on the cached path.
    assert len(resp2.json()["citations"]) == 1


async def test_ingest_extraction_failure_records_fail_and_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = ClaimResult(extraction_id=777, owns_claim=True, cached_payload=None)
    mocks = _patch_pipeline(
        monkeypatch,
        claim=claim,
        extract_raises=ExtractionFailed("vision call failed"),
    )

    pdf_bytes = _make_pdf_bytes(pages=1)
    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 500, resp.text
    # Generic message — no internal details leaked.
    assert resp.json().get("detail") == "Document extraction failed"

    # store.fail was called with the right extraction_id.
    assert mocks["fail"].await_count == 1
    call = mocks["fail"].await_args
    assert call.kwargs.get("extraction_id") == 777


async def test_ingest_audit_events_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = _make_lab_report(value_text="4.2 mmol/L")
    mocks = _patch_pipeline(monkeypatch, extraction=extraction)

    pdf_bytes = _make_pdf_bytes(pages=1)
    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text

    emit_calls = mocks["audit_emit"].await_args_list
    assert len(emit_calls) == 2

    events = [call.args[0] for call in emit_calls]
    event_types = [ev.event_type for ev in events]
    assert event_types == ["document_ingested", "document_extracted"]

    # No clinical values appear in detail_json of either event.
    forbidden = "4.2 mmol/L"
    for ev in events:
        serialised = repr(ev.detail_json)
        assert forbidden not in serialised, f"clinical value leaked: {serialised}"

    # ingested event carries path/size/pages.
    ingested = events[0]
    assert ingested.detail_json.get("path") == "fhir"
    assert ingested.detail_json.get("size_bytes") == len(pdf_bytes)
    assert ingested.detail_json.get("page_count") == 1

    # extracted event carries kind + counts, never values.
    extracted = events[1]
    assert extracted.detail_json.get("kind") == "lab_report"
    assert extracted.detail_json.get("n_fields") == 1
    assert "values" not in extracted.detail_json


async def test_no_phi_in_logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    extraction = _make_lab_report(value_text="4.2 mmol/L")
    _patch_pipeline(monkeypatch, extraction=extraction)

    pdf_bytes = _make_pdf_bytes(pages=1)
    with caplog.at_level(logging.DEBUG):
        resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text

    forbidden = "4.2 mmol/L"
    for record in caplog.records:
        # Walk the message AND every extra attribute set on the record.
        msg = record.getMessage()
        assert forbidden not in msg, f"PHI leaked into log msg: {msg!r}"
        for key, value in record.__dict__.items():
            if isinstance(value, str):
                assert forbidden not in value, (
                    f"PHI leaked into log record.{key}={value!r}"
                )
