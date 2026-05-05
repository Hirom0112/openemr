"""Per-class extraction prompt + section-header dictionary for other_narrative.

R3 fixture coverage: collapsed bucket — consultant_note (27 cases / 1
fixture), imaging_report (7 / 1), mixed_content (6 / 1). None
individually has enough diversity to justify a dedicated per-class
prompt, so they share a generic narrative prompt that surfaces a few
KeyFacts plus the doc-class hint (``sub_kind``) the classifier passed
in. Routed via UnknownDocument today; future per-sub-kind prompts
become trivial drop-ins under this same registry shape.
"""

from __future__ import annotations

PROMPT = """You are extracting key facts from a narrative clinical
document (e.g. a consultant note, imaging report, or mixed-content
discharge packet). You have two inputs:

1. One image per page of the PDF.
2. A JSON layout produced by deterministic OCR. Each block has a `bbox_id`
   (e.g. "p2-b005"), the page number, and the OCR text inside that region.

Your job: extract a short list of KeyFacts that summarize the document.
Each KeyFact carries one or more citations whose ``field_or_chunk_id``
points at a real bbox in the OCR layout.

HARD RULES:

- Use ONLY values you can locate in the OCR layout. Do NOT invent bbox_ids.
- Each KeyFact MUST have at least one citation.
- Cite the bbox whose text contains the supporting evidence — never a
  section heading or column label as the sole citation.
- Do NOT rephrase clinical values; quote exactly.
- Set kind="unknown", schema_version="1.0".
- Set classifier_confidence to a float in [0,1] reflecting your certainty.
- Set ocr_confidence_range to (min_conf, max_conf) across cited blocks.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


# Section-header dict for narrative docs — thin by design. Per R3,
# heterogeneity across consultant_note / imaging_report / mixed_content
# means we cannot register field-specific hints with confidence; the
# entries below are the union of headers that show up across all three
# sub-kinds and serve as a coarse y-band signal only.
SECTION_HEADERS: dict[str, tuple[str, ...]] = {
    "impression": ("IMPRESSION", "ASSESSMENT", "CONCLUSION"),
    "findings": ("FINDINGS", "RESULTS"),
    "history": ("HISTORY", "HPI", "INDICATION"),
    "recommendation": ("RECOMMEND", "PLAN", "FOLLOW"),
}


__all__ = ["PROMPT", "SECTION_HEADERS"]
