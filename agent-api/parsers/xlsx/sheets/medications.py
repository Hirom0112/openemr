"""Medications-sheet importer (Phase 9 Slice 9.5).

Tabular layout, one row per active medication:

    ``Brand | Generic | Strength | Route | Sig | Indication | Start_Date |
      Last_Filled | Refills_Remaining | Prescriber``

Mapping:

* ``name`` ← ``"{generic} ({brand})"`` when both are present, else
  whichever is supplied.
* ``dose`` ← concatenation of ``Strength``, ``Route``, ``Sig`` joined
  with `` | `` (and any missing pieces dropped).
* ``indication``, ``prescriber``, ``last_filled``, ``refills_remaining``
  populate the optional Slice 9.1 schema slots.

Per-row citations point at the canonical "name" cell — column ``Generic``
when present, else ``Brand``. Locator: ``sheet=Medications|row=N|col=Generic``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from extractors.schemas import Citation, MedicationItem, TextField


# Header alias table — keys are normalised (lower, no spaces / dashes).
_HEADER_ALIASES: Dict[str, str] = {
    "brand": "brand",
    "brandname": "brand",
    "tradename": "brand",
    "generic": "generic",
    "genericname": "generic",
    "drugname": "generic",
    "strength": "strength",
    "dose": "strength",
    "route": "route",
    "sig": "sig",
    "instructions": "sig",
    "indication": "indication",
    "reason": "indication",
    "startdate": "start_date",
    "lastfilled": "last_filled",
    "refillsremaining": "refills_remaining",
    "refills": "refills_remaining",
    "prescriber": "prescriber",
    "provider": "prescriber",
}


def _normalise(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _coerce_date(value: object) -> Optional[date]:
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
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None


def _coerce_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            f = float(text)
            if f.is_integer():
                return int(f)
        except ValueError:
            pass
    return None


def _build_locator(row: int, col_label: str) -> str:
    return f"sheet=Medications|row={row}|col={col_label}"


def parse_medications_sheet(
    rows: List[Tuple[object, ...]],
    *,
    document_reference_id: str,
) -> List[MedicationItem]:
    """Parse the ``Medications`` sheet rows into ``MedicationItem`` list.

    Returns ``[]`` when no header row maps to a known column.
    """
    if not rows:
        return []
    header = rows[0] if rows else ()
    header_map: Dict[str, int] = {}
    for idx, cell in enumerate(header):
        canonical = _HEADER_ALIASES.get(_normalise(cell))
        if canonical is not None:
            header_map[canonical] = idx
    # If neither brand nor generic columns are present we cannot identify
    # medications by name — bail out.
    if "brand" not in header_map and "generic" not in header_map:
        return []

    out: List[MedicationItem] = []
    for row_idx, row in enumerate(rows[1:], start=2):
        if not any(cell is not None and str(cell).strip() != "" for cell in row):
            continue
        brand = _cell(row, header_map.get("brand"))
        generic = _cell(row, header_map.get("generic"))
        # Compose canonical name.
        name = _compose_name(brand, generic)
        if not name:
            continue

        strength = _cell(row, header_map.get("strength"))
        route = _cell(row, header_map.get("route"))
        sig = _cell(row, header_map.get("sig"))
        dose_pieces = [p for p in (strength, route, sig) if p]
        dose = " | ".join(dose_pieces) if dose_pieces else None

        # Anchor citation: the name column.
        anchor_label = "Generic" if "generic" in header_map else "Brand"
        anchor_locator = _build_locator(row_idx, anchor_label)
        citations = [
            Citation(
                source_type="document",
                source_id=document_reference_id,
                page_or_section="Medications",
                field_or_chunk_id=anchor_locator,
                quote_or_value=name,
            )
        ]

        indication_raw = _cell(row, header_map.get("indication"))
        indication = _make_text_field(
            indication_raw,
            document_reference_id=document_reference_id,
            locator=_build_locator(row_idx, "Indication"),
        ) if indication_raw else None

        prescriber_raw = _cell(row, header_map.get("prescriber"))
        prescriber = _make_text_field(
            prescriber_raw,
            document_reference_id=document_reference_id,
            locator=_build_locator(row_idx, "Prescriber"),
        ) if prescriber_raw else None

        last_filled_raw = _raw_cell(row, header_map.get("last_filled"))
        last_filled = _coerce_date(last_filled_raw)

        refills_raw = _raw_cell(row, header_map.get("refills_remaining"))
        refills = _coerce_int(refills_raw)

        out.append(
            MedicationItem(
                name=name,
                dose=dose,
                citations=citations,
                indication=indication,
                prescriber=prescriber,
                last_filled=last_filled,
                refills_remaining=refills,
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


def _compose_name(brand: Optional[str], generic: Optional[str]) -> str:
    if generic and brand:
        return f"{generic} ({brand})"
    return generic or brand or ""


def _make_text_field(
    value: str,
    *,
    document_reference_id: str,
    locator: str,
) -> TextField:
    return TextField(
        value=value,
        citations=[
            Citation(
                source_type="document",
                source_id=document_reference_id,
                page_or_section="Medications",
                field_or_chunk_id=locator,
                quote_or_value=value,
            )
        ],
    )
