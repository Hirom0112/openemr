"""Per-class extraction prompt + section-header dictionary for intake_form.

R3 fixture coverage: ~54 cases across multiple intake / admission /
triage layouts. Strong enough to ship a per-class prompt (vs the
generic narrative fallback). The header dict feeds the y-band
repointer (``extractors.intake._FIELD_ANCHOR_HINTS``) — values are
case-insensitive substrings matched against detected anchor blocks.
"""

from __future__ import annotations

PROMPT = """You are extracting structured intake-form data from a hospital
admission / intake / triage document. You have two inputs:

1. One image per page of the PDF.
2. A JSON layout produced by deterministic OCR. Each block has a `bbox_id`
   (e.g. "p2-b005"), the page number, and the OCR text inside that region.

Your job: fill the IntakeForm schema by calling the `submit_intake_form` tool.

HARD RULES (the agent will reject your output otherwise):

- Use ONLY values you can locate in the OCR layout. Do NOT invent bbox_ids.
- For EVERY filled clinical field, attach a Citation with:
    source_type      = "document"
    source_id        = the document_reference_id passed to you
    page_or_section  = the page number as a string ("1", "2", ...)
    field_or_chunk_id = the bbox_id from the OCR layout (e.g. "p2-b005")
    quote_or_value   = the exact substring from THAT bbox's text that
                       contains the value. Do NOT rephrase.
    nearest_label    = (OPTIONAL, recommended) 1-3 words from the OCR
                       layout that name the field this value belongs to,
                       as they appear in the document immediately before
                       or above the value. Examples: "DOB", "Date of
                       Birth", "Allergies", "Medications". Used only as
                       a TIE-BREAKER when multiple bboxes contain the
                       same value text — never as a primary signal.
                       Omit if uncertain; do NOT invent labels.
- The cited bbox MUST contain the field's actual VALUE text — never a
  section header, column name, or row label. Concretely: if the value
  is "06/08/1971", the cited bbox's text must contain "06/08/1971"
  (or a substring of it). NEVER cite a bbox whose text is just
  "DEMOGRAPHICS", "DOB", "Address", "Chief Concern", "Medications",
  "Allergies", or any other heading.
- Each demographic / medication / allergy / family-history item MUST
  cite a different bbox_id where its specific value appears. Do NOT
  reuse one section-header bbox across multiple fields.
- Granularity preference: when the OCR layout JSON lists both
  "line" and "word" granularity blocks containing the value, ALWAYS
  prefer the smallest block that fully covers the value. Word-level
  blocks ("granularity": "word") win over line-level blocks
  ("granularity": "line"). Only fall back to a line-level block
  when no word-level block contains the full value (e.g. tabular
  rows where a single OCR line aggregates label + value).
- Each TextField / MedicationItem / AllergyItem / FamilyHistoryItem /
  CodeStatus must have at least one citation.
- Lab values inside a non-LabReport intake document (e.g. a
  "Pertinent Labs" section of a referral letter, or values mentioned
  in HPI prose) MUST be surfaced as entries in ``pertinent_labs``.
  Each entry is a ``LabValue`` with the same shape used by
  ``lab_report``: populate ``test_name``, ``normalized_test_name``,
  ``value``, ``unit`` when printed, ``reference_range`` when printed,
  ``collection_date`` when printed, and ``abnormal_flag`` (use
  ``"high"`` / ``"low"`` only when the document explicitly flags the
  value, e.g. ``[HIGH]`` or ``H``; otherwise ``"unknown"``). The
  cited bbox MUST contain the value text — same value-bbox rule as
  every other field. Omit the field entirely if the document
  carries no labs.
- code_status.value must be one of:
    "full_code", "DNR", "DNI", "comfort_care", "POLST", "unknown".
  Map common phrases: "Full Code"->"full_code", "DNR/DNI"->"DNR".
- Problem List / Past Medical History: when the document has a
  section labelled "PROBLEM LIST", "PAST MEDICAL HISTORY", "PMH",
  or equivalent, populate ``problem_list`` with one ProblemListItem
  per row. Tabular layout typical:
      CONDITION    → condition  (REQUIRED, verbatim text)
      ICD-10       → icd10_code (optional — see grounding rule)
      SNOMED       → snomed_code (optional)
      ONSET        → onset_date (optional, accept verbatim
                     including "~2018" / "adolescence")
      STATUS       → status     (optional — map "Active"→"active",
                     "Resolved"→"resolved", "Inactive"/"Hx"→
                     "inactive"; omit when not stated)
  Each ProblemListItem MUST have ≥1 citation pointing to the bbox
  where the condition value appears. Example for the row
  "Atrial fibrillation | I48.91 | 2022 | Active" cited at p1-b015::
      {"condition": "Atrial fibrillation",
       "icd10_code": "I48.91", "onset_date": "2022",
       "status": "active",
       "citations": [{"source_type": "document",
                       "source_id": "<doc_ref_id>",
                       "page_or_section": "1",
                       "field_or_chunk_id": "p1-b015",
                       "quote_or_value": "I48.91"}]}
  ICD-10 GROUNDING RULE (HARD): only emit ``icd10_code`` if the
  code appears LITERALLY in the source document. Do NOT infer
  ICD-10 codes from condition names — even if you "know" Atrial
  fibrillation maps to I48.91, do NOT emit the code unless the
  document prints it. The system runs a literal-substring
  validator post-extraction and DROPS any code that doesn't
  appear in the OCR text; the row stays (condition + onset +
  status + citations preserved) but the fabricated code is
  nulled out. Same rule applies to ``snomed_code``.
- For each MedicationItem, populate as many of these fields as the
  source document grounds:
    name        — required.
    dose        — required when present in the document.
    route       — verbatim from the ROUTE / FREQ column or equivalent
                  (e.g. "PO daily", "PO BID", "IV", "topical"). Fold
                  combined route+frequency cells into this single
                  field rather than splitting (the schema's separate
                  `frequency` field is a v1.5 split — until then,
                  carry the whole "PO daily AM" string in `route`).
    indication  — verbatim from the REASON / INDICATION column.
  Omit any of dose/route/indication you cannot ground; do NOT
  fabricate. Citation rules apply unchanged — every populated field
  must be groundable in an OCR bbox.
- For each FamilyHistoryItem, in addition to relation + condition,
  populate when the source document grounds them:
    age_at_onset  — string. The age at which the relative was
                    diagnosed with the condition. Verbatim from the
                    OCR (e.g. "61", "50s", "~1999", "Unknown").
    status        — string. Living-or-deceased context. Verbatim
                    from the OCR (e.g. "deceased age 72",
                    "living (age 84, on insulin)", "living").
                    Treat blank "Living?" cells as omitted, not
                    "unknown".
    snomed_code   — string. The SNOMED code as printed in the
                    document (e.g. "22298006", "44054006"). NEVER
                    invent a code; if the document shows
                    "(no code)" or omits the column, omit the field.
  All three are optional. Omit any you cannot ground in the OCR.
- Omit any optional field you cannot ground in the OCR (do not fabricate).
- Set kind="intake_form", schema_version="1.0".
- Set classifier_confidence to a float in [0,1] reflecting your certainty.
- Set ocr_confidence_range to (min_conf, max_conf) across cited blocks.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


# Field-name → tuple of substrings (matched case-insensitively against
# anchor text) registering the section-header dictionary for the y-band
# anchor scorer. Same shape as ``extractors.intake._FIELD_ANCHOR_HINTS``
# (the canonical runtime copy still lives in intake.py — registry entries
# here are the source of truth for new classes; extending this map is
# the documented way to add new spatial hints for intake fixtures).
SECTION_HEADERS: dict[str, tuple[str, ...]] = {
    "name": ("DEMOGRAPHIC", "PATIENT"),
    "dob": ("DEMOGRAPHIC", "PATIENT"),
    "sex": ("DEMOGRAPHIC", "PATIENT"),
    "mrn": ("DEMOGRAPHIC", "PATIENT"),
    "address": ("DEMOGRAPHIC", "PATIENT", "ADDRESS"),
    "chief_concern": ("CHIEF", "COMPLAINT", "REASON"),
    "medication": ("MEDICATION", "MEDS", "RX"),
    "allergy": ("ALLERG", "NKDA"),
    "family": ("FAMILY",),
}


__all__ = ["PROMPT", "SECTION_HEADERS"]
