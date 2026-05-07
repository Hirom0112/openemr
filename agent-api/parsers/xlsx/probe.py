"""Pre-dispatch identity probe for XLSX workbooks (Phase 9 Slice 9.5).

Reads only the ``Patient`` sheet via ``openpyxl.load_workbook`` in
read-only mode. Returns ``CandidateHints`` with whatever MRN / Name /
DOB the sheet exposes; ``None`` when the workbook has no Patient sheet
or cannot be opened.

This deliberately performs the lightest possible openpyxl pass — we do
not iterate the other three sheets. The full structural parse happens
later in :func:`parsers.xlsx.parser.parse_xlsx`.

Slice 9.2's ``demographics/resolver.py`` plugs this in via its own
``probe_xlsx`` shim; until then the resolver returns ``None`` for XLSX
inputs and the caller treats that as ``NO_IDENTITY_HINTS``.
"""

from __future__ import annotations

import io
from typing import Optional

from .types import CandidateHints
from .sheets.patient import _normalise_field, _FIELD_ALIASES


def probe_identity(raw: bytes) -> Optional[CandidateHints]:
    """Light Patient-sheet read returning identity hints.

    Returns ``None`` when:

    * The bytes cannot be opened by openpyxl (zip bomb, truncated, etc).
    * No ``Patient`` sheet exists.

    Always returns a :class:`CandidateHints` (with possibly-None members)
    when the Patient sheet is present — symmetric with the HL7 v2 probe
    contract.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("probe_identity expects bytes")

    try:
        import openpyxl  # noqa: WPS433 — late import keeps lint sandboxes happy
    except ImportError:
        return None

    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(bytes(raw)),
            data_only=True,
            read_only=True,
        )
    except Exception:  # noqa: BLE001 — openpyxl raises a wide tree
        return None

    try:
        sheet_name = _find_patient_sheet(wb.sheetnames)
        if sheet_name is None:
            return None
        ws = wb[sheet_name]
        mrn: Optional[str] = None
        name_family: Optional[str] = None
        name_given: Optional[str] = None
        dob: Optional[str] = None
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 2:
                continue
            key = _normalise_field(row[0])
            canonical = _FIELD_ALIASES.get(key)
            if canonical is None:
                continue
            value = row[1]
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            if canonical == "mrn" and mrn is None:
                mrn = text
            elif canonical == "name" and name_given is None and name_family is None:
                # Best-effort split: "Given Family" or "Family, Given".
                if "," in text:
                    family_part, _, given_part = text.partition(",")
                    name_family = family_part.strip() or None
                    name_given = given_part.strip() or None
                else:
                    parts = text.split(None, 1)
                    if len(parts) == 2:
                        name_given, name_family = parts[0].strip() or None, parts[1].strip() or None
                    else:
                        name_family = parts[0].strip() or None
            elif canonical == "dob" and dob is None:
                dob = text
        if mrn is None and name_family is None and name_given is None and dob is None:
            return None
        return CandidateHints(
            mrn=mrn,
            name_family=name_family,
            name_given=name_given,
            dob=dob,
        )
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass


def _find_patient_sheet(sheetnames) -> Optional[str]:
    for name in sheetnames:
        if str(name).strip().lower() == "patient":
            return name
    return None
