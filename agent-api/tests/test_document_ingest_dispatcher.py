"""Tests for the Phase 9 Slice 9.10 MIME dispatcher in /document/ingest.

The dispatcher detects the inbound format via magic bytes and routes to
the per-format parser before the legacy PDF page-guard. PDF/PNG keep
flowing through the existing pipeline.

These tests exercise:
  * pure-function MIME detection across all 5 supported formats,
  * end-to-end routing for HL7 (ORU + ADT), XLSX, DOCX, TIFF via mocks,
  * ADT-A08 → ``demographics.resolver`` quarantine wiring,
  * preservation of the legacy PDF/PNG path.
"""

from __future__ import annotations

import datetime as _dt
import io
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as main_module  # noqa: E402
from documents.fhir_writer import WriteResult  # noqa: E402
from documents.store import ClaimResult  # noqa: E402

pytestmark = [pytest.mark.hard_failure]


# Async-routing tests below also use asyncio; the pure detection tests do
# not. Per-test ``@pytest.mark.asyncio`` is added to the async cases
# directly so the sync ones do not trigger pytest-asyncio collection
# warnings.


# ── MIME detection (pure-function) ───────────────────────────────────────────


def _build_zip(names: list[str]) -> bytes:
    """Tiny in-memory zip with the given file names + 1-byte payloads."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")
    return buf.getvalue()


def test_detect_format_hl7() -> None:
    raw = b"MSH|^~\\&|HOSPITAL|HOSP|LAB|LABFAC|202605070101||ADT^A08|MSG-1|P|2.5\r"
    assert main_module._detect_ingest_format(raw) == "hl7"


def test_detect_format_pdf() -> None:
    assert main_module._detect_ingest_format(b"%PDF-1.4\n%\xe2\xe3") == "pdf"


def test_detect_format_png() -> None:
    assert main_module._detect_ingest_format(b"\x89PNG\r\n\x1a\nrest") == "png"


def test_detect_format_tiff_le_and_be() -> None:
    assert main_module._detect_ingest_format(b"II*\x00rest") == "tiff"
    assert main_module._detect_ingest_format(b"MM\x00*rest") == "tiff"


def test_detect_format_docx_via_zip_directory() -> None:
    raw = _build_zip(
        ["word/document.xml", "[Content_Types].xml", "word/_rels/document.xml.rels"]
    )
    assert main_module._detect_ingest_format(raw) == "docx"


def test_detect_format_xlsx_via_zip_directory() -> None:
    raw = _build_zip(["xl/workbook.xml", "[Content_Types].xml", "xl/sharedStrings.xml"])
    assert main_module._detect_ingest_format(raw) == "xlsx"


def test_detect_format_unknown_for_random_zip() -> None:
    raw = _build_zip(["foo/bar.txt"])
    assert main_module._detect_ingest_format(raw) == "unknown"


def test_detect_format_unknown_for_empty_or_garbage() -> None:
    assert main_module._detect_ingest_format(b"") == "unknown"
    assert main_module._detect_ingest_format(b"\x00\x01\x02\x03not-a-format") == "unknown"


# ── End-to-end routing (mocked downstream parsers) ──────────────────────────


def _patch_fhir_and_claim(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Patch fhir_writer.write_document and store.claim_or_get with AsyncMocks."""
    from documents import fhir_writer as _fhir_writer
    from documents import store as _store

    write_mock = AsyncMock(
        return_value=WriteResult(
            document_reference_id="doc-test-1", binary_id="bin-1", path="fhir"
        )
    )
    claim_mock = AsyncMock(
        return_value=ClaimResult(extraction_id=42, owns_claim=True, cached_payload=None)
    )
    complete_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(_fhir_writer, "write_document", write_mock)
    monkeypatch.setattr(_store, "claim_or_get", claim_mock)
    monkeypatch.setattr(_store, "complete", complete_mock)
    monkeypatch.setattr(_store, "compute_sha256", lambda b: "sha256-fake")
    return {"write": write_mock, "claim": claim_mock, "complete": complete_mock}


async def _post_bytes(
    raw: bytes,
    *,
    filename: str = "upload.bin",
    content_type: str = "application/octet-stream",
    patient_id: str = "pt-1",
) -> httpx.Response:
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": (filename, raw, content_type)}
        data = {"patient_id": patient_id}
        return await client.post("/document/ingest", files=files, data=data)


@pytest.mark.asyncio
async def test_dispatch_hl7_oru_routes_to_parse_hl7(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HL7 ORU bytes should call parsers.hl7.dispatch.parse_hl7 and stage values."""
    _patch_fhir_and_claim(monkeypatch)

    # Stub parse_hl7 to return a LabReport (ORU branch). Build a minimal
    # LabReport via the real schema so model_dump succeeds.
    from extractors.schemas import Citation, LabReport, LabValue
    cit = Citation(
        source_type="document",
        source_id="doc-test-1",
        page_or_section="MSH",
        field_or_chunk_id="OBX-3",
        quote_or_value="Lactate",
    )
    lab = LabReport(
        kind="lab_report",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id="doc-test-1",
        collection_facility="Memorial",
        values=[
            LabValue(
                test_name="Lactate",
                normalized_test_name="lactate",
                value="3.1",
                unit="mmol/L",
                normalized_unit="mmol/L",
                reference_range="0.5-2.2",
                collection_date=None,
                abnormal_flag="high",
                citations=[cit],
            )
        ],
        classifier_confidence=1.0,
        ocr_confidence_range=(1.0, 1.0),
        extracted_at=_dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )

    parse_hl7_mock = MagicMock(return_value=lab)
    from parsers.hl7 import dispatch as _hl7_dispatch
    monkeypatch.setattr(_hl7_dispatch, "parse_hl7", parse_hl7_mock)

    stage_mock = AsyncMock(return_value=None)
    from observations import writer as _obs_writer
    monkeypatch.setattr(_obs_writer, "stage_observation", stage_mock)

    raw = (
        b"MSH|^~\\&|HOSPITAL|HOSP|LAB|LABFAC|202605070101||ORU^R01|MSG-1|P|2.5\r"
        b"PID|||MRN-1||DOE^JANE\r"
    )
    resp = await _post_bytes(raw, filename="lab.hl7", content_type="application/hl7-v2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metadata"]["format"] == "hl7"
    assert body["metadata"]["parse_summary"]["kind"] == "lab_report"
    assert body["metadata"]["parse_summary"]["lab_values_staged"] == 1
    assert parse_hl7_mock.call_count == 1
    assert stage_mock.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_hl7_adt_calls_resolver_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HL7 ADT^A08 → DemographicUpdateEvent must invoke demographics.resolver."""
    _patch_fhir_and_claim(monkeypatch)

    from extractors.schemas import Citation
    from parsers.hl7.types import DemographicField, DemographicUpdateEvent
    cit = Citation(
        source_type="document",
        source_id="doc-test-1",
        page_or_section="PID",
        field_or_chunk_id="PID-3.1",
        quote_or_value="MRN-1",
    )
    event = DemographicUpdateEvent(
        patient_id="pt-1",
        document_reference_id="doc-test-1",
        event_type="ADT^A08",
        control_id="MSG-1",
        mrn=DemographicField(value="MRN-1", citations=[cit]),
        extracted_at=_dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )

    from parsers.hl7 import dispatch as _hl7_dispatch
    monkeypatch.setattr(_hl7_dispatch, "parse_hl7", MagicMock(return_value=event))

    # Resolver returns Quarantine — exercises the quarantine path. We
    # stub the resolver and the quarantine_document writer; the test
    # asserts the resolver was called with format_hint="hl7" and the
    # response surfaces a quarantine_id.
    from demographics import resolver as _resolver
    from demographics import quarantine as _quar

    resolve_mock = AsyncMock(
        return_value=_resolver.Quarantine(
            reason_code="MRN_NOT_FOUND",
            candidate_hints=[{"mrn": "MRN-1"}],
        )
    )
    quar_mock = AsyncMock(
        return_value={
            "quarantine_id": 99,
            "expires_at": "2026-05-08T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(_resolver, "resolve", resolve_mock)
    monkeypatch.setattr(_quar, "quarantine_document", quar_mock)
    # audit_writer.get_pool is awaited inside the dispatcher; stub it.
    pool_mock = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(main_module.audit_writer, "get_pool", pool_mock)

    raw = (
        b"MSH|^~\\&|HOSPITAL|HOSP|ADT|ADTFAC|202605070101||ADT^A08|MSG-1|P|2.5\r"
        b"PID|||MRN-1||DOE^JANE\r"
    )
    resp = await _post_bytes(raw, filename="adt.hl7")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "quarantined"
    assert body["reason_code"] == "MRN_NOT_FOUND"
    assert resolve_mock.await_count == 1
    # Resolver must have been called with format_hint="hl7"
    kwargs = resolve_mock.await_args.kwargs
    assert kwargs.get("format_hint") == "hl7"
    assert quar_mock.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_xlsx_routes_to_parse_and_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fhir_and_claim(monkeypatch)

    from parsers.xlsx import types as _xlsx_types

    parsed_wb = _xlsx_types.ParsedWorkbook(
        intake_form=None,
        lab_reports=[],
        pending_tasks=[],
    )
    stage_mock = AsyncMock(return_value=parsed_wb)
    # Patch via the module main.py imports from.
    import parsers.xlsx as _xlsx_pkg
    monkeypatch.setattr(_xlsx_pkg, "parse_and_stage", stage_mock)

    raw = _build_zip(["xl/workbook.xml", "[Content_Types].xml"])
    resp = await _post_bytes(raw, filename="data.xlsx")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metadata"]["format"] == "xlsx"
    assert body["metadata"]["parse_summary"]["kind"] == "workbook"
    assert stage_mock.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_docx_routes_to_extract_intake_from_docx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fhir_and_claim(monkeypatch)

    from extractors import intake as _intake
    from extractors.schemas import Citation, KeyFact, UnknownDocument

    cit = Citation(
        source_type="document",
        source_id="doc-test-1",
        page_or_section="prose",
        field_or_chunk_id="P-001",
        quote_or_value="(empty)",
    )
    extraction = UnknownDocument(
        kind="unknown",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id="doc-test-1",
        document_kind_guess="docx",
        summary="DOCX summary.",
        key_facts=[KeyFact(text="fact", citations=[cit])],
        classifier_confidence=0.9,
        ocr_confidence_range=(1.0, 1.0),
        extracted_at=_dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )
    extract_mock = AsyncMock(return_value=extraction)
    monkeypatch.setattr(_intake, "extract_intake_from_docx", extract_mock)

    raw = _build_zip(["word/document.xml", "[Content_Types].xml"])
    resp = await _post_bytes(raw, filename="referral.docx")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metadata"]["format"] == "docx"
    assert body["metadata"]["parse_summary"]["kind"] == "unknown"
    assert extract_mock.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_tiff_routes_through_extractor_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fhir_and_claim(monkeypatch)

    # Stub the TIFF loader to return an empty layout — drives the lab branch.
    from documents import tiff_loader as _tiff_loader
    monkeypatch.setattr(_tiff_loader, "extract_tiff_layout", lambda b: [])

    from extractors import lab as _lab
    from extractors.schemas import Citation, LabReport
    extraction = LabReport(
        kind="lab_report",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id="doc-test-1",
        collection_facility="Memorial",
        values=[],
        classifier_confidence=0.9,
        ocr_confidence_range=(0.95, 1.0),
        extracted_at=_dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )
    _ = Citation  # silence unused
    extract_mock = AsyncMock(return_value=extraction)
    monkeypatch.setattr(_lab, "extract", extract_mock)

    raw = b"II*\x00" + b"\x00" * 64
    resp = await _post_bytes(raw, filename="fax.tiff")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metadata"]["format"] == "tiff"
    assert body["metadata"]["parse_summary"]["kind"] == "lab_report"
    assert extract_mock.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_pdf_falls_through_to_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF must NOT touch the new dispatcher branch — the legacy lab pipeline runs."""
    from documents import fhir_writer as _fhir_writer
    from documents import store as _store
    from extractors import lab as _lab
    from extractors.schemas import Citation, LabReport, LabValue

    cit = Citation(
        source_type="document",
        source_id="doc-test-1",
        page_or_section="p1",
        field_or_chunk_id="p1-b001",
        quote_or_value="4.2 mmol/L",
    )
    extraction = LabReport(
        kind="lab_report",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id="doc-test-1",
        collection_facility="Memorial",
        values=[
            LabValue(
                test_name="Lactate",
                normalized_test_name="lactate",
                value="4.2 mmol/L",
                unit="mmol/L",
                normalized_unit="mmol/L",
                reference_range="0.5-2.2",
                collection_date=None,
                abnormal_flag="high",
                citations=[cit],
            )
        ],
        classifier_confidence=0.95,
        ocr_confidence_range=(0.85, 0.99),
        extracted_at=_dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )

    write_mock = AsyncMock(
        return_value=WriteResult(
            document_reference_id="doc-test-1", binary_id="bin-1", path="fhir"
        )
    )
    claim_mock = AsyncMock(
        return_value=ClaimResult(extraction_id=42, owns_claim=True, cached_payload=None)
    )
    extract_mock = AsyncMock(return_value=extraction)

    monkeypatch.setattr(_fhir_writer, "write_document", write_mock)
    monkeypatch.setattr(_store, "claim_or_get", claim_mock)
    monkeypatch.setattr(_store, "complete", AsyncMock(return_value=None))
    monkeypatch.setattr(_store, "fail", AsyncMock(return_value=None))
    monkeypatch.setattr(_store, "record_observation_ids", AsyncMock(return_value=None))
    monkeypatch.setattr(_lab, "extract", extract_mock)
    monkeypatch.setattr(
        main_module.audit_writer,
        "emit",
        AsyncMock(return_value=None),
    )

    # Build a real minimal PDF via PyMuPDF.
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    pdf_bytes = doc.tobytes()
    doc.close()

    resp = await _post_bytes(
        pdf_bytes, filename="upload.pdf", content_type="application/pdf"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Legacy PDF path returns the full extraction body — and metadata
    # carries no ``format`` key (that's a Slice 9.10 multimodal-only field).
    assert body["extraction"]["kind"] == "lab_report"
    assert "format" not in body["metadata"] or body["metadata"].get("format") is None
    assert extract_mock.await_count == 1
