"""Production intake-form extractor (Slice 4.5).

Pipeline (W2_ARCHITECTURE.md §5.3 / §7.2):

    pdf_bytes
        -> documents.ocr.extract_layout (deterministic location)
        -> classifier.classify_keywords (fast-path)
        -> (intake_form) Claude vision schema-fill via tool_use
        -> IntakeForm pydantic validation
        -> (otherwise) UnknownDocument fallback (no LLM call)

Mirrors ``extractors.lab.extract`` — same model fallback chain, same
citation contract (§8), same async + PSR-3 logging discipline. Reuses
``ExtractionFailed`` from ``extractors.lab`` so callers handle one failure
class regardless of dispatch.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import anthropic
import pymupdf

from agent.metrics import agent_citation_repoint_total
from documents.ocr import LayoutBlock, extract_layout
from extractors.classifier import classify_keywords
from extractors.lab import ExtractionFailed  # re-export single failure class
from extractors.schemas import (
    Citation,
    IntakeForm,
    KeyFact,
    UnknownDocument,
)

logger = logging.getLogger(__name__)

_MODEL_CANDIDATES: Tuple[str, ...] = (
    "claude-sonnet-4-5-20250929",
    "claude-3-5-sonnet-20241022",
)

_PROMPT = """You are extracting structured intake-form data from a hospital
admission / intake document. You have two inputs:

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
- The cited bbox MUST contain the field's actual VALUE text — never a
  section header, column name, or row label. Concretely: if the value
  is "06/08/1971", the cited bbox's text must contain "06/08/1971"
  (or a substring of it). NEVER cite a bbox whose text is just
  "DEMOGRAPHICS", "DOB", "Address", "Chief Concern", "Medications",
  "Allergies", or any other heading.
- Each demographic / medication / allergy / family-history item MUST
  cite a different bbox_id where its specific value appears. Do NOT
  reuse one section-header bbox across multiple fields.
- Each TextField / MedicationItem / AllergyItem / FamilyHistoryItem /
  CodeStatus must have at least one citation.
- code_status.value must be one of:
    "full_code", "DNR", "DNI", "comfort_care", "POLST", "unknown".
  Map common phrases: "Full Code"->"full_code", "DNR/DNI"->"DNR".
- Omit any optional field you cannot ground in the OCR (do not fabricate).
- Set kind="intake_form", schema_version="1.0".
- Set classifier_confidence to a float in [0,1] reflecting your certainty.
- Set ocr_confidence_range to (min_conf, max_conf) across cited blocks.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


# --------------------------------------------------------------------------- #
# Helpers (mirror lab.py — small surface area, no shared mutable state)
# --------------------------------------------------------------------------- #


def _render_pages_to_png(pdf_bytes: bytes) -> List[bytes]:
    pngs: List[bytes] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
            pngs.append(pix.tobytes("png"))
    return pngs


def _layout_to_prompt_json(blocks: List[LayoutBlock]) -> str:
    return json.dumps(
        [
            {
                "bbox_id": b.bbox_id,
                "page": b.page,
                "text": b.text,
                "ocr_confidence": b.ocr_confidence,
            }
            for b in blocks
        ],
        ensure_ascii=False,
    )


def _build_user_content(
    pdf_bytes: bytes,
    blocks: List[LayoutBlock],
    patient_id: str,
    document_reference_id: str,
) -> List[dict[str, Any]]:
    pngs = _render_pages_to_png(pdf_bytes)
    content: List[dict[str, Any]] = []
    for png in pngs:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": (
                f"patient_id = {patient_id}\n"
                f"document_reference_id = {document_reference_id}\n"
                f"current_utc = {datetime.now(timezone.utc).isoformat()}\n\n"
                "OCR layout JSON (the only source of truth for bbox_ids):\n"
                f"{_layout_to_prompt_json(blocks)}"
            ),
        }
    )
    return content


def _ocr_confidence_range(blocks: List[LayoutBlock]) -> Tuple[float, float]:
    if not blocks:
        return (0.0, 0.0)
    confs = [b.ocr_confidence for b in blocks]
    return (min(confs), max(confs))


def _unknown_summary(blocks: List[LayoutBlock]) -> str:
    snippets: List[str] = []
    for b in blocks[:3]:
        text = " ".join(b.text.split())
        if text:
            snippets.append(text)
        if len(snippets) == 3:
            break
    if not snippets:
        return "Document has no extractable text."
    joined = " | ".join(snippets)
    if len(joined) > 240:
        joined = joined[:237] + "..."
    return f"Unclassified clinical document; first text regions: {joined}"


def _index_blocks(blocks: List[LayoutBlock]) -> dict[str, LayoutBlock]:
    return {b.bbox_id: b for b in blocks}


_NORMALIZE_RE = re.compile(r"\W+")


def _normalize_for_match(s: str) -> str:
    """Strip non-word chars and uppercase. Lets us treat '06/08/1971' and
    '06081971' as the same token."""
    return _NORMALIZE_RE.sub("", s or "").upper()


def _value_in_block(value: str, block_text: str) -> bool:
    nv = _normalize_for_match(value)
    nb = _normalize_for_match(block_text)
    if not nv or not nb:
        return False
    return nv in nb or nb in nv


def _block_centroid_y(b: LayoutBlock) -> float:
    """Vertical centroid of a layout block (PDF-points)."""
    _x, y, _w, h = b.bbox
    return float(y) + float(h) / 2.0


def _block_height(b: LayoutBlock) -> float:
    return float(b.bbox[3])


def _block_granularity(b: LayoutBlock) -> Optional[str]:
    """Read Wave-2A's ``granularity`` field if present; otherwise None.

    Wave 2A adds a ``granularity`` attribute on LayoutBlock; the value may
    be a ``BlockGranularity`` enum (``WORD`` / ``LINE``) or — in test code
    that constructs simple namespaces — a plain string. We normalize to
    the enum's string value (lowercased) so callers can compare cheaply.
    Returns None when the field is absent (Wave 2A not yet landed)."""
    val = getattr(b, "granularity", None)
    if val is None:
        return None
    # str subclass / plain string → use as-is.
    inner = getattr(val, "value", val)
    return str(inner)


def _is_line_granularity(b: LayoutBlock) -> bool:
    """True iff the block is line-granularity. Tolerant of casing and of
    both enum / string representations from Wave 2A."""
    g = _block_granularity(b)
    return g is not None and g.upper() == "LINE"


def _is_anchor_eligible(b: LayoutBlock, *, page_median_height: float) -> bool:
    """Decide whether a layout block looks structurally like a section header.

    Signals (a block must satisfy ≥2 of these to qualify):

    - All-caps text: of the alphabetic characters in the block, at least 80%
      are uppercase, and there are at least 3 alphabetic characters total.
      (Numeric-only blocks fail this signal.)
    - Trailing colon: the trimmed text ends with ``:`` (e.g. "DEMOGRAPHICS:",
      "Chief Concern:").
    - Tall: the block's height exceeds ``1.4 × page_median_height`` — section
      headers tend to render taller than body text on the same page.
    - Short standalone line: the block contains 1-3 whitespace-delimited
      tokens. Long sentences are not headers.

    Ratio + threshold are intentionally fuzzy and deterministic; no
    hand-curated keyword list. See `_detect_section_anchors` for callers."""
    text = (b.text or "").strip()
    if not text:
        return False

    signals = 0

    # Signal 1: all-caps (alphabetic letters only).
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 3:
        upper = sum(1 for c in letters if c.isupper())
        if upper / len(letters) >= 0.8:
            signals += 1

    # Signal 2: trailing colon.
    if text.endswith(":"):
        signals += 1

    # Signal 3: significantly taller than the page-median block.
    if page_median_height > 0.0 and _block_height(b) > 1.4 * page_median_height:
        signals += 1

    # Signal 4: short standalone line (1-3 whitespace tokens).
    tokens = text.split()
    if 1 <= len(tokens) <= 3:
        signals += 1

    return signals >= 2


def _detect_section_anchors(blocks: List[LayoutBlock]) -> List[LayoutBlock]:
    """Return the subset of `blocks` that look structurally like section
    headers. See `_is_anchor_eligible` for the rule set."""
    if not blocks:
        return []

    # Compute a per-page median block height so the "tall" signal is
    # page-relative. A single global median would misclassify on
    # multi-page docs with different layouts per page.
    by_page: dict[int, List[float]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(_block_height(b))

    page_median: dict[int, float] = {}
    for page, heights in by_page.items():
        sh = sorted(heights)
        n = len(sh)
        if n == 0:
            page_median[page] = 0.0
        elif n % 2 == 1:
            page_median[page] = sh[n // 2]
        else:
            page_median[page] = (sh[n // 2 - 1] + sh[n // 2]) / 2.0

    anchors: List[LayoutBlock] = []
    for b in blocks:
        if _is_anchor_eligible(b, page_median_height=page_median.get(b.page, 0.0)):
            anchors.append(b)
    return anchors


# Field-name → tuple of substrings (matched case-insensitively against
# anchor text) that indicate the anchor is likely the section header for
# that field. Entries are intentionally minimal — extending this map is
# how we add new spatial hints without touching the selection algorithm.
#
# Rationale per entry:
#   name           → demographics blocks (patient identifier section)
#   dob            → demographics blocks (date of birth lives there)
#   sex            → demographics blocks
#   mrn            → demographics blocks
#   address        → demographics blocks
#   chief_concern  → "chief concern" / "complaint" / "reason for visit"
#   medication     → "medication" header (covers MEDICATIONS, CURRENT MEDS)
#   allergy        → "allerg" stem covers ALLERGIES / ALLERGY / NKDA section
#   family         → "family" header (FAMILY HISTORY)
_FIELD_ANCHOR_HINTS: dict[str, tuple[str, ...]] = {
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


def _candidate_blocks_for_value(
    value: str, blocks: List[LayoutBlock]
) -> List[Tuple[LayoutBlock, int]]:
    """Return all blocks whose normalized text overlaps `value` above the
    minimum-length floor, paired with the overlap length. The floor is
    identical to the legacy `_find_block_for_value` floor: at least 4
    normalized chars OR half the value length, whichever is smaller."""
    nv = _normalize_for_match(value)
    if not nv:
        return []
    min_overlap = min(4, max(1, len(nv) // 2))
    out: List[Tuple[LayoutBlock, int]] = []
    for b in blocks:
        nb = _normalize_for_match(b.text)
        if not nb:
            continue
        if nv in nb:
            overlap = len(nv)
        elif nb in nv:
            overlap = len(nb)
        else:
            continue
        if overlap < min_overlap:
            continue
        out.append((b, overlap))
    return out


def _anchor_text_compatible(
    anchor: LayoutBlock, field_name: Optional[str]
) -> bool:
    """True iff `anchor.text` contains any hint substring registered for
    `field_name`. Returns False when the field has no registered hints —
    callers fall back to "closest anchor of any kind" in that case."""
    if not field_name:
        return False
    hints = _FIELD_ANCHOR_HINTS.get(field_name)
    if not hints:
        return False
    upper_text = (anchor.text or "").upper()
    return any(h.upper() in upper_text for h in hints)


def _nearest_anchor(
    candidate: LayoutBlock, anchors: List[LayoutBlock]
) -> Tuple[Optional[LayoutBlock], float]:
    """Return (nearest-anchor, |Δy|) on the same page. If no anchor exists
    on the candidate's page, fall back to anchors on any page (cross-page
    distance still uses centroid Δy, which is a coarse but deterministic
    tiebreaker)."""
    if not anchors:
        return (None, float("inf"))
    cy = _block_centroid_y(candidate)
    same_page = [a for a in anchors if a.page == candidate.page]
    pool = same_page or anchors
    best: Optional[LayoutBlock] = None
    best_dy = float("inf")
    for a in pool:
        dy = abs(_block_centroid_y(a) - cy)
        if dy < best_dy:
            best_dy = dy
            best = a
    return (best, best_dy)


def _select_best_candidate(
    candidates: List[Tuple[LayoutBlock, int]],
    anchors: List[LayoutBlock],
    field_name: Optional[str],
) -> Tuple[LayoutBlock, Optional[LayoutBlock], float, str]:
    """Pick the best candidate block among ≥2 candidates.

    Returns (chosen_block, nearest_anchor, y_distance, outcome_label).
    Outcome label is one of ``repointed_with_anchor`` (anchor-driven
    selection succeeded) or ``repointed_no_anchor`` (anchor pool empty
    or fell back to overlap/granularity tiebreaks)."""
    # Compute (candidate, overlap, anchor, dy, anchor_compatible) tuples.
    enriched: List[
        Tuple[LayoutBlock, int, Optional[LayoutBlock], float, bool]
    ] = []
    for cand, overlap in candidates:
        anchor, dy = _nearest_anchor(cand, anchors)
        compat = anchor is not None and _anchor_text_compatible(anchor, field_name)
        enriched.append((cand, overlap, anchor, dy, compat))

    # Step 1: prefer field-compatible anchored candidates if any exist.
    compat_set = [e for e in enriched if e[4]]
    if compat_set:
        # Smallest |Δy| wins; tie-break by overlap (desc) then LINE granularity.
        compat_set.sort(
            key=lambda e: (
                e[3],
                -e[1],
                0 if _is_line_granularity(e[0]) else 1,
            )
        )
        chosen, _ov, anchor, dy, _c = compat_set[0]
        return (chosen, anchor, dy, "repointed_with_anchor")

    # Step 2: if any anchor exists at all, prefer the candidate whose
    # nearest anchor is closest, regardless of text content.
    if anchors:
        enriched_sorted = sorted(
            enriched,
            key=lambda e: (
                e[3],
                -e[1],
                0 if _is_line_granularity(e[0]) else 1,
            ),
        )
        chosen, _ov, anchor, dy, _c = enriched_sorted[0]
        return (chosen, anchor, dy, "repointed_with_anchor")

    # Step 3: no anchors detected — fall back to legacy overlap-then-LINE
    # tiebreak. This preserves pre-2B behavior on documents that have no
    # structural section headers (e.g. plain free-text scans).
    legacy_sorted = sorted(
        enriched,
        key=lambda e: (
            -e[1],
            0 if _is_line_granularity(e[0]) else 1,
        ),
    )
    chosen, _ov, _a, _dy, _c = legacy_sorted[0]
    return (chosen, None, float("inf"), "repointed_no_anchor")


def _find_block_for_value(
    value: str,
    blocks: List[LayoutBlock],
    *,
    anchors: Optional[List[LayoutBlock]] = None,
    field_name: Optional[str] = None,
) -> Optional[LayoutBlock]:
    """Search the layout for the block whose text best matches `value`.

    Behavior:

    - 0 candidates above the overlap floor → return None.
    - 1 candidate → return it (legacy single-block path).
    - ≥2 candidates → spatial selection: prefer candidates whose nearest
      section anchor is field-compatible (per `_FIELD_ANCHOR_HINTS`),
      then by smallest |Δy| to any anchor, then by overlap length, then
      by LINE granularity over WORD granularity.

    Floor: overlap must be at least 4 normalized chars OR cover at least
    half of `value`, whichever is smaller. Without this, two-letter
    coincidences (e.g. 'IL' inside 'LISINOPRIL' matching the IL state
    abbreviation in the address) win, and the citation lands on an
    unrelated bbox far from the value's actual position."""
    cands = _candidate_blocks_for_value(value, blocks)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0][0]
    anchor_pool = anchors if anchors is not None else _detect_section_anchors(blocks)
    chosen, _anchor, _dy, _outcome = _select_best_candidate(
        cands, anchor_pool, field_name
    )
    return chosen


def _repoint_citation(
    cit: Citation,
    value: Optional[str],
    blocks: List[LayoutBlock],
    block_index: dict[str, LayoutBlock],
    *,
    anchors: Optional[List[LayoutBlock]] = None,
    field_name: Optional[str] = None,
) -> Citation:
    """If the cited bbox doesn't contain the field's actual value, search
    the layout for one that does and re-point the citation. The LLM
    sometimes parks every demographic field on the section-header block
    ('DEMOGRAPHICS', 'CHIEF CONCERN', ...) — this corrects that without
    discarding the citation.

    Wave 2B: when ≥2 layout blocks contain the value text, prefer the
    one whose nearest structural section anchor is compatible with
    `field_name` (e.g. for `dob`, prefer a candidate near a
    "DEMOGRAPHICS" anchor over one near "SIGNATURE"). Anchors are
    detected structurally (`_detect_section_anchors`), not from a
    hand-curated dictionary."""
    # Lazy-compute the anchor pool once per call site.
    anchor_pool = anchors if anchors is not None else _detect_section_anchors(blocks)

    cited_block = block_index.get(cit.field_or_chunk_id)
    if value and cited_block is not None and _value_in_block(value, cited_block.text):
        # Already cites a block containing the value — just hydrate.
        agent_citation_repoint_total.labels(
            field=field_name or "unknown", outcome="kept"
        ).inc()
        logger.info(
            "extractor_citation_repointed",
            extra={
                "tool": "intake",
                "field_name": field_name,
                "outcome": "kept",
                "from": cit.field_or_chunk_id,
                "to": cit.field_or_chunk_id,
                "candidate_count": 1,
                "chosen_bbox_id": cited_block.bbox_id,
                "chosen_granularity": _block_granularity(cited_block),
                "anchor_bbox_id": None,
                "anchor_text_preview": None,
                "y_distance": None,
                "value_preview": (value or "")[:32],
            },
        )
        return cit.model_copy(update={"bbox": cited_block.bbox, "page": cited_block.page})

    if value:
        cands = _candidate_blocks_for_value(value, blocks)
        if cands:
            if len(cands) == 1:
                target = cands[0][0]
                outcome = "repointed_no_anchor"
                anchor: Optional[LayoutBlock] = None
                dy = float("inf")
            else:
                target, anchor, dy, outcome = _select_best_candidate(
                    cands, anchor_pool, field_name
                )
            agent_citation_repoint_total.labels(
                field=field_name or "unknown", outcome=outcome
            ).inc()
            anchor_text_preview = (
                (anchor.text or "")[:32] if anchor is not None else None
            )
            logger.info(
                "extractor_citation_repointed",
                extra={
                    "tool": "intake",
                    "field_name": field_name,
                    "outcome": outcome,
                    "from": cit.field_or_chunk_id,
                    "to": target.bbox_id,
                    "candidate_count": len(cands),
                    "chosen_bbox_id": target.bbox_id,
                    "chosen_granularity": _block_granularity(target),
                    "anchor_bbox_id": anchor.bbox_id if anchor is not None else None,
                    "anchor_text_preview": anchor_text_preview,
                    "y_distance": (
                        round(dy, 2) if dy != float("inf") else None
                    ),
                    "value_preview": (value or "")[:32],
                },
            )
            return cit.model_copy(
                update={
                    "field_or_chunk_id": target.bbox_id,
                    "quote_or_value": target.text or cit.quote_or_value,
                    "bbox": target.bbox,
                    "page": target.page,
                }
            )
        # No candidate cleared the floor: emit a no_match observation so
        # we can audit how often the LLM citation goes unverified.
        agent_citation_repoint_total.labels(
            field=field_name or "unknown", outcome="no_match"
        ).inc()
        logger.info(
            "extractor_citation_repointed",
            extra={
                "tool": "intake",
                "field_name": field_name,
                "outcome": "no_match",
                "from": cit.field_or_chunk_id,
                "to": cit.field_or_chunk_id,
                "candidate_count": 0,
                "chosen_bbox_id": cit.field_or_chunk_id,
                "chosen_granularity": (
                    _block_granularity(cited_block) if cited_block is not None else None
                ),
                "anchor_bbox_id": None,
                "anchor_text_preview": None,
                "y_distance": None,
                "value_preview": (value or "")[:32],
            },
        )

    # No value to match against, or no overlap anywhere — best we can do
    # is keep the original cite and stamp bbox/page if the bbox_id is real.
    if cited_block is None:
        logger.warning(
            "extractor_citation_bbox_lookup_failed",
            extra={
                "field_or_chunk_id": cit.field_or_chunk_id,
                "source_id": cit.source_id,
                "tool": "intake",
            },
        )
        return cit
    return cit.model_copy(update={"bbox": cited_block.bbox, "page": cited_block.page})


def _hydrate_citation(cit: Citation, block_index: dict[str, LayoutBlock]) -> Citation:
    """Hydrate a citation that has no value-anchor (used by the
    fallback synthetic citations only)."""
    block = block_index.get(cit.field_or_chunk_id)
    if block is None:
        logger.warning(
            "extractor_citation_bbox_lookup_failed",
            extra={
                "field_or_chunk_id": cit.field_or_chunk_id,
                "source_id": cit.source_id,
                "tool": "intake",
            },
        )
        return cit
    return cit.model_copy(update={"bbox": block.bbox, "page": block.page})


def _hydrate_citations_list(
    citations: List[Citation], block_index: dict[str, LayoutBlock]
) -> List[Citation]:
    return [_hydrate_citation(c, block_index) for c in citations]


def _repoint_citations_list(
    citations: List[Citation],
    value: Optional[str],
    blocks: List[LayoutBlock],
    block_index: dict[str, LayoutBlock],
    *,
    field_name: Optional[str] = None,
    anchors: Optional[List[LayoutBlock]] = None,
) -> List[Citation]:
    anchor_pool = anchors if anchors is not None else _detect_section_anchors(blocks)
    return [
        _repoint_citation(
            c, value, blocks, block_index, anchors=anchor_pool, field_name=field_name
        )
        for c in citations
    ]


def _hydrate_intake_form_citations(
    form: IntakeForm, blocks: List[LayoutBlock]
) -> IntakeForm:
    """Walk every cite-bearing IntakeForm field and stamp bbox/page."""
    block_index = _index_blocks(blocks)
    anchors = _detect_section_anchors(blocks)

    # Demographics — TextField sub-fields each carry citations.
    demographics = form.demographics
    if demographics is not None:
        demo_updates: dict[str, Any] = {}
        for attr in ("name", "dob", "sex", "mrn", "address"):
            tf = getattr(demographics, attr, None)
            if tf is not None:
                demo_updates[attr] = tf.model_copy(
                    update={
                        "citations": _repoint_citations_list(
                            tf.citations,
                            tf.value,
                            blocks,
                            block_index,
                            field_name=attr,
                            anchors=anchors,
                        )
                    }
                )
        if demo_updates:
            demographics = demographics.model_copy(update=demo_updates)

    chief = form.chief_concern
    if chief is not None:
        chief = chief.model_copy(
            update={
                "citations": _repoint_citations_list(
                    chief.citations,
                    chief.value,
                    blocks,
                    block_index,
                    field_name="chief_concern",
                    anchors=anchors,
                )
            }
        )

    meds = [
        m.model_copy(
            update={
                "citations": _repoint_citations_list(
                    m.citations,
                    m.name,
                    blocks,
                    block_index,
                    field_name="medication",
                    anchors=anchors,
                )
            }
        )
        for m in form.current_medications
    ]
    allergies = [
        a.model_copy(
            update={
                "citations": _repoint_citations_list(
                    a.citations,
                    a.substance,
                    blocks,
                    block_index,
                    field_name="allergy",
                    anchors=anchors,
                )
            }
        )
        for a in form.allergies
    ]
    fam = [
        f.model_copy(
            update={
                "citations": _repoint_citations_list(
                    f.citations,
                    f.condition,
                    blocks,
                    block_index,
                    field_name="family",
                    anchors=anchors,
                )
            }
        )
        for f in form.family_history
    ]
    code_status = form.code_status
    if code_status is not None:
        code_status = code_status.model_copy(
            update={
                "citations": _hydrate_citations_list(code_status.citations, block_index)
            }
        )

    return form.model_copy(
        update={
            "demographics": demographics,
            "chief_concern": chief,
            "current_medications": meds,
            "allergies": allergies,
            "family_history": fam,
            "code_status": code_status,
        }
    )


def _unknown_key_facts(
    blocks: List[LayoutBlock], document_reference_id: str
) -> List[KeyFact]:
    if not blocks:
        return []
    first = blocks[0]
    snippet = " ".join(first.text.split())
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return [
        KeyFact(
            text=snippet or "(empty block)",
            citations=[
                Citation(
                    source_type="document",
                    source_id=document_reference_id,
                    page_or_section=str(first.page),
                    field_or_chunk_id=first.bbox_id,
                    quote_or_value=first.text,
                    bbox=first.bbox,
                    page=first.page,
                )
            ],
        )
    ]


# --------------------------------------------------------------------------- #
# Claude vision call (async)
# --------------------------------------------------------------------------- #


async def _call_claude_extract(
    client: anthropic.AsyncAnthropic,
    user_content: List[dict[str, Any]],
) -> dict[str, Any]:
    """Call Claude with the IntakeForm tool. Returns the tool input dict."""
    tool = {
        "name": "submit_intake_form",
        "description": "Submit the structured IntakeForm extracted from the document.",
        "input_schema": IntakeForm.model_json_schema(),
    }
    last_err: Exception | None = None
    for model in _MODEL_CANDIDATES:
        try:
            t0 = time.monotonic()
            resp = await client.messages.create(
                model=model,
                max_tokens=4096,
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_intake_form"},
                system=_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "extractor_claude_call_ok",
                extra={"model": model, "duration_ms": duration_ms, "tool": "intake"},
            )
            for block in resp.content:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "submit_intake_form"
                ):
                    return dict(block.input)
            last_err = RuntimeError("no tool_use block in response")
            logger.error(
                "extractor_claude_no_tool_use",
                extra={"model": model, "tool": "intake"},
            )
        except anthropic.NotFoundError as e:
            last_err = e
            logger.warning(
                "extractor_claude_model_unavailable",
                extra={"model": model, "tool": "intake"},
            )
            continue
        except Exception as e:  # noqa: BLE001 — boundary, re-wrapped below
            last_err = e
            logger.error(
                "extractor_claude_call_failed",
                extra={"model": model, "tool": "intake", "error_type": type(e).__name__},
            )
            break

    raise ExtractionFailed("vision call failed") from last_err


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def extract_intake(
    pdf_bytes: bytes,
    *,
    patient_id: str,
    document_reference_id: str,
) -> IntakeForm | UnknownDocument:
    """Run the intake extractor and return a validated IntakeForm or UnknownDocument.

    Mirrors ``extractors.lab.extract`` but for intake forms. On any non-intake
    fast-path verdict (or no verdict at all), returns ``UnknownDocument`` —
    no LLM call. Raises ``ExtractionFailed`` only when the intake path fails.
    """
    blocks = extract_layout(pdf_bytes)
    if not blocks:
        logger.error(
            "extractor_no_layout_blocks",
            extra={"document_reference_id": document_reference_id, "tool": "intake"},
        )
        raise ExtractionFailed("vision call failed")

    ocr_range = _ocr_confidence_range(blocks)
    verdict = classify_keywords(blocks)

    # Fallback path: any non-intake outcome → UnknownDocument.
    if verdict is None or verdict.kind != "intake_form":
        classifier_confidence = verdict.confidence if verdict is not None else 0.0
        guess = verdict.kind if verdict is not None else "unknown"
        logger.info(
            "extractor_unknown_fallback",
            extra={
                "document_reference_id": document_reference_id,
                "verdict_kind": guess,
                "classifier_confidence": classifier_confidence,
                "tool": "intake",
            },
        )
        return UnknownDocument(
            kind="unknown",
            schema_version="1.0",
            patient_id=patient_id,
            document_reference_id=document_reference_id,
            document_kind_guess=guess,
            summary=_unknown_summary(blocks),
            key_facts=_unknown_key_facts(blocks, document_reference_id),
            classifier_confidence=classifier_confidence,
            ocr_confidence_range=ocr_range,
            extracted_at=datetime.now(timezone.utc),
        )

    # Intake path — Claude vision schema-fill.
    client = anthropic.AsyncAnthropic()
    user_content = _build_user_content(
        pdf_bytes, blocks, patient_id, document_reference_id
    )
    tool_input = await _call_claude_extract(client, user_content)

    try:
        form = IntakeForm.model_validate_json(json.dumps(tool_input))
    except Exception as e:  # noqa: BLE001 — boundary
        logger.error(
            "extractor_validation_failed",
            extra={
                "document_reference_id": document_reference_id,
                "error_type": type(e).__name__,
                "tool": "intake",
            },
        )
        raise ExtractionFailed("vision call failed") from e

    final = _hydrate_intake_form_citations(
        form.model_copy(
            update={
                "classifier_confidence": float(verdict.confidence),
                "ocr_confidence_range": ocr_range,
            }
        ),
        blocks,
    )
    logger.info(
        "extractor_intake_ok",
        extra={
            "document_reference_id": document_reference_id,
            "n_meds": len(final.current_medications),
            "n_allergies": len(final.allergies),
            "classifier_confidence": final.classifier_confidence,
        },
    )
    return final
