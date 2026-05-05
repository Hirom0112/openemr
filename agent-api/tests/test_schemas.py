"""Tests for extractors/schemas.py — Slice 1.2.

Verifies strict-mode validation, citation requirements, and the
discriminated union on `kind`.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.schemas import (  # noqa: E402
    Citation,
    ExtractionResult,
    LabReport,
    LabValue,
    UnknownDocument,
)

pytestmark = pytest.mark.hard_failure


def _citation() -> dict:
    return {
        "source_type": "document",
        "source_id": "doc-1",
        "page_or_section": "2",
        "field_or_chunk_id": "p2-b017",
        "quote_or_value": "4.2",
    }


def _minimal_lab_report_dict() -> dict:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "pt-001",
        "document_reference_id": "doc-1",
        "collection_facility": "OSH Lab",
        "values": [
            {
                "test_name": "Lactate",
                "normalized_test_name": "lactate",
                "value": "4.2",
                "unit": "mmol/L",
                "normalized_unit": "mmol/L",
                "reference_range": "0.5 - 2.2",
                "collection_date": date(2026, 4, 30),
                "abnormal_flag": "critical_high",
                "citations": [_citation()],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": (1.0, 1.0),
        "extracted_at": datetime.now(timezone.utc),
    }


def test_lab_report_round_trip() -> None:
    data = _minimal_lab_report_dict()
    report = LabReport.model_validate(data)
    dumped_json = report.model_dump_json()
    re_validated = LabReport.model_validate_json(dumped_json)
    assert re_validated.values[0].value == "4.2"
    assert re_validated.values[0].citations[0].field_or_chunk_id == "p2-b017"


def test_lab_value_requires_citations() -> None:
    bad = {
        "test_name": "Lactate",
        "normalized_test_name": "lactate",
        "value": "4.2",
        "abnormal_flag": "critical_high",
        # citations omitted → must fail
    }
    with pytest.raises(ValidationError):
        LabValue.model_validate(bad)

    # Empty list also rejected (min_length=1).
    bad2 = {**bad, "citations": []}
    with pytest.raises(ValidationError):
        LabValue.model_validate(bad2)


def test_extraction_result_discriminator() -> None:
    adapter = TypeAdapter(ExtractionResult)

    lab = adapter.validate_python(_minimal_lab_report_dict())
    assert isinstance(lab, LabReport)

    unknown_data = {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "pt-001",
        "document_reference_id": "doc-1",
        "document_kind_guess": "consultant note",
        "summary": "free-text summary of unknown doc",
        "key_facts": [
            {
                "text": "discharge planned for tomorrow",
                "citations": [_citation()],
            }
        ],
        "classifier_confidence": 0.4,
        "ocr_confidence_range": (1.0, 1.0),
        "extracted_at": datetime.now(timezone.utc),
    }
    unknown = adapter.validate_python(unknown_data)
    assert isinstance(unknown, UnknownDocument)


def test_citation_strict_extra_forbid() -> None:
    bad = {**_citation(), "unknown_field": "x"}
    with pytest.raises(ValidationError):
        Citation.model_validate(bad)
