"""Care_Gaps-sheet importer (Phase 9 Slice 9.5).

Tabular layout, one row per care-gap measure:

    ``Measure | HEDIS_or_USPSTF_ref | Status | Last_Done | Due_Date | Notes``

Each row stages as a :class:`PendingTask` (Phase 9 Slice 9.1 schema). The
``Status`` column maps to the schema ``Literal['UP TO DATE', 'OVERDUE',
'DUE_SOON', 'NOT_DUE']``. Unrecognised statuses raise — the schema is
strict on that field so we'd fail at construction anyway.

Locator anchor for citations is the ``Measure`` cell:
``sheet=Care_Gaps|row=N|col=Measure``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from extractors.schemas import Citation, PendingTask, TextField


_HEADER_ALIASES: Dict[str, str] = {
    "measure": "measure",
    "measurename": "measure",
    "hedisorhuspstfref": "measure_ref",
    "hedisorus pstfref": "measure_ref",  # tolerant (space-stripping handles it)
    "hedisorus pstf": "measure_ref",
    "hedisorus pstf ref": "measure_ref",
    "ref": "measure_ref",
    "reference": "measure_ref",
    "hedisrefr": "measure_ref",
    "hedisuspstfref": "measure_ref",
    "status": "status",
    "lastdone": "last_done",
    "duedate": "due_date",
    "notes": "notes",
    "comments": "notes",
}


_STATUS_VALUES = {"UP TO DATE", "OVERDUE", "DUE_SOON", "NOT_DUE"}


def _normalise(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _build_locator(row: int, col_label: str) -> str:
    return f"sheet=Care_Gaps|row={row}|col={col_label}"


def _coerce_date(value: object) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text == "—" or text == "-":
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None


def parse_care_gaps_sheet(
    rows: List[Tuple[object, ...]],
    *,
    patient_id: str,
    document_reference_id: str,
    staged_at: Optional[datetime] = None,
) -> List[PendingTask]:
    if staged_at is None:
        staged_at = datetime.now(timezone.utc)
    if not rows:
        return []

    header = rows[0]
    header_map: Dict[str, int] = {}
    for idx, cell in enumerate(header):
        canonical = _HEADER_ALIASES.get(_normalise(cell))
        if canonical is not None:
            header_map[canonical] = idx
    if "measure" not in header_map or "status" not in header_map:
        return []

    out: List[PendingTask] = []
    for row_idx, row in enumerate(rows[1:], start=2):
        if not any(cell is not None and str(cell).strip() != "" for cell in row):
            continue
        measure_raw = _cell(row, header_map.get("measure"))
        if not measure_raw:
            continue
        status_raw = _cell(row, header_map.get("status"))
        if not status_raw:
            continue
        status = status_raw.strip().upper().replace("-", "_")
        # Normalise common variants.
        if status == "UPTODATE":
            status = "UP TO DATE"
        if status not in _STATUS_VALUES:
            # Unknown status — soft skip rather than raise; PendingTask is
            # strict on this Literal and we'd fail construction otherwise.
            continue

        measure_anchor = _build_locator(row_idx, "Measure")
        measure_field = TextField(
            value=measure_raw,
            citations=[
                Citation(
                    source_type="document",
                    source_id=document_reference_id,
                    page_or_section="Care_Gaps",
                    field_or_chunk_id=measure_anchor,
                    quote_or_value=measure_raw,
                )
            ],
        )

        ref_raw = _cell(row, header_map.get("measure_ref"))
        measure_ref = (
            TextField(
                value=ref_raw,
                citations=[
                    Citation(
                        source_type="document",
                        source_id=document_reference_id,
                        page_or_section="Care_Gaps",
                        field_or_chunk_id=_build_locator(row_idx, "HEDIS_or_USPSTF_ref"),
                        quote_or_value=ref_raw,
                    )
                ],
            )
            if ref_raw
            else None
        )

        notes_raw = _cell(row, header_map.get("notes"))
        notes_field = (
            TextField(
                value=notes_raw,
                citations=[
                    Citation(
                        source_type="document",
                        source_id=document_reference_id,
                        page_or_section="Care_Gaps",
                        field_or_chunk_id=_build_locator(row_idx, "Notes"),
                        quote_or_value=notes_raw,
                    )
                ],
            )
            if notes_raw
            else None
        )

        last_done_raw = _raw_cell(row, header_map.get("last_done"))
        due_date_raw = _raw_cell(row, header_map.get("due_date"))

        out.append(
            PendingTask(
                patient_id=patient_id,
                document_reference_id=document_reference_id,
                measure=measure_field,
                measure_ref=measure_ref,
                status=status,  # type: ignore[arg-type]
                last_done=_coerce_date(last_done_raw),
                due_date=_coerce_date(due_date_raw),
                notes=notes_field,
                staged_at=staged_at,
            )
        )
    return out


def _cell(row: Tuple[object, ...], idx: Optional[int]) -> Optional[str]:
    if idx is None or idx >= len(row):
        return None
    raw = row[idx]
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _raw_cell(row: Tuple[object, ...], idx: Optional[int]) -> object:
    if idx is None or idx >= len(row):
        return None
    return row[idx]
