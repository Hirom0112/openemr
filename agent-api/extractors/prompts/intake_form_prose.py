"""Per-class extraction prompt for prose-mode intake (Phase 9 Slice 9.6).

The vision-fed ``intake_form`` prompt (sibling file ``intake_form.py``)
references ``bbox_id`` locators against per-page PNG images. DOCX referrals
have no images and no bboxes — paragraphs and runs are the only addressable
units. This prompt is the prose-mode variant: same ``IntakeForm`` schema,
same ``submit_intake_form`` tool, but with locator grammar reworked for
``para=N|run=M`` and the system prompt's "cite a bbox" rule replaced with
"cite a paragraph or run".

Decision (Slice 9.6 §591): the prose extractor MUST emit a citable
``LabValue`` for every numeric clinical fact mentioned in HPI prose
(e.g. "LDL-C at 142 mg/dL"). Those LabValues land on the IntakeForm via
the ``current_medications`` / numeric-fact carve-out the schema already
allows — but for prose-mode we emit them as standalone LabValues attached
to the ``IntakeForm`` ``key_facts`` slot is NOT correct because that slot
lives on UnknownDocument. Instead, we widen the schema discipline: the
prose extractor populates ``current_medications`` (with citation) for
medication mentions and produces ``LabValue`` rows that land in a
sibling ``LabReport`` IF the document was structured as labs — otherwise
the LabValues are surfaced via the same prose-mode citation chain on
the corresponding ``key_fact`` text. See ``extract_intake_from_docx``
in ``extractors.intake`` for the wiring.
"""

from __future__ import annotations

PROMPT = """You are extracting structured intake data from a referral
letter or admission note delivered as a Microsoft Word (.docx) document.

You DO NOT have images of the document. You have only the rendered
paragraph + run text. Each paragraph carries a 1-based ``para_idx``
(continuing across body and table cells), and each paragraph contains
zero or more runs each with a 1-based ``run_idx``.

Your job: fill the IntakeForm schema by calling the
``submit_intake_form`` tool.

LOCATOR GRAMMAR (the agent will reject your output otherwise):

- ``page_or_section`` → the paragraph's section name when the prose
  surfaced one (e.g. "History of Present Illness", "Past Medical
  History"); otherwise the literal string ``"prose"``.
- ``field_or_chunk_id`` → ``para={N}`` for paragraph-level citations,
  OR ``para={N}|run={M}`` when the value lives inside a single run.
  Always 1-based, document-order. NEVER fabricate paragraph or run
  numbers higher than the input contains.
- ``quote_or_value`` → the exact substring from THAT paragraph (or run)
  that contains the value. Do NOT rephrase.
- ``bbox`` and ``page`` MUST be omitted (DOCX has no geometry).
- ``source_type`` → ``"document"``.
- ``source_id`` → the ``document_reference_id`` passed to you.

HARD RULES:

- Use ONLY values you can locate in the prose. Do NOT invent.
- For EVERY filled clinical field, attach at least one Citation.
- Each medication / allergy / family-history item MUST cite a different
  ``para=N|run=M`` locator where its specific value appears.
- Numeric clinical facts mentioned in HPI prose (e.g. "LDL-C at
  142 mg/dL", "blood pressure 140/85") MUST be surfaced — emit them
  via the IntakeForm ``current_medications`` when they describe a drug
  + dose pair, OR via a paragraph-level Citation with the full
  ``quote_or_value`` substring. Never drop a numeric clinical fact
  silently.
- Lab values inside a non-LabReport document (e.g. a "Pertinent
  Labs" section of a referral letter, or values cited in HPI prose)
  MUST be surfaced as entries in ``pertinent_labs``. Each entry is a
  ``LabValue`` with the same shape used by ``lab_report``: populate
  ``test_name``, ``normalized_test_name``, ``value``, ``unit`` when
  printed, ``reference_range`` when printed, ``collection_date`` when
  printed, ``abnormal_flag`` (use ``"high"`` / ``"low"`` only when the
  document explicitly flags the value, e.g. ``[HIGH]`` or ``H``;
  otherwise ``"unknown"``), and a paragraph-level Citation. Example
  for the line ``"LDL-C: 142 mg/dL [HIGH]  (2026-04-12)"`` cited at
  ``para=26``::

      {"test_name": "LDL-C",
       "normalized_test_name": "LDL cholesterol",
       "value": "142", "unit": "mg/dL", "reference_range": null,
       "collection_date": "2026-04-12", "abnormal_flag": "high",
       "citations": [{"source_type": "document",
                       "source_id": "<doc_ref_id>",
                       "page_or_section": "Pertinent Labs",
                       "field_or_chunk_id": "para=26",
                       "quote_or_value": "LDL-C: 142 mg/dL [HIGH]"}]}

  NEVER drop a labelled lab value silently. Omit the field entirely
  if the document carries no labs.
- Problem List / Past Medical History extraction: when the prose
  has a section like "Past Medical History:", "Problem List:",
  "PMH:", or narrates problems inline ("known to have…",
  "history significant for…"), populate ``problem_list`` with one
  ProblemListItem per distinct condition. Each item carries:
      condition    REQUIRED — verbatim from the prose
      icd10_code   optional — see grounding rule below
      snomed_code  optional
      onset_date   optional — accept verbatim values including
                   "since 2018" / "adolescence" / "in his 30s"
      status       optional — map "active"→"active",
                   "resolved"→"resolved", "history of"/"former"→
                   "inactive"; omit when not stated
  Each ProblemListItem MUST cite a paragraph (or run) where the
  condition value appears. Example for "PMH: Atrial fibrillation
  (I48.91), Hyperlipidemia, BPH" cited at para=18::
      {"condition": "Atrial fibrillation",
       "icd10_code": "I48.91",
       "citations": [{"source_type": "document",
                       "source_id": "<doc_ref_id>",
                       "page_or_section": "Past Medical History",
                       "field_or_chunk_id": "para=18",
                       "quote_or_value": "Atrial fibrillation (I48.91)"}]}
  ICD-10 GROUNDING RULE (HARD): only emit ``icd10_code`` if the
  code appears LITERALLY in the prose. Do NOT infer ICD-10 codes
  from condition names — even if you "know" Atrial fibrillation
  maps to I48.91, do NOT emit the code unless the document prints
  it. The system runs a literal-substring validator
  post-extraction and DROPS any code that doesn't appear in the
  rendered text; the row stays (condition + onset + status +
  citations preserved) but the fabricated code is nulled out.
  Same rule applies to ``snomed_code``.
- code_status.value must be one of:
    "full_code", "DNR", "DNI", "comfort_care", "POLST", "unknown".
- Omit any optional field you cannot ground in the prose.
- Set kind="intake_form", schema_version="1.0".
- Set classifier_confidence to a float in [0,1].
- Set ocr_confidence_range to (1.0, 1.0) — DOCX has no OCR; the prose
  is rendered text, by definition fully confident.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


# DOCX has no spatial layout, so the section-header dictionary is unused
# in prose mode. We register an empty mapping so the prompt registry's
# uniform shape is preserved.
SECTION_HEADERS: dict[str, tuple[str, ...]] = {}


__all__ = ["PROMPT", "SECTION_HEADERS"]
