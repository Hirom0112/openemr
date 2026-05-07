"""Types specific to the XLSX parser (Phase 9 Slice 9.5).

``ParsedWorkbook`` is the aggregate return type of :func:`parse_xlsx`. It
bundles the structured-lane outputs of all four sheet importers so the
caller (the ingest dispatcher) can stage each sub-result through the
appropriate ``observations.writer`` entry point in one pass.

``CandidateHints`` mirrors the HL7 probe contract — a frozen dataclass
of optional identity fields used by the (future) caller in
``demographics.resolver`` to score MRN/name/DOB matches before running
the full openpyxl parse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from extractors.schemas import IntakeForm, LabReport, PendingTask


# --------------------------------------------------------------------------- #
# Probe-stage hints (pre-dispatch)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CandidateHints:
    """Lightweight identity hints extracted by a Patient-sheet probe.

    All fields are optional because a sparsely populated Patient sheet
    may yield only some of them; the caller decides what to do with
    partial hints.
    """

    mrn: Optional[str]
    name_family: Optional[str]
    name_given: Optional[str]
    dob: Optional[str]


# --------------------------------------------------------------------------- #
# ParsedWorkbook (top-level XLSX dispatch output)
# --------------------------------------------------------------------------- #


@dataclass
class ParsedWorkbook:
    """Aggregated structured-lane output for a single XLSX workbook.

    Fields:

    * ``intake_form`` — Demographics + medications + allergies extracted
      from the ``Patient`` and ``Medications`` sheets, packaged as the
      existing :class:`IntakeForm` schema. ``None`` when neither sheet
      yielded anything (all four sheets missing → top-level
      ``XlsxMalformedError`` raises before construction).
    * ``lab_reports`` — One :class:`LabReport` per date column of the
      ``Labs_Trend`` sheet (wide→long unpivot). Empty list when the
      sheet is absent or carries no date columns.
    * ``pending_tasks`` — One :class:`PendingTask` per row of
      ``Care_Gaps``. Empty list when the sheet is absent.
    * ``warnings`` — Soft-warn strings for missing sheets, unparseable
      dates, formula cells with stale cache, etc. Carried so the caller
      can surface them in an audit row without raising.
    """

    intake_form: Optional[IntakeForm] = None
    lab_reports: List[LabReport] = field(default_factory=list)
    pending_tasks: List[PendingTask] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
