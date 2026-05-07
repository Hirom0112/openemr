"""Patient-sheet importer (Phase 9 Slice 9.5).

Vertical key/value layout:

    ``Field | Value``
    ``Name  | Margaret Chen``
    ``DOB   | 1968-03-12``
    ``...``

We tolerate header drift via a small alias table (``Mailing_Address``,
``Home_Address`` collapse to ``Address``; ``Date_of_Birth`` to ``DOB``;
etc.). The Allergies row is special-cased: ``"NKDA"`` (or the empty
string) collapses to no allergy items; otherwise the value is split on
common separators and each token becomes one :class:`AllergyItem`.

Locator grammar: ``sheet=Patient|row=N|col=Value`` (the value column has
a stable label, ``Value``; the row number is 1-based to match the
openpyxl row index a clinician sees in Excel).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from extractors.schemas import (
    AllergyItem,
    Citation,
    Demographics,
    TextField,
)


# --------------------------------------------------------------------------- #
# Alias tables
# --------------------------------------------------------------------------- #

_FIELD_ALIASES: Dict[str, str] = {
    # Canonical demographic keys. Keys are normalised (lower, no spaces / dashes).
    "name": "name",
    "patientname": "name",
    "fullname": "name",
    "dob": "dob",
    "dateofbirth": "dob",
    "birthdate": "dob",
    "sex": "sex",
    "gender": "sex",
    "mrn": "mrn",
    "patientid": "mrn",
    "medicalrecordnumber": "mrn",
    "address": "address",
    "mailingaddress": "address",
    "homeaddress": "address",
    "streetaddress": "address",
    "allergies": "allergies",
    "allergy": "allergies",
    "knownallergies": "allergies",
}


def _normalise_field(raw: object) -> str:
    if raw is None:
        return ""
    return str(raw).strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _build_locator(row: int, col_label: str = "Value") -> str:
    return f"sheet=Patient|row={row}|col={col_label}"


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
                page_or_section="Patient",
                field_or_chunk_id=locator,
                quote_or_value=value,
            )
        ],
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def parse_patient_sheet(
    rows: List[Tuple[object, ...]],
    *,
    document_reference_id: str,
) -> Tuple[Optional[Demographics], List[AllergyItem]]:
    """Parse the ``Patient`` sheet rows into ``(Demographics, allergies)``.

    Returns ``(None, [])`` when the sheet is empty or has no header.
    """
    if not rows:
        return None, []

    # Detect/skip the header row (Field | Value). Anything else gets treated
    # as data with a synthetic header.
    data_start = 0
    if len(rows) >= 1:
        first_field = _normalise_field(rows[0][0] if len(rows[0]) > 0 else None)
        if first_field == "field":
            data_start = 1

    name: Optional[TextField] = None
    dob: Optional[TextField] = None
    sex: Optional[TextField] = None
    mrn: Optional[TextField] = None
    address: Optional[TextField] = None
    allergies: List[AllergyItem] = []

    for idx, row in enumerate(rows[data_start:], start=data_start + 1):
        if len(row) < 2:
            continue
        key_norm = _normalise_field(row[0])
        canonical = _FIELD_ALIASES.get(key_norm)
        if canonical is None:
            # Out-of-schema field — drop silently. Audit-only Citations on
            # arbitrary demographic rows are out of scope for v1.
            continue
        raw_value = row[1]
        if raw_value is None:
            continue
        value_str = str(raw_value).strip()
        if not value_str:
            continue
        locator = _build_locator(idx)
        if canonical == "allergies":
            for item in _parse_allergies(
                value_str,
                document_reference_id=document_reference_id,
                locator=locator,
            ):
                allergies.append(item)
            continue
        text_field = _make_text_field(
            value_str,
            document_reference_id=document_reference_id,
            locator=locator,
        )
        if canonical == "name":
            name = text_field
        elif canonical == "dob":
            dob = text_field
        elif canonical == "sex":
            sex = text_field
        elif canonical == "mrn":
            mrn = text_field
        elif canonical == "address":
            address = text_field

    if name is None and dob is None and sex is None and mrn is None and address is None:
        demographics: Optional[Demographics] = None
    else:
        demographics = Demographics(
            name=name,
            dob=dob,
            sex=sex,
            mrn=mrn,
            address=address,
        )
    return demographics, allergies


# --------------------------------------------------------------------------- #
# Allergies parser
# --------------------------------------------------------------------------- #


_NKDA_TOKENS = {"nkda", "nka", "none", "no known allergies", "no known drug allergies"}


def _parse_allergies(
    value: str,
    *,
    document_reference_id: str,
    locator: str,
) -> List[AllergyItem]:
    """Split a free-text allergies field into ``AllergyItem`` rows.

    NKDA (and equivalents) collapses to no items. Otherwise we split on
    ``;`` / ``,`` / ``|``, drop empty tokens, and produce one
    ``AllergyItem`` per token (no reaction parsing in v1).
    """
    if value.strip().lower() in _NKDA_TOKENS:
        return []
    sentinel = chr(0x1F)
    buffer = value
    for sep in (";", "|", ","):
        buffer = buffer.replace(sep, sentinel)
    tokens: List[str] = []
    for tok in buffer.split(sentinel):
        cleaned = tok.strip()
        if cleaned:
            tokens.append(cleaned)
    out: List[AllergyItem] = []
    for tok in tokens:
        out.append(
            AllergyItem(
                substance=tok,
                citations=[
                    Citation(
                        source_type="document",
                        source_id=document_reference_id,
                        page_or_section="Patient",
                        field_or_chunk_id=locator,
                        quote_or_value=tok,
                    )
                ],
            )
        )
    return out
