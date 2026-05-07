"""Tests for parsers.hl7.oru (Phase 9 Slice 9.4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from extractors.schemas import LabReport
from parsers.hl7 import ParserMalformedError, parse_hl7
from parsers.hl7.dispatch import _import_metrics

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "w2" / "multimodal" / "hl7v2"


def test_p01_chen_oru_r01_full_round_trip():
    """Full LabReport round-trip for the lipid panel fixture."""
    raw = (FIXTURE_DIR / "p01-chen-oru-r01.hl7").read_bytes()
    report = parse_hl7(raw, document_reference_id="DOC-p01-oru", patient_id="patient-1")
    assert isinstance(report, LabReport)
    assert report.kind == "lab_report"
    assert report.patient_id == "patient-1"
    assert report.document_reference_id == "DOC-p01-oru"
    assert report.classifier_confidence == 1.0
    assert report.ocr_confidence_range == (1.0, 1.0)
    assert len(report.values) == 4

    expected = [
        ("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "218", "mg/dL", "<200", "high"),
        (
            "2089-1",
            "Cholesterol in LDL [Mass/volume] in Serum or Plasma by Direct assay",
            "142",
            "mg/dL",
            "<100",
            "high",
        ),
        (
            "2085-9",
            "Cholesterol in HDL [Mass/volume] in Serum or Plasma",
            "48",
            "mg/dL",
            ">=40",
            "normal",
        ),
        ("2571-8", "Triglyceride [Mass/volume] in Serum or Plasma", "168", "mg/dL", "<150", "high"),
    ]
    for lab_value, (loinc, name, value, unit, ref, flag) in zip(report.values, expected):
        assert lab_value.test_name == name
        assert lab_value.value == value
        assert lab_value.unit == unit
        assert lab_value.reference_range == ref
        assert lab_value.abnormal_flag == flag
        assert lab_value.collection_date == date(2026, 4, 12)
        # Two citations: OBX-5 (value) and OBX-3.1 (LOINC code)
        assert len(lab_value.citations) == 2
        value_citation, loinc_citation = lab_value.citations
        assert value_citation.field_or_chunk_id.startswith("OBX-5|seg=")
        assert value_citation.quote_or_value == value
        assert loinc_citation.field_or_chunk_id.startswith("OBX-3.1|seg=")
        assert loinc_citation.quote_or_value == loinc


def test_p01_synthetic_locator_uses_absolute_segment_index():
    """seg=N must be the parser-assigned absolute segment index (1-based,
    across the whole message), NOT OBX-1 set ID. p01 layout is:
       MSH(1) PID(2) PV1(3) ORC(4) OBR(5) OBX(6) OBX(7) OBX(8) OBX(9) NTE(10).
    So the four OBX segments must report seg=6..9."""
    raw = (FIXTURE_DIR / "p01-chen-oru-r01.hl7").read_bytes()
    report = parse_hl7(raw, document_reference_id="DOC-p01-oru", patient_id="patient-1")
    seg_indices = []
    for lv in report.values:
        for cit in lv.citations:
            if cit.field_or_chunk_id.startswith("OBX-5|seg="):
                seg_indices.append(int(cit.field_or_chunk_id.split("=", 1)[1]))
    assert seg_indices == [6, 7, 8, 9]


def test_p06_johnson_bnp_hh_maps_to_critical_high():
    """Canary: HL70078 abnormal-flag 'HH' (critical high) on the BNP=842
    OBX must surface as LabValue.abnormal_flag='critical_high'."""
    raw = (FIXTURE_DIR / "p06-johnson-oru-r01.hl7").read_bytes()
    report = parse_hl7(raw, document_reference_id="DOC-p06-oru", patient_id="patient-6")
    assert isinstance(report, LabReport)

    # Find the BNP value — LOINC 30934-4
    bnp = next(
        lv
        for lv in report.values
        if any(c.quote_or_value == "30934-4" for c in lv.citations)
    )
    assert bnp.value == "842"
    assert bnp.unit == "pg/mL"
    assert bnp.abnormal_flag == "critical_high"


def test_p06_repeated_obx_segments_disambiguated_by_seg_index():
    """p06 has two OBR groups; seg= indices must be globally unique even
    when OBX-1 set IDs collide across OBR groups."""
    raw = (FIXTURE_DIR / "p06-johnson-oru-r01.hl7").read_bytes()
    report = parse_hl7(raw, document_reference_id="DOC-p06-oru", patient_id="patient-6")
    seg_indices = []
    for lv in report.values:
        for cit in lv.citations:
            if cit.field_or_chunk_id.startswith("OBX-5|seg="):
                seg_indices.append(int(cit.field_or_chunk_id.split("=", 1)[1]))
    # All seg indices must be unique — distinct OBX segments must not collide.
    assert len(seg_indices) == len(set(seg_indices))


def test_missing_pid_raises_malformed_error():
    raw = (
        b"MSH|^~\\&|A|B|C|D|20260101000000||ORU^R01^ORU_R01|MSG-bad|P|2.5.1\r"
        b"OBR|1|ORD|FIL|57698-3^Lipid panel^LN\r"
    )
    with pytest.raises(ParserMalformedError) as excinfo:
        parse_hl7(raw, document_reference_id="DOC-x", patient_id="patient-x")
    assert excinfo.value.code == "hl7_missing_pid"
    assert excinfo.value.control_id == "MSG-bad"


def test_metrics_helper_returns_pair_when_prometheus_available():
    """Sanity: dispatch._import_metrics must return both registered
    instruments so the dispatcher can record outcomes."""
    counter, histogram = _import_metrics()
    assert counter is not None
    assert histogram is not None


def test_nte_at_report_level_logged_not_attached(caplog):
    """v1 schema gap: NTE-3 cannot ride on LabValue (extra='forbid'); we
    log it instead. Confirm at least one ``hl7_nte_dropped`` event."""
    import logging

    raw = (FIXTURE_DIR / "p01-chen-oru-r01.hl7").read_bytes()
    with caplog.at_level(logging.INFO, logger="parsers.hl7.oru"):
        parse_hl7(raw, document_reference_id="DOC-p01-oru", patient_id="patient-1")
    nte_events = [r for r in caplog.records if r.message == "hl7_nte_dropped"]
    assert nte_events, "expected hl7_nte_dropped log event for the OBR-trailing NTE"
