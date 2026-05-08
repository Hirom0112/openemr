"""Per-class extraction prompt + section-header dictionary for lab_report_tabular.

R3 fixture coverage: ~37 cases across hematology / chemistry / coag
panels. Strong enough to ship a per-class prompt (vs the generic
narrative fallback). The header dict feeds the y-band repointer's
anchor scorer — header substrings register the document section a
LabValue's row is most likely to appear under.
"""

from __future__ import annotations

PROMPT = """You are extracting structured lab data from an outside-hospital
laboratory report. You have two inputs:

1. One image per page of the PDF.
2. A JSON layout produced by deterministic OCR. Each block has a `bbox_id`
   (e.g. "p2-b005"), the page number, and the OCR text inside that region.

Your job: fill the LabReport schema by calling the `submit_lab_report` tool.

HARD RULES (the agent will reject your output otherwise):

- Use ONLY values you can locate in the OCR layout. Do NOT invent bbox_ids.
- For EVERY filled clinical field, attach a Citation with:
    source_type      = "document"
    source_id        = the document_reference_id passed to you
    page_or_section  = the page number as a string ("1", "2", ...)
    field_or_chunk_id = the bbox_id from the OCR layout (e.g. "p2-b005")
    quote_or_value   = the exact substring from THAT bbox's text that
                       contains the value. Do NOT rephrase.
- Each LabValue.citations must have at least one citation.
- For abnormal_flag, map: "HH"->"critical_high", "LL"->"critical_low",
  "H"->"high", "L"->"low", blank->"normal".
- normalized_test_name: lowercase test name (e.g. "lactate", "wbc",
  "creatinine", "sodium").
- Patient demographics printed on the lab report header (PATIENT
  block at the top of the document) MUST be surfaced in
  ``patient_demographics``. Each populated sub-field is a TextField
  with at least one Citation pointing to the bbox where the value
  appears:
    name      — full name, verbatim (e.g. "WHITAKER, JAMES").
    dob       — date of birth, verbatim from the OCR (e.g.
                "1958-11-03" or "11/03/1958"; do NOT reformat).
    sex       — single letter or word as printed (e.g. "M", "F",
                "Male", "Female"). Lowercase the canonical value:
                "M"/"Male"->"male", "F"/"Female"->"female".
    mrn       — medical record number, verbatim. Cite the bbox that
                contains the MRN value, NOT the bbox that says "MRN".
    address   — patient address. Cite the bbox containing the
                address value.
  Omit any sub-field the document doesn't print. Same value-bbox
  rule as every other field — never cite a label-only bbox like
  "DOB" or "MRN"; always cite the bbox containing the value.
- Set kind="lab_report", schema_version="1.0".
- Set classifier_confidence to a float in [0,1] reflecting your certainty.
- Set ocr_confidence_range to (min_conf, max_conf) across cited blocks.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


# Section-header dict for the y-band anchor scorer. Lab tabular
# documents tend to organize values under panel headers
# ("HEMATOLOGY", "CHEMISTRY", "COAGULATION", "URINALYSIS"). The
# repointer uses these as a tiebreaker when multiple OCR blocks
# overlap the LabValue's value text.
SECTION_HEADERS: dict[str, tuple[str, ...]] = {
    "test_name": ("HEMATOLOGY", "CHEMISTRY", "COAGULATION", "URINALYSIS", "PANEL"),
    "value": ("RESULT", "VALUE"),
    "reference_range": ("REFERENCE", "RANGE", "NORMAL"),
    "units": ("UNITS",),
    "abnormal_flag": ("FLAG",),
}


__all__ = ["PROMPT", "SECTION_HEADERS"]
