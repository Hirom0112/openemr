"""Per-doc-class extraction prompts and section-header dictionaries.

Each module in this package exposes two module-level constants:

- ``PROMPT`` — the system prompt sent to the Claude vision call.
- ``SECTION_HEADERS`` — a mapping of ``field_name → tuple[str, ...]``
  registering header substrings that the y-band repointer's
  ``_anchor_text_compatible`` check uses as a hint for that field.

R3 audit (Wave 2D): only intake_form and lab_report_tabular have enough
fixture diversity to justify per-class prompts; everything narrative
(consultant_note / imaging_report / mixed_content) collapses to
``other_narrative`` with a generic prompt and a thin header dict. The
``non_extractable`` class is a deterministic pre-LLM gate (page count
== 0 OR encrypted OR OCR token count < 5) and therefore has NO prompt
file — see ``extractors.classifier._is_non_extractable``.
"""

from extractors.prompts import intake_form, lab_report_tabular, other_narrative

__all__ = ["intake_form", "lab_report_tabular", "other_narrative"]
