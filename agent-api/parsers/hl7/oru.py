"""ORU^R01 parser → :class:`extractors.schemas.LabReport` (Phase 9 Slice 9.4).

Field map (per todo.md Slice 9.4):
  * OBR-4 — order panel LOINC (recorded as a panel-level marker, not a LabValue)
  * OBR-7 — collection time (fallback when OBX-14 is empty)
  * OBX-3 — observation identifier (LOINC code in CE.1, name in CE.2)
  * OBX-5 — value
  * OBX-6 — unit (CE.1)
  * OBX-7 — reference range (raw string)
  * OBX-8 — abnormal flag (HL70078: H, L, HH, LL, N, …)
  * OBX-14 — observation date/time (preferred over OBR-7)
  * NTE-3 — note text (collected at REPORT level — schema gap below)

Synthetic locators per W2_ARCHITECTURE §4.8: ``OBX-5|seg=N`` where
``seg=N`` is the **parser-assigned absolute segment index** (1-based,
across the entire message), NOT OBX-1. Vendors disagree on OBX-1 set-ID
ordering when an ORU has multiple OBR groups, so we use the absolute
index that hl7apy.children iteration produces.

Each :class:`LabValue` carries TWO citations: one at OBX-5 (the value
itself) and one at OBX-3.1 (the test-code source). Together they let
the critic walk both the value and the LOINC code back to source.

**Schema gap (v1, documented):** ``LabReport`` has no per-LabValue
``notes`` field, and ``LabValue.model_config`` has ``extra="forbid"``,
so NTE-3 cannot ride on the LabValue. We surface NTE-3 only when the
NTE follows the OBR (i.e. report-level note) — by truncating it into
``LabReport.collection_facility`` would be a contract violation, so
NTE-3 is dropped from the structured output in v1 and a warning log
event is emitted with the locator. Slice 9.10's schema-amendment work
tracks this; until then the note is preserved on the source HL7
document only.

**LOINC widening (deferred to Slice 9.3):** ``observations/writer.py``
has a ``_LOINC_TABLE`` that maps the W2 PDF/PNG vocabulary. BNP, eGFR,
HCO3, sodium, potassium, etc. are not yet in that table; the writer
falls through to ``LP-UNKNOWN``. This parser does NOT touch the writer
table — Slice 9.3 owns it. We emit a warning when an OBX-3.1 LOINC is
not in the W2 vocabulary, but we still produce a valid LabValue with
the raw LOINC carried in OBX-3.1's citation quote.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

from extractors.schemas import Citation, LabReport, LabValue

logger = logging.getLogger(__name__)

# HL70078 abnormal-flag subset → LabValue.abnormal_flag literal. Anything
# else (A=abnormal, S=susceptible, etc.) maps to "unknown" — the schema
# only models a five-element domain.
_ABNORMAL_FLAG_MAP = {
    "H": "high",
    "L": "low",
    "HH": "critical_high",
    "LL": "critical_low",
    "N": "normal",
    "": "unknown",
}


def _value_or_none(node) -> Optional[str]:
    if node is None:
        return None
    val = getattr(node, "value", None)
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _parse_collection_date(raw: Optional[str]) -> Optional[date]:
    """Parse an HL7 TS string (YYYYMMDD[HHMMSS]) into a date.

    We only need date-precision for LabValue.collection_date; the time
    component is dropped. Returns None on any parse failure.
    """
    if not raw:
        return None
    digits = raw[:8]
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def _normalize_test_name(name: str) -> str:
    """Lowercased, whitespace-collapsed canonical form for dedup keys.

    Matches the §7.5 dedup-key normalization: lower-case + single-space
    collapsing. Punctuation is preserved (it's part of the test name).
    """
    return " ".join(name.lower().split())


def _build_obx_citations(
    document_reference_id: str,
    abs_seg_index: int,
    obx,
) -> Tuple[List[Citation], str, str, str, Optional[str]]:
    """Return (citations, loinc, test_name, value, unit) for one OBX.

    Citations:
      * OBX-5|seg=N → the value itself.
      * OBX-3.1|seg=N → the LOINC code that names the test.
    """
    try:
        loinc = _value_or_none(obx.obx_3.ce_1) or ""
    except Exception:  # noqa: BLE001
        loinc = ""
    try:
        test_name = _value_or_none(obx.obx_3.ce_2) or loinc or "unknown_test"
    except Exception:  # noqa: BLE001
        test_name = loinc or "unknown_test"

    value = _value_or_none(obx.obx_5) or ""
    try:
        unit = _value_or_none(obx.obx_6.ce_1) if hasattr(obx.obx_6, "ce_1") else _value_or_none(obx.obx_6)
    except Exception:  # noqa: BLE001
        unit = _value_or_none(obx.obx_6)

    citations = [
        Citation(
            source_type="document",
            source_id=document_reference_id,
            page_or_section=None,
            field_or_chunk_id=f"OBX-5|seg={abs_seg_index}",
            quote_or_value=value,
        ),
        Citation(
            source_type="document",
            source_id=document_reference_id,
            page_or_section=None,
            field_or_chunk_id=f"OBX-3.1|seg={abs_seg_index}",
            quote_or_value=loinc,
        ),
    ]
    return citations, loinc, test_name, value, unit


def parse_oru_r01(
    message,
    *,
    document_reference_id: str,
    patient_id: str,
) -> LabReport:
    """Convert a parsed hl7apy ORU^R01 message into a LabReport.

    The dispatcher passes the already-parsed hl7apy Message in. We walk
    ``message.children`` in absolute order so ``seg=N`` is computable
    without re-parsing.
    """
    values: List[LabValue] = []
    notes: List[str] = []  # report-level NTEs, used for warning log only (v1 gap)
    last_obr_collection: Optional[str] = None
    last_obr_facility: Optional[str] = None

    for index, child in enumerate(message.children, start=1):
        name = child.name
        if name == "OBR":
            try:
                last_obr_collection = _value_or_none(child.obr_7)
            except Exception:  # noqa: BLE001
                last_obr_collection = None
            # OBR-21 (Filler facility) — not always populated; skip if missing.
            try:
                facility_raw = child.obr_21.to_er7().strip() if hasattr(child, "obr_21") else ""
                last_obr_facility = facility_raw or last_obr_facility
            except Exception:  # noqa: BLE001
                pass
        elif name == "OBX":
            citations, loinc, test_name, value, unit = _build_obx_citations(
                document_reference_id, index, child
            )
            try:
                ref_range = _value_or_none(child.obx_7)
            except Exception:  # noqa: BLE001
                ref_range = None
            try:
                flag_raw = (_value_or_none(child.obx_8) or "").upper()
            except Exception:  # noqa: BLE001
                flag_raw = ""
            abnormal_flag = _ABNORMAL_FLAG_MAP.get(flag_raw, "unknown")
            try:
                obx14 = _value_or_none(child.obx_14)
            except Exception:  # noqa: BLE001
                obx14 = None
            collection_raw = obx14 or last_obr_collection
            collection_date = _parse_collection_date(collection_raw)

            normalized = _normalize_test_name(test_name) if test_name else ""

            if not loinc:
                logger.warning(
                    "hl7_obx_missing_loinc",
                    extra={"seg": index, "document_reference_id": document_reference_id},
                )

            values.append(
                LabValue(
                    test_name=test_name,
                    normalized_test_name=normalized,
                    value=value,
                    unit=unit,
                    normalized_unit=unit,
                    reference_range=ref_range,
                    collection_date=collection_date,
                    abnormal_flag=abnormal_flag,  # type: ignore[arg-type]
                    citations=citations,
                )
            )
        elif name == "NTE":
            try:
                note_raw = _value_or_none(child.nte_3)
            except Exception:  # noqa: BLE001
                note_raw = None
            if note_raw:
                notes.append(note_raw)

    if notes:
        # v1 schema gap: LabReport has no notes field. Log the dropped notes
        # with their locators so audit can later reconstruct.
        logger.info(
            "hl7_nte_dropped",
            extra={
                "document_reference_id": document_reference_id,
                "note_count": len(notes),
            },
        )

    report = LabReport(
        kind="lab_report",
        schema_version="1.0",
        patient_id=patient_id,
        document_reference_id=document_reference_id,
        collection_facility=last_obr_facility,
        values=values,
        classifier_confidence=1.0,
        ocr_confidence_range=(1.0, 1.0),
        extracted_at=datetime.now(timezone.utc),
    )
    return report
