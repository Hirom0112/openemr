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


class VerificationResult(BaseModel):
    """Outcome of the optional Wave-2C ``citation_verifier`` second pass.

    Attached to a :class:`Citation` when the verifier ran for that citation
    (see ``agent.citation_verifier``). ``status`` follows the contract:

    * ``yes``     — Claude vision confirmed the value is visible in the crop.
    * ``partial`` — only a fragment was visible; the upstream pipeline
                    downgrades WORD-granularity citations to LINE.
    * ``no``      — Claude reported the value is NOT in the crop. The
                    pipeline repoints once with the rejected bbox excluded;
                    if the second pass also returns ``no`` the citation is
                    dropped and the parent value is flagged ``needs_review``.

    ``rationale`` is the verifier's one-line justification, capped at
    100 chars by the prompt and re-asserted here.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["yes", "no", "partial"]
    rationale: str = Field(min_length=1, max_length=100)


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
    # Wave 2B (polygon-aware citations) — when the source LayoutBlock carries
    # a polygon (e.g. paddle line-level shape), we propagate it onto the
    # Citation so the UI can render the true shape and the IoU rubric can
    # compute polygon-vs-polygon overlap rather than falling back to bbox.
    # Coordinates share the SAME frame as ``bbox``. ``None`` when the source
    # engine has no polygon (tesseract, PDF text-layer) or the block was
    # synthesized via approximation. We use a list-of-pairs (rather than a
    # tuple-of-tuples) so it round-trips cleanly through pydantic JSON.
    polygon: Optional[List[Tuple[float, float]]] = None
    # Wave 2B tiebreaker hint: short text label (1-3 words) the LLM saw
    # immediately preceding the value. Used by the y-band repointer ONLY
    # to break ties between candidate blocks at equal |Δy| to the anchor —
    # never to veto a block that already won on |Δy|. Optional and
    # additive; older payloads without this field still validate.
    nearest_label: Optional[str] = None
    # Wave 2C — optional self-verification result attached by
    # ``agent.citation_verifier``. ``None`` when the verifier did not run for
    # this citation (flag off, sampled-out, or cap-skipped). Older payloads
    # without this field still validate.
    verification: Optional[VerificationResult] = None


# --------------------------------------------------------------------------- #
# LabReport (§7.1)
# --------------------------------------------------------------------------- #


class LabValue(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    test_name: str
    normalized_test_name: str
    value: str
    unit: Optional[str] = None
    # Carried through from sources that name the LOINC directly
    # (e.g. HL7 OBX-3.1, FHIR Observation.code.coding.code). When set,
    # ``observations.writer`` uses it as the authoritative code so that
    # multiple rows with distinct LOINCs yield distinct
    # ``deterministic_observation_id``s and don't collapse via UPSERT.
    loinc_code: Optional[str] = None
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
    # Wave 2C — set by ``agent.citation_verifier`` when every citation it
    # could attach to this value came back ``no`` (twice, after one repoint
    # attempt). The UI surfaces a "needs review" affordance for the value.
    needs_review: bool = False


class LabReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["lab_report"]
    schema_version: Literal["1.0"]
    patient_id: str
    document_reference_id: str
    collection_facility: Optional[str] = None
    values: List[LabValue]
    # Patient demographics printed on the lab report itself (Whitaker / DOB
    # / MRN at the top of the document). Reuses the same `Demographics`
    # model the intake-form path uses, so the review panel renders one
    # card per surfaced sub-field via the existing `'demographics'` UI
    # editor branch — no UI change required. The `Demographics` model is
    # forward-declared earlier in this file (used by `IntakeForm`); the
    # forward reference resolves cleanly at module import. Optional and
    # additive — older payloads without this field still validate.
    patient_demographics: Optional["Demographics"] = None
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
    # Wave 2C — see :class:`VerificationResult`. ``True`` when the verifier
    # rejected every citation for this fact even after a single repoint.
    needs_review: bool = False


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
    # Wave 2C — see :class:`VerificationResult`.
    needs_review: bool = False


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
    needs_review: bool = False
    # Phase 9 Slice 9.1 — XLSX Medications sheet carries Indication, Prescriber,
    # Last_Filled, and Refills_Remaining columns that the v1 IntakeForm path
    # could not represent. They're optional and additive; older payloads
    # without these fields still validate.
    indication: Optional[TextField] = None
    prescriber: Optional[TextField] = None
    last_filled: Optional[date] = None
    refills_remaining: Optional[int] = None


class AllergyItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    substance: str
    reaction: Optional[str] = None
    citations: List[Citation] = Field(min_length=1)
    needs_review: bool = False


class FamilyHistoryItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    relation: str
    condition: str
    # Optional richer context. Strings (not ints) for age fields because
    # source documents commonly carry imprecise values like "~1999",
    # "50s", "Unknown". The extractor must omit any field it cannot
    # ground in the OCR — never fabricate.
    age_at_onset: Optional[str] = None
    status: Optional[str] = None
    snomed_code: Optional[str] = None
    citations: List[Citation] = Field(min_length=1)
    needs_review: bool = False


class CodeStatus(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    value: Literal["full_code", "DNR", "DNI", "comfort_care", "POLST", "unknown"]
    citations: List[Citation] = Field(min_length=1)
    needs_review: bool = False


class ProblemListItem(BaseModel):
    """One PMH / problem-list entry from an intake or referral document.

    Mirrors the FamilyHistoryItem shape (relation/condition + optional
    contextual fields) but tuned for problem-list rows. ``icd10_code`` and
    ``snomed_code`` are optional because real intake forms frequently
    carry only the problem text; when codes ARE present the extractor
    must literal-substring-ground them in the source (see
    ``extractors.intake.validate_icd10_grounded`` for the runtime check
    that drops hallucinated codes pre-staging).

    ``onset_date`` is a string (not a date) because real source
    documents carry imprecise values like ``"~2018"``, ``"adolescence"``,
    ``"unknown"`` that don't survive ``date.fromisoformat()``.

    ``status`` is the FHIR-aligned clinical status, mapped from the
    document's STATUS column when present.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    condition: str
    icd10_code: Optional[str] = None
    snomed_code: Optional[str] = None
    onset_date: Optional[str] = None
    status: Optional[Literal["active", "resolved", "inactive"]] = None
    citations: List[Citation] = Field(default_factory=list)
    needs_review: bool = False


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
    # Lab values surfaced from a non-LabReport document (e.g. labs section
    # inside a referral letter or admission note). Reuses ``LabValue`` so
    # the schema, citation contract, and UI editor are shared with the
    # primary lab extraction path. Provenance stays distinct: these stage
    # as ``IntakeFormField`` rows (informational approval, no FHIR write),
    # NOT as ``Observation`` rows like the LabReport flow. Optional and
    # additive — older payloads without this field still validate.
    pertinent_labs: List[LabValue] = Field(default_factory=list)
    # PMH / problem list — surfaced when the document has a Problem List
    # / Past Medical History section. Each item carries condition + optional
    # ICD-10 / SNOMED codes; ICD-10 codes pass through the literal-grounding
    # guardrail in ``extractors.intake`` before staging so hallucinated codes
    # are dropped (problem still surfaces, code goes None).
    problem_list: List[ProblemListItem] = Field(default_factory=list)
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


# --------------------------------------------------------------------------- #
# PendingTask (Phase 9 Slice 9.1) — XLSX Care_Gaps staging row
# --------------------------------------------------------------------------- #
#
# PendingTask is NOT a discriminated-union member of ``ExtractionResult``. It
# is a separate top-level model used by the XLSX Care_Gaps importer to stage
# rows that will eventually become FHIR Tasks. The dispatch path is:
#
#     XLSX Care_Gaps row → PendingTask → copilot_pending_extractions
#         (target_resource_type='Task') → clinician approval → FHIR Task POST
#
# Per todo.md Slice 9.5 the v1 build has no Task writer endpoint, so approved
# rows transition to ``failed`` with ``write_error='task_writer_unavailable'``.
# The schema here lands in Slice 9.1 so the staging table can carry the
# payload from day one.


class PendingTask(BaseModel):
    """A staged Care_Gaps row destined to become a FHIR Task on approval."""

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["pending_task"] = "pending_task"
    schema_version: Literal["1.0"] = "1.0"
    patient_id: str
    document_reference_id: str
    measure: TextField
    measure_ref: Optional[TextField] = None
    status: Literal["UP TO DATE", "OVERDUE", "DUE_SOON", "NOT_DUE"]
    last_done: Optional[date] = None
    due_date: Optional[date] = None
    notes: Optional[TextField] = None
    staged_at: datetime


# --------------------------------------------------------------------------- #
# WorkbookExtraction (Phase 3 Item 2 — discriminated multi-extraction wrapper)
# --------------------------------------------------------------------------- #
#
# Design: Operator-approved Option B — a single discriminated wrapper kind
# (``"workbook"``) that embeds the heterogeneous outputs of an XLSX parse
# (zero-or-one ``IntakeForm``, zero-or-many ``LabReport`` per date column,
# zero-or-many ``PendingTask`` per Care_Gaps row).
#
# Why a wrapper instead of one-of (pick one)? An XLSX is intrinsically
# multi-document — every workbook simultaneously carries demographic context,
# multiple lab snapshots in time, and pending-task rows. Picking a single
# ``expected_kind`` would force the eval framework to grade only one of those
# lanes per fixture; a wrapper preserves the parser's full output and lets
# the rubric machinery walk every cited item across all lanes.
#
# Rubric integration: ``evals.rubrics_mechanical._iter_cited_items`` recurses
# into ``intake_form`` / ``lab_reports`` / ``pending_tasks`` so every
# ``citation_present`` / ``citation_resolvable`` / ``citation_row_match``
# rule fires on the embedded extractions without per-rubric forking.
#
# Strictly NOT a member of the ``ExtractionResult`` discriminated union —
# the union gates the lab/intake/unknown lanes the W1 critic walks; the
# workbook wrapper lives alongside it as a parallel kind discriminator
# consumed by the eval rubric registry.


class WorkbookExtraction(BaseModel):
    """Multi-extraction wrapper for an XLSX workbook (eval-graded as a unit)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["workbook"] = "workbook"
    schema_version: Literal["1.0"] = "1.0"
    patient_id: str
    document_reference_id: str
    intake_form: Optional[IntakeForm] = None
    lab_reports: List[LabReport] = Field(default_factory=list)
    pending_tasks: List[PendingTask] = Field(default_factory=list)
    classifier_confidence: float
    ocr_confidence_range: Tuple[float, float]
    extracted_at: datetime
