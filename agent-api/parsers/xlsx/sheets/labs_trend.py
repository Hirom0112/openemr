"""Labs_Trend-sheet importer (Phase 9 Slice 9.5).

Wide layout, one row per analyte, one column per collection date:

    ``Test | LOINC | Units | Reference_Range | YYYY-MM-DD | YYYY-MM-DD | ...``

We unpivot to N :class:`LabReport` instances — one per date column. Each
LabReport carries one :class:`LabValue` per analyte that has a non-empty
cell on that date.

Locator grammar: ``sheet=Labs_Trend|row=N|col=YYYY-MM-DD`` (header text
preferred — the date column header IS the locator coordinate).

Reference-range driven ``abnormal_flag``: simple inequality and dashed
ranges are supported. The ``(DM)`` / ``(F)`` / ``(M)`` suffixes are
stripped before parsing — patient-level context (sex, DM status) is not
re-evaluated here; the suffix conveys the *clinical* lower bound the
range refers to but not enough to override the numeric threshold. v1
treats the threshold value as authoritative.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from extractors.schemas import Citation, LabReport, LabValue

logger = logging.getLogger(__name__)


# Header alias table — fixed columns only. Date columns are detected
# dynamically.
_HEADER_ALIASES: Dict[str, str] = {
    "test": "test",
    "testname": "test",
    "analyte": "test",
    "loinc": "loinc",
    "loinccode": "loinc",
    "units": "units",
    "unit": "units",
    "referencerange": "reference_range",
    "refrange": "reference_range",
    "range": "reference_range",
}


def _normalise(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _coerce_date(value: object) -> Optional[date]:
    """Best-effort parse of a date column header.

    Excel sometimes presents date headers as ``datetime`` objects (when
    the cell carries the ISO string under a date format) or as strings.
    Numeric serials should NOT appear in column headers in practice; we
    don't attempt the 1900-base conversion here.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    # Permit slash-separated forms.
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _build_locator(row: int, date_label: str) -> str:
    return f"sheet=Labs_Trend|row={row}|col={date_label}"


def parse_labs_trend_sheet(
    rows: List[Tuple[object, ...]],
    *,
    patient_id: str,
    document_reference_id: str,
    extracted_at: Optional[datetime] = None,
) -> List[LabReport]:
    """Unpivot the Labs_Trend sheet into one LabReport per date column."""
    if extracted_at is None:
        extracted_at = datetime.now(timezone.utc)
    if not rows:
        return []

    header = rows[0]
    fixed_cols: Dict[str, int] = {}
    date_cols: List[Tuple[int, date, str]] = []  # (col_index, date, header_text)
    for idx, cell in enumerate(header):
        norm = _normalise(cell)
        canonical = _HEADER_ALIASES.get(norm)
        if canonical is not None:
            fixed_cols[canonical] = idx
            continue
        d = _coerce_date(cell)
        if d is not None:
            header_text = str(cell).strip() if not isinstance(cell, (date, datetime)) else d.isoformat()
            # Prefer ISO form for the locator regardless of input form.
            date_cols.append((idx, d, d.isoformat()))

    if not date_cols or "test" not in fixed_cols:
        return []

    # Pre-extract per-row metadata.
    per_date_values: Dict[date, List[LabValue]] = {d: [] for _, d, _ in date_cols}
    per_date_locator_label: Dict[date, str] = {d: lbl for _, d, lbl in date_cols}

    for row_idx, row in enumerate(rows[1:], start=2):
        if not any(cell is not None and str(cell).strip() != "" for cell in row):
            continue
        test_name_raw = _cell(row, fixed_cols.get("test"))
        if not test_name_raw:
            continue
        test_name = test_name_raw
        normalized_test_name = test_name.lower()
        unit_raw = _cell(row, fixed_cols.get("units"))
        ref_range_raw = _cell(row, fixed_cols.get("reference_range"))

        for col_idx, dt, date_label in date_cols:
            if col_idx >= len(row):
                continue
            raw_val = row[col_idx]
            if raw_val is None:
                continue
            value_str = _format_numeric(raw_val)
            if not value_str:
                continue
            locator = _build_locator(row_idx, date_label)
            citations = [
                Citation(
                    source_type="document",
                    source_id=document_reference_id,
                    page_or_section="Labs_Trend",
                    field_or_chunk_id=locator,
                    quote_or_value=value_str,
                )
            ]
            abnormal = _derive_abnormal_flag(value_str, ref_range_raw)
            lab_value = LabValue(
                test_name=test_name,
                normalized_test_name=normalized_test_name,
                value=value_str,
                unit=unit_raw,
                normalized_unit=unit_raw,
                reference_range=ref_range_raw,
                collection_date=dt,
                abnormal_flag=abnormal,
                citations=citations,
            )
            per_date_values[dt].append(lab_value)

    out: List[LabReport] = []
    for _, dt, _ in date_cols:
        values = per_date_values.get(dt, [])
        if not values:
            continue
        out.append(
            LabReport(
                kind="lab_report",
                schema_version="1.0",
                patient_id=patient_id,
                document_reference_id=document_reference_id,
                values=values,
                classifier_confidence=1.0,
                ocr_confidence_range=(1.0, 1.0),
                extracted_at=extracted_at,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _cell(row: Tuple[object, ...], idx: Optional[int]) -> Optional[str]:
    if idx is None or idx >= len(row):
        return None
    raw = row[idx]
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _format_numeric(raw: object) -> str:
    """Render a numeric cell as a stable string.

    Floats that are whole numbers render without a trailing ``.0`` so
    integer-typed reference ranges (``<200``) compare cleanly. Strings
    pass through after a strip.
    """
    if isinstance(raw, bool):
        return str(int(raw))
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        if raw.is_integer():
            return str(int(raw))
        return f"{raw:g}"
    text = str(raw).strip()
    return text


_RANGE_LT = re.compile(r"^\s*<\s*([\d.]+)")
_RANGE_LE = re.compile(r"^\s*<=\s*([\d.]+)")
_RANGE_GT = re.compile(r"^\s*>\s*([\d.]+)")
_RANGE_GE = re.compile(r"^\s*>=\s*([\d.]+)")
_RANGE_DASH = re.compile(r"^\s*([\d.]+)\s*[-–]\s*([\d.]+)")


def _derive_abnormal_flag(value_str: str, ref_range: Optional[str]):
    """Return a Literal abnormal flag based on the reference range parse.

    Strategy:

    * ``<X``  → ``high`` when value ≥ X, else ``normal``.
    * ``<=X`` → ``high`` when value > X, else ``normal``.
    * ``>X``  → ``low``  when value ≤ X, else ``normal``.
    * ``>=X`` → ``low``  when value < X, else ``normal``.
    * ``A-B`` → ``low`` if < A, ``high`` if > B, else ``normal``.
    * Anything else, or an unparseable numeric value → ``unknown``.
    """
    if not ref_range:
        return "unknown"
    try:
        numeric = float(value_str)
    except ValueError:
        return "unknown"
    # Strip clinical-context suffixes ``(DM)``, ``(F)``, ``(M)`` etc.
    cleaned = re.sub(r"\([^)]*\)", "", ref_range).strip()
    m = _RANGE_LE.match(cleaned)
    if m:
        threshold = float(m.group(1))
        return "high" if numeric > threshold else "normal"
    m = _RANGE_LT.match(cleaned)
    if m:
        threshold = float(m.group(1))
        return "high" if numeric >= threshold else "normal"
    m = _RANGE_GE.match(cleaned)
    if m:
        threshold = float(m.group(1))
        return "low" if numeric < threshold else "normal"
    m = _RANGE_GT.match(cleaned)
    if m:
        threshold = float(m.group(1))
        return "low" if numeric <= threshold else "normal"
    m = _RANGE_DASH.match(cleaned)
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2))
        if numeric < lo:
            return "low"
        if numeric > hi:
            return "high"
        return "normal"
    return "unknown"
