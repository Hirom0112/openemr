"""Types specific to the HL7 v2 parser (Phase 9 Slice 9.4).

``DemographicUpdateEvent`` is the structured-lane analogue of
``IntakeForm.demographics`` for ADT^A08 messages. It is **not** a
discriminated-union member of ``ExtractionResult`` — ADT messages do
not produce LabReport / IntakeForm / UnknownDocument payloads, they
produce demographic deltas that Slice 9.2's resolver will route into
the patient-update path.

``CandidateHints`` is the lightweight dataclass returned by
``probe_identity`` — used by the dispatcher and (eventually) by
Slice 9.2's identity resolver to score MRN/name/DOB matches before
running the full hl7apy parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Literal, Optional

from extractors.schemas import Citation
from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Probe-stage hints (pre-dispatch, regex-only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CandidateHints:
    """Lightweight identity hints extracted by a regex probe.

    Returned by :func:`parsers.hl7.probe.probe_identity`. All fields are
    optional because a malformed PID may yield only some of them; the
    dispatcher / resolver decides what to do with partial hints.
    """

    mrn: Optional[str]
    name_family: Optional[str]
    name_given: Optional[str]
    dob: Optional[str]


# --------------------------------------------------------------------------- #
# DemographicUpdateEvent (ADT^A08 output)
# --------------------------------------------------------------------------- #


class DemographicField(BaseModel):
    """A single demographic data point with its provenance."""

    model_config = ConfigDict(strict=True, extra="forbid")

    value: str
    citations: List[Citation] = Field(min_length=1)


class DemographicUpdateEvent(BaseModel):
    """The output of an ADT^A08 parse.

    v1 carries the fields that Slice 9.2's resolver and the §5.6 wrong-
    patient detector consume: MRN (PID-3.1), name (PID-5), DOB (PID-7),
    sex (PID-8), address (PID-11), home phone (PID-13), marital status
    (PID-16-but v1-drops), language (PID-15-but v1-drops), race
    (PID-10), and a small set of admin fields from PV1 / NK1 / GT1 /
    IN1. PD1-4 NPI sync, EVN-6 free text, PID-15/16/22 are explicitly
    dropped per todo.md Slice 9.4. Every populated field carries a
    Citation back to the source segment with synthetic locator (e.g.
    ``PID-3.1``, ``PV1-3``).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["demographic_update"] = "demographic_update"
    schema_version: Literal["1.0"] = "1.0"
    patient_id: str
    document_reference_id: str
    event_type: str  # ADT^A08
    control_id: str  # MSH-10
    # Identity fields (PID)
    mrn: Optional[DemographicField] = None
    name_family: Optional[DemographicField] = None
    name_given: Optional[DemographicField] = None
    name_middle: Optional[DemographicField] = None
    dob: Optional[date] = None
    sex: Optional[DemographicField] = None
    race: Optional[DemographicField] = None
    address_line: Optional[DemographicField] = None
    address_city: Optional[DemographicField] = None
    address_state: Optional[DemographicField] = None
    address_postal: Optional[DemographicField] = None
    home_phone: Optional[DemographicField] = None
    # Visit (PV1)
    patient_class: Optional[DemographicField] = None
    assigned_location: Optional[DemographicField] = None
    attending_npi: Optional[DemographicField] = None
    # Next of kin (NK1) — first occurrence only in v1
    nok_name: Optional[DemographicField] = None
    nok_relationship: Optional[DemographicField] = None
    nok_phone: Optional[DemographicField] = None
    # Guarantor (GT1) — first occurrence only in v1
    guarantor_name: Optional[DemographicField] = None
    # Insurance (IN1) — first occurrence only in v1
    insurance_plan_id: Optional[DemographicField] = None
    insurance_company: Optional[DemographicField] = None
    insurance_member_id: Optional[DemographicField] = None
    extracted_at: datetime
