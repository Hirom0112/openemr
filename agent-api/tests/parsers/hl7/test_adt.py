"""Tests for parsers.hl7.adt (Phase 9 Slice 9.4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from parsers.hl7 import parse_hl7
from parsers.hl7.types import DemographicUpdateEvent

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "w2" / "multimodal" / "hl7v2"


def test_p01_chen_adt_a08_produces_demographic_update_event():
    raw = (FIXTURE_DIR / "p01-chen-adt-a08.hl7").read_bytes()
    event = parse_hl7(raw, document_reference_id="DOC-p01-adt", patient_id="patient-1")
    assert isinstance(event, DemographicUpdateEvent)
    assert event.event_type == "ADT^A08"
    assert event.control_id == "MSG-p01-20260506143215-ADT"

    # PID-3.1 (MRN)
    assert event.mrn is not None
    assert event.mrn.value == "BHS-2847163"
    assert event.mrn.citations[0].field_or_chunk_id == "PID-3.1"
    assert event.mrn.citations[0].source_type == "document"
    assert event.mrn.citations[0].source_id == "DOC-p01-adt"

    # PID-5 — name
    assert event.name_family is not None
    assert event.name_family.value == "CHEN"
    assert event.name_family.citations[0].field_or_chunk_id == "PID-5.1"
    assert event.name_given is not None
    assert event.name_given.value == "MARGARET"
    assert event.name_given.citations[0].field_or_chunk_id == "PID-5.2"

    # PID-7 — DOB parsed to date
    assert event.dob == date(1968, 3, 12)

    # PID-8 — sex
    assert event.sex is not None
    assert event.sex.value == "F"
    assert event.sex.citations[0].field_or_chunk_id == "PID-8"

    # PV1 — assigned location
    assert event.assigned_location is not None
    assert event.assigned_location.citations[0].field_or_chunk_id == "PV1-3"
