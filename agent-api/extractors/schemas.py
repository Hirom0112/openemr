"""Pydantic v2 schemas for document extraction (W2_ARCHITECTURE.md §7-§8).

Strict mode + extra=forbid throughout. The discriminated union on `kind`
gates downstream routing; the Citation contract (§8) is what the critic
walks for resolution and fidelity checks.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Citation contract (§8.1)
# --------------------------------------------------------------------------- #


class Citation(BaseModel):
    """A single citation tying a clinical claim back to a source region."""

    model_config = ConfigDict(strict=True, extra="forbid")

    source_type: Literal["document", "observation", "guideline"]
    source_id: str
    page_or_section: Optional[str] = None
    field_or_chunk_id: str
    quote_or_value: str
    # Layout coordinates carried through from the OCR layout block keyed by
    # ``field_or_chunk_id``. Optional because (a) cached payloads written
    # before this schema change won't have them, and (b) non-document
    # citation sources (observation / guideline) have no bbox. When present:
    # ``bbox`` is in the same coordinate frame as ``documents.ocr.LayoutBlock.bbox``
    # — (x, y, w, h) in PDF user-space points — and ``page`` is 1-indexed.
    bbox: Optional[Tuple[float, float, float, float]] = None
    page: Optional[int] = None


# --------------------------------------------------------------------------- #
# LabReport (§7.1)
# --------------------------------------------------------------------------- #


class LabValue(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    test_name: str
    normalized_test_name: str
    value: str
    unit: Optional[str] = None
    normalized_unit: Optional[str] = None
    reference_range: Optional[str] = None
    collection_date: Optional[date] = None
    abnormal_flag: Literal[
        "high",
        "low",
        "critical_high",
        "critical_low",
        "normal",
        "unknown",
    ]
    citations: List[Citation] = Field(min_length=1)


class LabReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["lab_report"]
    schema_version: Literal["1.0"]
    patient_id: str
    document_reference_id: str
    collection_facility: Optional[str] = None
    values: List[LabValue]
    classifier_confidence: float
    ocr_confidence_range: Tuple[float, float]
    extracted_at: datetime


# --------------------------------------------------------------------------- #
# UnknownDocument (§7.3)
# --------------------------------------------------------------------------- #


class KeyFact(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    text: str
    citations: List[Citation] = Field(min_length=1)


class UnknownDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["unknown"]
    schema_version: Literal["1.0"]
    patient_id: str
    document_reference_id: str
    document_kind_guess: str
    summary: str
    key_facts: List[KeyFact]
    classifier_confidence: float
    ocr_confidence_range: Tuple[float, float]
    extracted_at: datetime


# --------------------------------------------------------------------------- #
# IntakeForm (§7.2)
# --------------------------------------------------------------------------- #


class TextField(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    value: str
    citations: List[Citation] = Field(min_length=1)


class Demographics(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: Optional[TextField] = None
    dob: Optional[TextField] = None
    sex: Optional[TextField] = None
    mrn: Optional[TextField] = None
    address: Optional[TextField] = None


class MedicationItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: str
    dose: Optional[str] = None
    citations: List[Citation] = Field(min_length=1)


class AllergyItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    substance: str
    reaction: Optional[str] = None
    citations: List[Citation] = Field(min_length=1)


class FamilyHistoryItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    relation: str
    condition: str
    citations: List[Citation] = Field(min_length=1)


class CodeStatus(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    value: Literal["full_code", "DNR", "DNI", "comfort_care", "POLST", "unknown"]
    citations: List[Citation] = Field(min_length=1)


class IntakeForm(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["intake_form"] = "intake_form"
    schema_version: Literal["1.0"] = "1.0"
    patient_id: str
    document_reference_id: str
    demographics: Optional[Demographics] = None
    chief_concern: Optional[TextField] = None
    current_medications: List[MedicationItem] = Field(default_factory=list)
    allergies: List[AllergyItem] = Field(default_factory=list)
    family_history: List[FamilyHistoryItem] = Field(default_factory=list)
    code_status: Optional[CodeStatus] = None
    classifier_confidence: float
    ocr_confidence_range: Tuple[float, float]
    extracted_at: datetime


# --------------------------------------------------------------------------- #
# Discriminated union (§7.4)
# --------------------------------------------------------------------------- #


ExtractionResult = Annotated[
    Union[LabReport, IntakeForm, UnknownDocument],
    Field(discriminator="kind"),
]
