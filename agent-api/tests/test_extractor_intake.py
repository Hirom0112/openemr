"""Tests for the production intake-form extractor (Slice 4.5).

Covers:

- Pydantic round-trip on a minimal valid IntakeForm.
- UnknownDocument fallback when classifier verdict is non-intake.
- Live happy-path E2E against the real Anthropic API (skipped without key).
- Validation-error path → ExtractionFailed (no PHI in message).
- /document/ingest classifier-first dispatch routes intake → intake extractor
  and lab → lab extractor.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors import (  # noqa: E402
    ExtractionFailed,
    IntakeForm,
    extract_intake,
)
from extractors.schemas import (  # noqa: E402
    AllergyItem,
    Citation,
    CodeStatus,
    MedicationItem,
    UnknownDocument,
)

pytestmark = pytest.mark.hard_failure

LAB_FIXTURE = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"
INTAKE_FIXTURE = ROOT / "tests" / "fixtures" / "intake_admission.pdf"


# --------------------------------------------------------------------------- #
# Schema round-trip
# --------------------------------------------------------------------------- #


def test_intake_schema_round_trip() -> None:
    cit = {
        "source_type": "document",
        "source_id": "doc-x",
        "page_or_section": "1",
        "field_or_chunk_id": "p1-b001",
        "quote_or_value": "Penicillin - rash",
    }
    payload = {
        "kind": "intake_form",
        "schema_version": "1.0",
        "patient_id": "pt-x",
        "document_reference_id": "doc-x",
        "demographics": None,
        "chief_concern": None,
        "current_medications": [
            {
                "name": "lisinopril",
                "dose": "10 mg",
                "citations": [cit],
            }
        ],
        "allergies": [
            {
                "substance": "penicillin",
                "reaction": "rash",
                "citations": [cit],
            }
        ],
        "family_history": [],
        "code_status": {
            "value": "full_code",
            "citations": [cit],
        },
        "classifier_confidence": 0.9,
        "ocr_confidence_range": [0.85, 0.99],
        "extracted_at": "2026-05-04T00:00:00+00:00",
    }
    # Round-trip via JSON so strict mode accepts ISO datetime and list→tuple.
    form = IntakeForm.model_validate_json(json.dumps(payload))
    assert form.kind == "intake_form"
    assert form.code_status is not None and form.code_status.value == "full_code"

    dumped = form.model_dump_json()
    re_validated = IntakeForm.model_validate_json(dumped)
    assert re_validated.allergies[0].substance == "penicillin"
    assert re_validated.current_medications[0].name == "lisinopril"


# --------------------------------------------------------------------------- #
# UnknownDocument fallback when fed a lab PDF — no Anthropic call.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extract_intake_returns_unknown_for_lab_pdf() -> None:
    if not LAB_FIXTURE.exists():
        pytest.skip(f"fixture missing: {LAB_FIXTURE}")
    pdf_bytes = LAB_FIXTURE.read_bytes()

    with patch("extractors.intake.anthropic.AsyncAnthropic") as mock_client_cls:
        result = await extract_intake(
            pdf_bytes,
            patient_id="pt-test-001",
            document_reference_id="doc-test-001",
        )
        # No Anthropic client should have been constructed on the unknown path.
        mock_client_cls.assert_not_called()

    assert isinstance(result, UnknownDocument)
    assert result.kind == "unknown"
    # Lab keywords fire the classifier — guess should be "lab_report" here.
    assert result.document_kind_guess == "lab_report"
    assert result.summary  # non-empty

    # Fallback synthetic citation must carry bbox/page (UI overlay contract).
    assert result.key_facts, "fallback should produce at least one KeyFact"
    cit = result.key_facts[0].citations[0]
    assert cit.bbox is not None and len(cit.bbox) == 4
    assert all(isinstance(v, float) for v in cit.bbox)
    assert isinstance(cit.page, int) and cit.page >= 1


# --------------------------------------------------------------------------- #
# Validation-error path → ExtractionFailed (no PHI in message).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extract_intake_validation_error_raises_extraction_failed() -> None:
    if not INTAKE_FIXTURE.exists():
        pytest.skip(f"fixture missing: {INTAKE_FIXTURE}")
    pdf_bytes = INTAKE_FIXTURE.read_bytes()

    # Malformed input — missing required `patient_id`.
    bad_tool_use = SimpleNamespace(
        type="tool_use",
        name="submit_intake_form",
        input={
            "kind": "intake_form",
            "schema_version": "1.0",
            # intentionally missing "patient_id"
            "document_reference_id": "doc-x",
            "current_medications": [],
            "allergies": [],
            "family_history": [],
            "classifier_confidence": 0.9,
            "ocr_confidence_range": [1.0, 1.0],
            "extracted_at": "2026-05-04T00:00:00+00:00",
        },
    )
    fake_resp = SimpleNamespace(content=[bad_tool_use])

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_resp)

    with patch(
        "extractors.intake.anthropic.AsyncAnthropic", return_value=fake_client
    ):
        with pytest.raises(ExtractionFailed) as excinfo:
            await extract_intake(
                pdf_bytes,
                patient_id="pt-test-002",
                document_reference_id="doc-test-002",
            )

    msg = str(excinfo.value)
    # Generic — no PHI / vendor text.
    assert msg == "vision call failed"
    assert "pt-test-002" not in msg
    assert "Marcus" not in msg


# --------------------------------------------------------------------------- #
# Live happy-path E2E
# --------------------------------------------------------------------------- #


@pytest.mark.live_api
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
@pytest.mark.asyncio
async def test_extract_intake_happy_path_e2e() -> None:
    if not INTAKE_FIXTURE.exists():
        pytest.skip(f"fixture missing: {INTAKE_FIXTURE}")

    pdf_bytes = INTAKE_FIXTURE.read_bytes()
    result = await extract_intake(
        pdf_bytes,
        patient_id="pt-live-002",
        document_reference_id="doc-live-002",
    )

    assert isinstance(result, IntakeForm)
    assert result.code_status is not None
    assert result.code_status.value == "full_code"

    # >=1 cited allergy mentioning penicillin.
    matched_pcn = any(
        "penicillin" in a.substance.lower() and len(a.citations) >= 1
        for a in result.allergies
    )
    assert matched_pcn, f"penicillin allergy not extracted: {result.allergies}"

    # >=1 cited medication mentioning lisinopril.
    matched_lis = any(
        "lisinopril" in m.name.lower() and len(m.citations) >= 1
        for m in result.current_medications
    )
    assert matched_lis, (
        f"lisinopril medication not extracted: {result.current_medications}"
    )

    # Every citation whose bbox_id matches a layout block must carry bbox/page.
    from documents.ocr import extract_layout
    bbox_ids = {b.bbox_id for b in extract_layout(pdf_bytes)}
    walked = 0
    for m in result.current_medications:
        for c in m.citations:
            if c.field_or_chunk_id in bbox_ids:
                assert c.bbox is not None and len(c.bbox) == 4
                assert all(isinstance(coord, float) for coord in c.bbox)
                assert isinstance(c.page, int) and c.page >= 1
                walked += 1
    for a in result.allergies:
        for c in a.citations:
            if c.field_or_chunk_id in bbox_ids:
                assert c.bbox is not None and len(c.bbox) == 4
                assert isinstance(c.page, int) and c.page >= 1
                walked += 1
    assert walked >= 1, "expected at least one resolvable cited bbox in intake E2E"


# --------------------------------------------------------------------------- #
# /document/ingest dispatch tests
# --------------------------------------------------------------------------- #


def _make_intake_form(*, document_reference_id: str = "doc-test-1") -> IntakeForm:
    cit = Citation(
        source_type="document",
        source_id=document_reference_id,
        page_or_section="1",
        field_or_chunk_id="p1-b001",
        quote_or_value="Full Code",
    )
    return IntakeForm(
        kind="intake_form",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id=document_reference_id,
        demographics=None,
        chief_concern=None,
        current_medications=[
            MedicationItem(name="lisinopril", dose="10 mg", citations=[cit])
        ],
        allergies=[
            AllergyItem(substance="penicillin", reaction="rash", citations=[cit])
        ],
        family_history=[],
        code_status=CodeStatus(value="full_code", citations=[cit]),
        classifier_confidence=0.95,
        ocr_confidence_range=(0.85, 0.99),
        extracted_at=_dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )


def _make_lab_report_for_dispatch(*, document_reference_id: str = "doc-test-1"):
    from extractors.schemas import LabReport, LabValue

    cit = Citation(
        source_type="document",
        source_id=document_reference_id,
        page_or_section="1",
        field_or_chunk_id="p1-b001",
        quote_or_value="4.2 mmol/L",
    )
    return LabReport(
        kind="lab_report",
        schema_version="1.0",
        patient_id="pt-1",
        document_reference_id=document_reference_id,
        collection_facility="Memorial OSH",
        values=[
            LabValue(
                test_name="Lactate",
                normalized_test_name="lactate",
                value="4.2",
                unit="mmol/L",
                normalized_unit="mmol/L",
                reference_range="0.5-2.2",
                collection_date=None,
                abnormal_flag="critical_high",
                citations=[cit],
            )
        ],
        classifier_confidence=0.95,
        ocr_confidence_range=(0.85, 0.99),
        extracted_at=_dt.datetime(2026, 5, 4, 12, 0, 0, tzinfo=_dt.timezone.utc),
    )


def _patch_dispatch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    intake_return,
    lab_return,
    document_reference_id: str = "doc-test-1",
) -> dict[str, AsyncMock]:
    import main as main_module
    from documents import fhir_writer as _fhir_writer
    from documents import store as _store
    from documents.fhir_writer import WriteResult
    from documents.store import ClaimResult
    from extractors import intake as _intake
    from extractors import lab as _lab

    write_mock = AsyncMock(
        return_value=WriteResult(
            document_reference_id=document_reference_id,
            binary_id="bin-1",
            path="fhir",
        )
    )
    claim_mock = AsyncMock(
        return_value=ClaimResult(
            extraction_id=42, owns_claim=True, cached_payload=None
        )
    )
    complete_mock = AsyncMock(return_value=None)
    fail_mock = AsyncMock(return_value=None)
    intake_mock = AsyncMock(return_value=intake_return)
    lab_mock = AsyncMock(return_value=lab_return)
    audit_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(_fhir_writer, "write_document", write_mock)
    monkeypatch.setattr(_store, "claim_or_get", claim_mock)
    monkeypatch.setattr(_store, "complete", complete_mock)
    monkeypatch.setattr(_store, "fail", fail_mock)
    monkeypatch.setattr(_intake, "extract_intake", intake_mock)
    monkeypatch.setattr(_lab, "extract", lab_mock)
    monkeypatch.setattr(main_module.audit_writer, "emit", audit_mock)

    return {
        "write_document": write_mock,
        "claim_or_get": claim_mock,
        "complete": complete_mock,
        "fail": fail_mock,
        "extract_intake": intake_mock,
        "extract": lab_mock,
        "audit": audit_mock,
    }


async def _post_ingest(pdf_bytes: bytes, *, patient_id: str = "pt-1") -> httpx.Response:
    import main as main_module

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("upload.pdf", pdf_bytes, "application/pdf")}
        data: dict[str, str] = {"patient_id": patient_id}
        return await client.post("/document/ingest", files=files, data=data)


@pytest.mark.asyncio
async def test_document_ingest_routes_intake_form_to_intake_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not INTAKE_FIXTURE.exists():
        pytest.skip(f"fixture missing: {INTAKE_FIXTURE}")
    pdf_bytes = INTAKE_FIXTURE.read_bytes()

    mocks = _patch_dispatch_pipeline(
        monkeypatch,
        intake_return=_make_intake_form(),
        lab_return=_make_lab_report_for_dispatch(),
    )

    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["extraction"]["kind"] == "intake_form"
    assert mocks["extract_intake"].await_count == 1
    assert mocks["extract"].await_count == 0


@pytest.mark.asyncio
async def test_document_ingest_still_routes_lab_to_lab_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not LAB_FIXTURE.exists():
        pytest.skip(f"fixture missing: {LAB_FIXTURE}")
    pdf_bytes = LAB_FIXTURE.read_bytes()

    mocks = _patch_dispatch_pipeline(
        monkeypatch,
        intake_return=_make_intake_form(),
        lab_return=_make_lab_report_for_dispatch(),
    )

    resp = await _post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["extraction"]["kind"] == "lab_report"
    assert mocks["extract"].await_count == 1
    assert mocks["extract_intake"].await_count == 0


# --------------------------------------------------------------------------- #
# Wave 2B — spatial repointer + structural anchor detection.
#
# These tests are pure-Python (no PDF, no Anthropic). They construct synthetic
# LayoutBlocks and exercise the repointer / anchor-detection helpers directly.
# --------------------------------------------------------------------------- #


def _block(
    bbox_id: str,
    page: int,
    bbox: tuple[float, float, float, float],
    text: str,
    granularity: str | None = None,
) -> "object":
    """Construct a LayoutBlock-like object. Uses the real LayoutBlock dataclass
    when no granularity is requested; falls back to a SimpleNamespace when
    `granularity` is given so tests work whether or not Wave 2A's field has
    landed on the dataclass yet."""
    from documents.ocr import LayoutBlock as _LayoutBlock

    if granularity is None:
        return _LayoutBlock(
            bbox_id=bbox_id,
            page=page,
            bbox=bbox,
            text=text,
            ocr_confidence=1.0,
        )
    return SimpleNamespace(
        bbox_id=bbox_id,
        page=page,
        bbox=bbox,
        text=text,
        ocr_confidence=1.0,
        granularity=granularity,
    )


def _make_citation(field_or_chunk_id: str = "p1-bXXX") -> Citation:
    return Citation(
        source_type="document",
        source_id="doc-x",
        page_or_section="1",
        field_or_chunk_id=field_or_chunk_id,
        quote_or_value="placeholder",
    )


def test_repointer_zero_candidates_returns_none() -> None:
    from extractors.intake import _find_block_for_value

    blocks = [
        _block("p1-b001", 1, (0.0, 0.0, 100.0, 12.0), "DEMOGRAPHICS"),
        _block("p1-b002", 1, (0.0, 20.0, 100.0, 12.0), "Address: 123 Main St"),
    ]
    # No block contains "06/08/1971".
    assert _find_block_for_value("06/08/1971", blocks) is None


def test_repointer_one_candidate_returns_it() -> None:
    from extractors.intake import _find_block_for_value

    blocks = [
        _block("p1-b001", 1, (0.0, 0.0, 100.0, 12.0), "DEMOGRAPHICS"),
        _block("p1-b002", 1, (0.0, 20.0, 100.0, 12.0), "DOB 06/08/1971"),
    ]
    chosen = _find_block_for_value("06/08/1971", blocks)
    assert chosen is not None
    assert chosen.bbox_id == "p1-b002"


def test_repointer_two_candidates_same_y_band_prefers_line_granularity() -> None:
    """When two candidates have the same overlap and similar y-distance to
    anchors, LINE granularity wins over WORD granularity."""
    from extractors.intake import _find_block_for_value

    blocks = [
        # Tall, all-caps, short, colon-terminated → anchor.
        _block("p1-b000", 1, (0.0, 0.0, 200.0, 30.0), "DEMOGRAPHICS:"),
        # Two candidates at the same y-band, same overlap. WORD-granularity
        # one comes first in iteration order; LINE-granularity should win.
        _block(
            "p1-b001",
            1,
            (0.0, 50.0, 80.0, 12.0),
            "06/08/1971",
            granularity="WORD",
        ),
        _block(
            "p1-b002",
            1,
            (90.0, 50.0, 200.0, 12.0),
            "06/08/1971",
            granularity="LINE",
        ),
    ]
    chosen = _find_block_for_value("06/08/1971", blocks, field_name="dob")
    assert chosen is not None
    assert chosen.bbox_id == "p1-b002", "expected LINE-granularity candidate to win"


def test_repointer_two_candidates_different_y_bands_prefers_field_compatible_anchor() -> None:
    """For `dob`, a candidate near a "DEMOGRAPHICS" anchor must beat one
    near "SIGNATURE", even though both contain the value text."""
    from extractors.intake import _find_block_for_value

    blocks = [
        # Anchor 1: DEMOGRAPHICS (top of page).
        _block("p1-b000", 1, (0.0, 0.0, 200.0, 30.0), "DEMOGRAPHICS:"),
        # DOB near DEMOGRAPHICS.
        _block("p1-b001", 1, (0.0, 50.0, 200.0, 12.0), "DOB: 06/08/1971"),
        # Anchor 2: SIGNATURE (bottom of page).
        _block("p1-b900", 1, (0.0, 800.0, 200.0, 30.0), "SIGNATURE:"),
        # Stray match near SIGNATURE (e.g. typed-in date on signature line).
        _block("p1-b901", 1, (0.0, 850.0, 200.0, 12.0), "Signed 06/08/1971"),
    ]
    chosen = _find_block_for_value("06/08/1971", blocks, field_name="dob")
    assert chosen is not None
    assert chosen.bbox_id == "p1-b001"


def test_repointer_two_candidates_no_anchors_falls_back_to_overlap() -> None:
    """When no structural anchors are detectable (e.g. all blocks are body
    paragraphs), the selector falls back to longest-overlap behavior."""
    from extractors.intake import _detect_section_anchors, _find_block_for_value

    blocks = [
        # All blocks are paragraph-style: lowercase, long, no colon. No
        # block satisfies ≥2 anchor signals.
        _block(
            "p1-b001",
            1,
            (0.0, 0.0, 400.0, 12.0),
            "the patient reports a date of birth of 06/08/1971 on intake.",
        ),
        _block(
            "p1-b002",
            1,
            (0.0, 30.0, 400.0, 12.0),
            "previous correspondence references date 06/08/1971 incidentally.",
        ),
    ]
    assert _detect_section_anchors(blocks) == []
    # Both blocks contain the value substring; tie on overlap. The fallback
    # path is deterministic — first candidate (b001) wins.
    chosen = _find_block_for_value("06/08/1971", blocks, field_name="dob")
    assert chosen is not None
    assert chosen.bbox_id == "p1-b001"


def test_anchor_detection_recognizes_caps_with_colon() -> None:
    from extractors.intake import _detect_section_anchors

    blocks = [
        _block("p1-b001", 1, (0.0, 0.0, 200.0, 12.0), "DEMOGRAPHICS:"),
        _block(
            "p1-b002",
            1,
            (0.0, 20.0, 400.0, 12.0),
            "the patient is a 54 year old male presenting with acute chest pain.",
        ),
    ]
    anchors = _detect_section_anchors(blocks)
    assert any(a.bbox_id == "p1-b001" for a in anchors)


def test_anchor_detection_rejects_paragraph_text() -> None:
    """A long lowercase sentence must not be classified as a section anchor."""
    from extractors.intake import _detect_section_anchors

    blocks = [
        _block(
            "p1-b001",
            1,
            (0.0, 0.0, 400.0, 12.0),
            "the patient reports occasional headaches over the past month.",
        ),
    ]
    assert _detect_section_anchors(blocks) == []


def test_repoint_citation_emits_log_and_metric() -> None:
    """End-to-end check: _repoint_citation increments the Prometheus counter
    and produces a log line with the new extras fields populated."""
    import logging as _logging

    from extractors.intake import _index_blocks, _repoint_citation
    from agent.metrics import agent_citation_repoint_total

    blocks = [
        _block("p1-b000", 1, (0.0, 0.0, 200.0, 30.0), "DEMOGRAPHICS:"),
        _block("p1-b001", 1, (0.0, 50.0, 200.0, 12.0), "DOB: 06/08/1971"),
    ]
    block_index = _index_blocks(blocks)
    cit = _make_citation(field_or_chunk_id="p1-b000")  # LLM parked on header.

    before = agent_citation_repoint_total.labels(
        field="dob", outcome="repointed_no_anchor"
    )._value.get()  # type: ignore[attr-defined]

    captured: list[_logging.LogRecord] = []

    class _Cap(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            captured.append(record)

    h = _Cap(level=_logging.INFO)
    target_logger = _logging.getLogger("extractors.intake")
    target_logger.addHandler(h)
    try:
        out = _repoint_citation(
            cit,
            "06/08/1971",
            blocks,
            block_index,
            field_name="dob",
        )
    finally:
        target_logger.removeHandler(h)

    assert out.field_or_chunk_id == "p1-b001"
    assert any(r.getMessage() == "extractor_citation_repointed" for r in captured)
    repointed = next(
        r for r in captured if r.getMessage() == "extractor_citation_repointed"
    )
    assert getattr(repointed, "field_name", None) == "dob"
    assert getattr(repointed, "chosen_bbox_id", None) == "p1-b001"
    assert getattr(repointed, "candidate_count", None) == 1
    # One candidate path uses repointed_no_anchor outcome.
    after_no_anchor = agent_citation_repoint_total.labels(
        field="dob", outcome="repointed_no_anchor"
    )._value.get()  # type: ignore[attr-defined]
    assert after_no_anchor == before + 1
