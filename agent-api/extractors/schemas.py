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
# Discriminated union (§7.4) — IntakeForm intentionally deferred for spike.
# --------------------------------------------------------------------------- #


ExtractionResult = Annotated[
    Union[LabReport, UnknownDocument],
    Field(discriminator="kind"),
]
