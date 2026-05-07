"""Top-level XLSX parser dispatch (Phase 9 Slice 9.5).

Routes raw XLSX bytes through openpyxl (``data_only=True``,
``read_only=True``), rejects macro-enabled and merged-cell workbooks at
dispatch, and runs the four sheet importers
(:mod:`parsers.xlsx.sheets`).

Output: :class:`parsers.xlsx.types.ParsedWorkbook` aggregating an
optional :class:`IntakeForm`, zero-or-more :class:`LabReport`, and
zero-or-more :class:`PendingTask`.

Failure modes:

* Macro-enabled archive (``xl/vbaProject.bin`` present) →
  :class:`XlsxMacroRejected`.
* openpyxl cannot open the bytes → :class:`XlsxMalformedError`.
* Any of the four known sheets carries merged-cell ranges →
  :class:`XlsxMergedCellsRejected`.
* All four known sheets absent → :class:`XlsxMalformedError`.
* Individual sheet absent → soft-warn carried on
  ``ParsedWorkbook.warnings``.

Metrics: ``agent_xlsx_parse_total{outcome}`` and
``agent_xlsx_parse_duration_seconds{outcome}``. The dispatcher also
emits a structured ``xlsx_parse_completed`` log event with sheet/row
counts (no values, no PHI).
"""

from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from extractors.schemas import IntakeForm

from .exceptions import (
    XlsxMacroRejected,
    XlsxMalformedError,
    XlsxMergedCellsRejected,
)
from .sheets.care_gaps import parse_care_gaps_sheet
from .sheets.labs_trend import parse_labs_trend_sheet
from .sheets.medications import parse_medications_sheet
from .sheets.patient import parse_patient_sheet
from .types import ParsedWorkbook

logger = logging.getLogger(__name__)


_KNOWN_SHEETS = ("Patient", "Medications", "Labs_Trend", "Care_Gaps")


def _import_metrics():
    """Late-binding metrics import — see ``parsers.hl7.dispatch._import_metrics``
    for the rationale (importlinter forbids ``parsers.xlsx -> agent``)."""
    try:
        from ._metrics import (  # noqa: WPS433
            agent_xlsx_parse_duration_seconds,
            agent_xlsx_parse_total,
            agent_xlsx_rows_extracted_total,
        )

        return (
            agent_xlsx_parse_total,
            agent_xlsx_parse_duration_seconds,
            agent_xlsx_rows_extracted_total,
        )
    except Exception:  # noqa: BLE001
        return None, None, None


def _detect_macro(raw: bytes) -> bool:
    """Return True when the XLSX zip archive contains ``xl/vbaProject.bin``."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if name.lower() == "xl/vbaproject.bin":
                    return True
    except zipfile.BadZipFile:
        return False
    return False


def _find_sheet(wb, target: str):
    """Case-insensitive sheet lookup."""
    target_l = target.strip().lower()
    for name in wb.sheetnames:
        if str(name).strip().lower() == target_l:
            return wb[name]
    return None


def _collect_rows(ws) -> List[Tuple[object, ...]]:
    return [tuple(row) for row in ws.iter_rows(values_only=True)]


def _has_merged_cells(ws) -> bool:
    """openpyxl's read-only mode does not populate merged_cells, so the
    caller must use a non-read-only loader for this gate. We do that via
    a second shallow open below."""
    ranges = getattr(ws.merged_cells, "ranges", ())
    try:
        return bool(list(ranges))
    except TypeError:
        return False


def parse_xlsx(
    raw: bytes,
    *,
    document_reference_id: str,
    patient_id: str,
) -> ParsedWorkbook:
    """Parse an XLSX byte stream into a :class:`ParsedWorkbook`.

    Args:
        raw: workbook bytes.
        document_reference_id: DocumentReference id this workbook is
            attached to (carried on every Citation).
        patient_id: patient identifier (carried on the IntakeForm and
            every LabReport).

    Raises:
        XlsxMacroRejected: macro-enabled .xlsm.
        XlsxMergedCellsRejected: any known sheet contains merged cells.
        XlsxMalformedError: openpyxl cannot open the bytes, or all four
            known sheets are absent.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("parse_xlsx expects bytes")

    counter, histogram, rows_counter = _import_metrics()
    started = time.perf_counter()
    outcome_label = "ok"

    try:
        if _detect_macro(bytes(raw)):
            outcome_label = "macro_rejected"
            raise XlsxMacroRejected()

        try:
            import openpyxl  # noqa: WPS433
        except ImportError as exc:
            outcome_label = "malformed"
            raise XlsxMalformedError("xlsx_library_unavailable") from exc

        # Non-read-only first to gate merged-cell ranges (read-only mode
        # does not populate worksheet.merged_cells).
        try:
            full_wb = openpyxl.load_workbook(
                io.BytesIO(bytes(raw)),
                data_only=True,
                read_only=False,
            )
        except Exception as exc:  # noqa: BLE001
            outcome_label = "malformed"
            raise XlsxMalformedError("xlsx_open_failed") from exc

        try:
            present_sheets: List[str] = []
            for canonical in _KNOWN_SHEETS:
                ws = _find_sheet(full_wb, canonical)
                if ws is None:
                    continue
                if _has_merged_cells(ws):
                    outcome_label = "merged_cells_rejected"
                    raise XlsxMergedCellsRejected(sheet=canonical)
                present_sheets.append(canonical)
        finally:
            try:
                full_wb.close()
            except Exception:  # noqa: BLE001
                pass

        if not present_sheets:
            outcome_label = "malformed"
            raise XlsxMalformedError(
                "xlsx_no_known_sheets",
                detail="none of Patient/Medications/Labs_Trend/Care_Gaps present",
            )

        # Re-open in read-only mode for the actual data pass.
        try:
            wb = openpyxl.load_workbook(
                io.BytesIO(bytes(raw)),
                data_only=True,
                read_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            outcome_label = "malformed"
            raise XlsxMalformedError("xlsx_open_failed") from exc

        warnings: List[str] = []
        intake_form: Optional[IntakeForm] = None
        lab_reports = []
        pending_tasks = []
        sheet_row_counts = {sheet: 0 for sheet in _KNOWN_SHEETS}

        try:
            extracted_at = datetime.now(timezone.utc)

            patient_ws = _find_sheet(wb, "Patient")
            medications_ws = _find_sheet(wb, "Medications")
            labs_ws = _find_sheet(wb, "Labs_Trend")
            care_gaps_ws = _find_sheet(wb, "Care_Gaps")

            patient_rows = _collect_rows(patient_ws) if patient_ws is not None else []
            medications_rows = _collect_rows(medications_ws) if medications_ws is not None else []
            labs_rows = _collect_rows(labs_ws) if labs_ws is not None else []
            care_gaps_rows = _collect_rows(care_gaps_ws) if care_gaps_ws is not None else []

            sheet_row_counts["Patient"] = max(0, len(patient_rows) - 1) if patient_rows else 0
            sheet_row_counts["Medications"] = max(0, len(medications_rows) - 1) if medications_rows else 0
            sheet_row_counts["Labs_Trend"] = max(0, len(labs_rows) - 1) if labs_rows else 0
            sheet_row_counts["Care_Gaps"] = max(0, len(care_gaps_rows) - 1) if care_gaps_rows else 0

            for canonical in _KNOWN_SHEETS:
                if _find_sheet(wb, canonical) is None:
                    warnings.append(f"xlsx_sheet_missing:{canonical}")

            demographics = None
            allergies = []
            if patient_rows:
                demographics, allergies = parse_patient_sheet(
                    patient_rows,
                    document_reference_id=document_reference_id,
                )
            current_meds = []
            if medications_rows:
                current_meds = parse_medications_sheet(
                    medications_rows,
                    document_reference_id=document_reference_id,
                )

            if demographics is not None or current_meds or allergies:
                intake_form = IntakeForm(
                    patient_id=patient_id,
                    document_reference_id=document_reference_id,
                    demographics=demographics,
                    current_medications=current_meds,
                    allergies=allergies,
                    classifier_confidence=1.0,
                    ocr_confidence_range=(1.0, 1.0),
                    extracted_at=extracted_at,
                )

            if labs_rows:
                lab_reports = parse_labs_trend_sheet(
                    labs_rows,
                    patient_id=patient_id,
                    document_reference_id=document_reference_id,
                    extracted_at=extracted_at,
                )

            if care_gaps_rows:
                pending_tasks = parse_care_gaps_sheet(
                    care_gaps_rows,
                    patient_id=patient_id,
                    document_reference_id=document_reference_id,
                    staged_at=extracted_at,
                )
        finally:
            try:
                wb.close()
            except Exception:  # noqa: BLE001
                pass

        duration_s = time.perf_counter() - started
        if counter is not None:
            counter.labels(outcome=outcome_label).inc()
        if histogram is not None:
            histogram.labels(outcome=outcome_label).observe(duration_s)
        if rows_counter is not None:
            for sheet, n in sheet_row_counts.items():
                if n:
                    rows_counter.labels(sheet=sheet).inc(n)

        logger.info(
            "xlsx_parse_completed",
            extra={
                "document_reference_id": document_reference_id,
                "duration_ms": round(duration_s * 1000.0, 2),
                "outcome": outcome_label,
                "sheets_present": present_sheets,
                "patient_rows": sheet_row_counts["Patient"],
                "medications_rows": sheet_row_counts["Medications"],
                "labs_trend_rows": sheet_row_counts["Labs_Trend"],
                "care_gaps_rows": sheet_row_counts["Care_Gaps"],
                "lab_reports_emitted": len(lab_reports),
                "pending_tasks_emitted": len(pending_tasks),
                "warnings": warnings,
            },
        )

        return ParsedWorkbook(
            intake_form=intake_form,
            lab_reports=lab_reports,
            pending_tasks=pending_tasks,
            warnings=warnings,
        )
    except (XlsxMacroRejected, XlsxMergedCellsRejected, XlsxMalformedError) as exc:
        duration_s = time.perf_counter() - started
        if counter is not None:
            counter.labels(outcome=outcome_label).inc()
        if histogram is not None:
            histogram.labels(outcome=outcome_label).observe(duration_s)
        logger.warning(
            "xlsx_parse_failed",
            extra={
                "document_reference_id": document_reference_id,
                "outcome": outcome_label,
                "code": getattr(exc, "code", None),
                "duration_ms": round(duration_s * 1000.0, 2),
            },
        )
        raise


# --------------------------------------------------------------------------- #
# parse_and_stage helper (consumer convenience)
# --------------------------------------------------------------------------- #


async def parse_and_stage(
    raw: bytes,
    *,
    document_reference_id: str,
    patient_id: str,
    file_batch_id: str,
    request_id: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> ParsedWorkbook:
    """Parse the workbook and stage every sub-result via ``observations.writer``.

    Stages:

    * Each :class:`LabValue` from each :class:`LabReport` →
      :func:`observations.writer.stage_observation` (source_format=``xlsx``).
    * Each :class:`AllergyItem` on the IntakeForm →
      :func:`observations.writer.stage_allergy` (will fail at write with
      ``allergy_writer_unavailable`` per Slice 9.3).
    * Each :class:`PendingTask` →
      :func:`observations.writer.stage_task` (will fail at write with
      ``task_writer_unavailable`` per Slice 9.3).

    The dispatcher (Slice 9.10) is the production caller; this helper is
    here so end-to-end tests can exercise the staging round-trip without
    re-implementing the same fan-out.
    """
    # Local imports keep the parser pure when callers want bytes-in /
    # ParsedWorkbook-out without dragging the staging stack along.
    from observations import writer as _writer  # noqa: WPS433

    parsed = parse_xlsx(
        raw,
        document_reference_id=document_reference_id,
        patient_id=patient_id,
    )
    # PHP custom-observation upsert enforces ``r"^copilot-\d+-..."``; the
    # caller passes a document_reference_id like ``"local:UUID"`` on the
    # local-disk fallback path, which would fail the pattern. Mirror the
    # legacy PDF/PNG branch's derivation (main.py ~line 2489): trailing
    # integer if present, otherwise a hash-derived numeric id.
    _trail = re.search(r"(\d+)$", document_reference_id or "")
    _doc_id_numeric = (
        _trail.group(1)
        if _trail
        else str(abs(hash(document_reference_id or "")) % (10**9))
    )
    if parsed.intake_form is not None:
        for allergy in parsed.intake_form.allergies:
            anchor = allergy.citations[0] if allergy.citations else None
            await _writer.stage_allergy(
                document_id=_doc_id_numeric,
                patient_id=patient_id,
                file_batch_id=file_batch_id,
                document_reference_id=document_reference_id,
                substance=allergy.substance,
                reaction=allergy.reaction,
                locator=anchor.field_or_chunk_id if anchor else None,
                source_format="xlsx",
                request_id=request_id,
                provider_id=provider_id,
            )
    for report in parsed.lab_reports:
        for lab_value in report.values:
            anchor = lab_value.citations[0] if lab_value.citations else None
            await _writer.stage_observation(
                document_id=_doc_id_numeric,
                patient_id=patient_id,
                lab_value=lab_value,
                file_batch_id=file_batch_id,
                document_reference_id=document_reference_id,
                locator=anchor.field_or_chunk_id if anchor else None,
                source_format="xlsx",
                request_id=request_id,
                provider_id=provider_id,
            )
    for task in parsed.pending_tasks:
        anchor = task.measure.citations[0] if task.measure.citations else None
        await _writer.stage_task(
            document_id=_doc_id_numeric,
            patient_id=patient_id,
            file_batch_id=file_batch_id,
            document_reference_id=document_reference_id,
            measure=task.measure.value,
            status=task.status,
            description=task.notes.value if task.notes else None,
            locator=anchor.field_or_chunk_id if anchor else None,
            source_format="xlsx",
            request_id=request_id,
            provider_id=provider_id,
        )
    return parsed
