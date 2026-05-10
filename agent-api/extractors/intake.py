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

from agent.metrics import (
    agent_citation_repoint_total,
    agent_icd10_guardrail_rejections_total,
)
from documents.docx_loader import (
    DocxParagraph,
    extract_docx_paragraphs,
    format_locator,
)
from documents.ocr import LayoutBlock, extract_layout
from extractors.classifier import classify_keywords
from extractors.lab import ExtractionFailed  # re-export single failure class
from extractors.prompt_registry import get_prompt
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

# Wave 2D: prompt sourced from the per-class registry. Kept as a
# module attribute (not a constant) so test suites that reference
# ``extractors.intake._PROMPT`` continue to work.
_PROMPT = get_prompt("intake_form")


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
                # Wave 2A — surface granularity so the prompt can steer
                # the LLM toward word-level blocks (tighter citations).
                "granularity": _block_granularity(b) or "line",
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


def _block_polygon_list(block: LayoutBlock) -> Optional[List[Tuple[float, float]]]:
    """Convert a LayoutBlock.polygon (tuple-of-tuples or None) into the
    list-of-pairs shape the Citation pydantic schema expects. ``None`` when
    the source engine has no polygon — never fabricate one."""
    poly = getattr(block, "polygon", None)
    if not poly:
        return None
    return [tuple(p) for p in poly]


_NORMALIZE_RE = re.compile(r"\W+")


def _normalize_for_match(s: str) -> str:
    """Strip non-word chars and uppercase. Lets us treat '06/08/1971' and
    '06081971' as the same token."""
    return _NORMALIZE_RE.sub("", s or "").upper()


# ── ICD-10 hallucination guardrail ────────────────────────────────────────
#
# ProblemListItem.icd10_code MUST appear literally in the source text.
# When the LLM produces a code that doesn't, we drop the code (set to
# None) but keep the row — the problem still surfaces, just without a
# fabricated code. Mirrors the discipline established for citations:
# "every clinical claim must be groundable in the source."
#
# ICD-10-CM format is a letter (A–Z, except U) + 2 digits + an optional
# decimal point + 1-4 alphanumeric characters. We accept either with or
# without the dot ("I48.91" and "I4891" should both validate when either
# form appears in the source). The guardrail does NOT enforce the format
# itself — that's the schema's job — only the literal-grounding check.

_ICD10_DOT_RE = re.compile(r"\.")


def validate_icd10_grounded(
    icd10_code: str,
    source_text: str,
) -> bool:
    """Return True iff ``icd10_code`` appears literally in ``source_text``.

    Case-insensitive; whitespace-tolerant; dot-optional. Specifically:

    * The check normalizes both sides to uppercase + collapsed whitespace
      so "i48.91" matches "I48.91" or "I48.91 " or "I48.91\\n".
    * Either form (with or without the embedded period) of the code is
      accepted: ``I48.91`` and ``I4891`` are treated as equivalent.
      Source documents print one form or the other; the LLM can extract
      either; we accept either.
    * Returns ``False`` for empty / non-string input rather than raising.

    Used pre-staging by :func:`apply_icd10_guardrail` to drop fabricated
    codes from the extraction's ``problem_list``.
    """
    if not isinstance(icd10_code, str) or not isinstance(source_text, str):
        return False
    code = icd10_code.strip().upper()
    if not code:
        return False
    haystack_upper = source_text.upper()
    # Whitespace-tolerant: collapse runs in the haystack so we don't miss
    # a code split across a line break.
    haystack_collapsed = re.sub(r"\s+", " ", haystack_upper)
    if code in haystack_collapsed:
        return True
    # Dot-optional: also accept when both sides agree after stripping the
    # decimal points so "I4891" matches "I48.91" (and vice versa).
    code_alt = _ICD10_DOT_RE.sub("", code)
    haystack_alt = _ICD10_DOT_RE.sub("", haystack_collapsed)
    return code_alt in haystack_alt


def _icd10_appears_malformed(icd10_code: str) -> bool:
    """Heuristic — flag obvious schema-violating shapes for the
    ``malformed`` reason label without rejecting borderline cases.

    ICD-10-CM canonical: 1 letter (A-Z) + 2 digits + optional ``.`` +
    1-4 alphanumerics. We accept I48, I48.91, I4891, M62.81; we reject
    obviously broken values (empty, all-letters, all-digits, contains
    spaces). Used only for metric attribution — the literal-grounding
    check is the actual gate.
    """
    if not isinstance(icd10_code, str):
        return True
    code = icd10_code.strip()
    if not code:
        return True
    # ICD-10 is always letter-prefixed; pure-digit "9999" is malformed.
    if code[0].isdigit():
        return True
    # Spaces / multi-token strings can't be a single ICD-10 code.
    if " " in code or "\t" in code:
        return True
    return False


def apply_icd10_guardrail(
    problem_list_items: List[Any],
    source_text: str,
    *,
    document_reference_id: str | None = None,
    request_id: str | None = None,
) -> List[Any]:
    """Walk ``problem_list_items``; drop fabricated ICD-10 codes in place.

    For each item with a non-null ``icd10_code``:

    * If the code grounds via :func:`validate_icd10_grounded`, leave it.
    * Otherwise null the field, increment
      ``agent_icd10_guardrail_rejections_total{reason}``, and emit one
      structured ``icd10_guardrail_rejected`` log line. Reason is
      ``malformed`` for shape-violating codes; ``not_in_source`` for
      shape-OK-but-hallucinated codes; ``other`` for the residual case
      (we never use this label today, but keep the dimension stable
      for future taxonomy expansion).

    Returns the (mutated) ``problem_list_items`` list. The condition,
    onset_date, status, and citations are preserved — only the code
    field is touched.
    """
    if not problem_list_items:
        return problem_list_items
    for item in problem_list_items:
        code = getattr(item, "icd10_code", None)
        if code is None:
            continue
        if not isinstance(code, str) or not code.strip():
            continue
        if validate_icd10_grounded(code, source_text or ""):
            continue
        reason = "malformed" if _icd10_appears_malformed(code) else "not_in_source"
        try:
            agent_icd10_guardrail_rejections_total.labels(reason=reason).inc()
        except Exception:  # pragma: no cover — metrics are best-effort
            pass
        condition_preview = (getattr(item, "condition", "") or "")[:48]
        logger.warning(
            "icd10_guardrail_rejected",
            extra={
                "request_id": request_id,
                "document_reference_id": document_reference_id,
                "rejected_code": code,
                "condition_preview": condition_preview,
                "reason": reason,
            },
        )
        # Pydantic model_copy keeps the rest of the fields intact and
        # produces a new instance; assign back so the caller's list
        # carries the mutated row.
        try:
            mutated = item.model_copy(update={"icd10_code": None})
        except Exception:
            # Defensive: if model_copy fails (non-pydantic shape), fall
            # back to attribute assignment. ProblemListItem is pydantic
            # so this branch is theoretical.
            try:
                item.icd10_code = None  # type: ignore[attr-defined]
                continue
            except Exception:
                continue
        idx = problem_list_items.index(item)
        problem_list_items[idx] = mutated
    return problem_list_items


# ── Family-history column-shift guardrail ──────────────────────────────
#
# Failure mode (observed on Reyes p03 intake): the LLM puts a
# living-status word ("Alive", "Living", "Deceased") into the
# ``age_at_onset`` slot of a FamilyHistoryItem because the source
# document's columns are aligned in a way that confuses the vision
# model. ``age_at_onset`` is supposed to carry a numeric / year value
# (or "Unknown"); ``status`` is the slot for living-status text.
#
# This guardrail walks ``family_history``; for any row whose
# ``age_at_onset`` matches a living-status word AND whose ``status`` is
# empty, it moves the value over and clears ``age_at_onset``. Idempotent.

_LIVING_STATUS_RE = re.compile(
    r"^\s*(alive|living|deceased|dead)\s*$",
    re.IGNORECASE,
)


def apply_family_history_status_guardrail(
    family_history_items: List[Any],
    *,
    document_reference_id: str | None = None,
) -> List[Any]:
    """Repair the ``age_at_onset`` ↔ ``status`` column-shift bug in place.

    For each FamilyHistoryItem whose ``age_at_onset`` is a bare
    living-status word (``Alive``, ``Living``, ``Deceased``, ``Dead``;
    case-insensitive; whitespace tolerant), and whose ``status`` is
    empty / missing, move the word into ``status`` and null out
    ``age_at_onset``. All other fields (relation, condition, citations,
    snomed_code) are preserved.

    Returns the (mutated) list of items.
    """
    if not family_history_items:
        return family_history_items
    for item in family_history_items:
        age = getattr(item, "age_at_onset", None)
        if not isinstance(age, str) or not age.strip():
            continue
        if not _LIVING_STATUS_RE.match(age):
            continue
        existing_status = getattr(item, "status", None)
        if isinstance(existing_status, str) and existing_status.strip():
            # Status already populated — just clear the bad age_at_onset.
            try:
                mutated = item.model_copy(update={"age_at_onset": None})
            except Exception:
                continue
        else:
            try:
                mutated = item.model_copy(
                    update={"status": age.strip(), "age_at_onset": None},
                )
            except Exception:
                continue
        idx = family_history_items.index(item)
        family_history_items[idx] = mutated
        logger.info(
            "family_history_column_shift_repaired",
            extra={
                "document_reference_id": document_reference_id,
                "moved_value": age.strip(),
                "relation": getattr(item, "relation", None),
                "condition": getattr(item, "condition", None),
            },
        )
    return family_history_items


def _value_in_block(value: str, block_text: str) -> bool:
    """True iff `block_text` overlaps `value` above the floor.

    Used by the kept-branch of :func:`_repoint_citation` to decide
    whether the LLM's already-cited block contains the value. Without
    the floor, any substring containment counts — so a tiny block whose
    text happens to appear inside a long value (e.g. a 5-char "weeks"
    block hit by a 100-char chief-concern paragraph) was wrongly
    accepted as the citation target. We share the same overlap formula
    as :func:`_candidate_blocks_for_value` to keep the kept-branch and
    search-branch decisions symmetric.
    """
    nv = _normalize_for_match(value)
    nb = _normalize_for_match(block_text)
    if not nv or not nb:
        return False
    if nv in nb:
        overlap = len(nv)
    elif nb in nv:
        overlap = len(nb)
    else:
        return False
    return overlap >= _min_overlap_floor(len(nv))


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
    # Problem-list / PMH section labels seen in real intake forms.
    # ``CONDITION`` is the column header most extractors return as
    # ``nearest_label`` for individual rows; the broader section terms
    # cover whole-block anchors.
    "problem_list": ("PROBLEM", "CONDITION", "DIAGNOS", "PMH", "PAST MEDICAL", "MEDICAL HISTORY"),
}


def _min_overlap_floor(nv_len: int) -> int:
    """Compute the minimum normalized-overlap a candidate must clear.

    The floor combines two guards:

    - Legacy short-value preservation: ``min(4, max(1, nv_len // 2))``
      so 1-2-character normalized values still match (e.g. 1-letter sex,
      2-character state codes). This is the pre-2026-05 behavior.
    - Long-value ratio guard: ``ceil(0.30 * nv_len)`` so a long value
      cannot be grounded by a tiny block whose text just happens to be
      a 5-character substring (the chief-concern → "weeks" failure
      mode at id #382 of copilot:429).

    The effective floor is the MAX of the two — the legacy floor still
    handles short values; the ratio guard activates once the value is
    long enough that 30% > the legacy floor (≈ 14 normalized chars and
    up).
    """
    legacy_floor = min(4, max(1, nv_len // 2))
    ratio_floor = (nv_len * 30 + 99) // 100  # ceil(0.30 * nv_len)
    return max(legacy_floor, ratio_floor)


def _candidate_blocks_for_value(
    value: str, blocks: List[LayoutBlock]
) -> List[Tuple[LayoutBlock, int]]:
    """Return all blocks whose normalized text overlaps `value` above the
    minimum-length floor, paired with the overlap length. See
    :func:`_min_overlap_floor` for the floor formula."""
    nv = _normalize_for_match(value)
    if not nv:
        return []
    min_overlap = _min_overlap_floor(len(nv))
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


def _bbox_union(
    bboxes: List[Tuple[float, float, float, float]],
) -> Tuple[float, float, float, float]:
    """Tight axis-aligned union of ≥1 (x, y, w, h) rectangles."""
    if not bboxes:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [b[0] for b in bboxes]
    ys = [b[1] for b in bboxes]
    rights = [b[0] + b[2] for b in bboxes]
    bottoms = [b[1] + b[3] for b in bboxes]
    x = min(xs)
    y = min(ys)
    w = max(rights) - x
    h = max(bottoms) - y
    return (float(x), float(y), float(w), float(h))


def _spans_for_value(
    value: str, blocks: List[LayoutBlock]
) -> List[Tuple[List[LayoutBlock], int]]:
    """Find y-adjacent runs of blocks on a single page whose concatenated
    text contains `value` (or vice-versa) above the
    :func:`_min_overlap_floor` threshold.

    Used by the repointer when no single block clears the floor — the
    canonical case is a multi-line chief concern paragraph that spans 2-3
    OCR line blocks. Returns ``[(run, overlap), ...]`` so the caller can
    pick the best run (smallest, highest-overlap) and synthesize a
    merged bbox.

    Y-adjacency rule: each consecutive pair must have a vertical gap less
    than ``2 × max(height(run))`` so we don't bridge paragraphs separated
    by white space the OCR happens to have seen. Maximum run length is
    capped at 5 blocks — clinical paragraphs rarely exceed that.
    """
    nv = _normalize_for_match(value)
    if not nv:
        return []
    min_overlap = _min_overlap_floor(len(nv))
    by_page: dict[int, List[LayoutBlock]] = {}
    for b in blocks:
        # Only consider line-granularity blocks for span concatenation —
        # word blocks would explode the search space for no benefit (a
        # multi-word value already matches the parent line block).
        if not _is_line_granularity(b):
            continue
        by_page.setdefault(b.page, []).append(b)
    out: List[Tuple[List[LayoutBlock], int]] = []
    for page_blocks in by_page.values():
        sorted_blocks = sorted(page_blocks, key=_block_centroid_y)
        n = len(sorted_blocks)
        for i in range(n):
            heights: List[float] = []
            for j in range(i + 1, min(i + 6, n + 1)):
                run = sorted_blocks[i:j]
                heights = [_block_height(b) for b in run]
                # Y-adjacency: each consecutive pair's gap (top of next
                # minus bottom of prev) must be < 2x max height in run.
                ok = True
                for k in range(len(run) - 1):
                    prev = run[k]
                    nxt = run[k + 1]
                    gap = nxt.bbox[1] - (prev.bbox[1] + prev.bbox[3])
                    if gap > 2.0 * max(heights):
                        ok = False
                        break
                if not ok:
                    continue
                if len(run) < 2:
                    # Single-block runs are handled by
                    # _candidate_blocks_for_value; skip here.
                    continue
                concat = "\n".join(b.text or "" for b in run)
                nb = _normalize_for_match(concat)
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
                out.append((list(run), overlap))
    return out


def _synthesize_span_block(
    run: List[LayoutBlock],
) -> LayoutBlock:
    """Materialize a synthetic LayoutBlock representing a multi-line span.

    Bbox is the tight union of the run's per-block bboxes. Text is the
    run's per-block text concatenated with newlines (used purely for
    citation `quote_or_value`; downstream consumers don't re-parse it).
    Granularity inherits the run's first block (LINE for our use case).
    `bbox_id` is suffixed with ``-spanN`` so logs make the span origin
    obvious; consumers that look up the id in the original block_index
    should fall back gracefully (the sites that do this all
    short-circuit on `is None`).
    """
    if not run:
        raise ValueError("_synthesize_span_block called with empty run")
    head = run[0]
    union = _bbox_union([b.bbox for b in run])
    text = "\n".join(b.text or "" for b in run)
    # Average ocr_confidence; fall back to head if anything goes wrong.
    try:
        confs = [float(getattr(b, "ocr_confidence", 1.0)) for b in run]
        avg_conf = sum(confs) / len(confs) if confs else float(head.ocr_confidence)
    except Exception:  # noqa: BLE001 — defensive, never block the request
        avg_conf = float(head.ocr_confidence)
    span_id = f"{head.bbox_id}-span{len(run)}"
    return LayoutBlock(
        bbox_id=span_id,
        page=head.page,
        bbox=union,
        text=text,
        ocr_confidence=avg_conf,
        granularity=head.granularity,
        polygon=None,
    )


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


def _label_match_score(
    candidate: LayoutBlock,
    blocks: List[LayoutBlock],
    nearest_label: Optional[str],
) -> float:
    """Score (0.0–1.0) how well ``nearest_label`` is grounded in the
    candidate's spatial neighborhood. Returns 0.0 when no label given.

    Heuristic: a hit if the normalized label appears as a substring in
    the candidate's own text OR in any block within 200pt euclidean
    distance of the candidate's centroid on the same page. Returns 1.0
    on hit, 0.0 otherwise. Kept as a float to leave room for graded
    scoring later without changing the sort-key shape.
    """
    if not nearest_label:
        return 0.0
    nl = _normalize_for_match(nearest_label)
    if not nl or len(nl) < 2:
        return 0.0
    # Candidate's own text first (cheapest).
    if nl in _normalize_for_match(candidate.text):
        return 1.0
    cx = float(candidate.bbox[0]) + float(candidate.bbox[2]) / 2.0
    cy = _block_centroid_y(candidate)
    for b in blocks:
        if b.page != candidate.page:
            continue
        if b.bbox_id == candidate.bbox_id:
            continue
        bx = float(b.bbox[0]) + float(b.bbox[2]) / 2.0
        by = _block_centroid_y(b)
        # Cheap rectilinear gate before sqrt — same 200pt window.
        if abs(bx - cx) > 200.0 or abs(by - cy) > 200.0:
            continue
        dist = ((bx - cx) ** 2 + (by - cy) ** 2) ** 0.5
        if dist > 200.0:
            continue
        if nl in _normalize_for_match(b.text):
            return 1.0
    return 0.0


def _select_best_candidate(
    candidates: List[Tuple[LayoutBlock, int]],
    anchors: List[LayoutBlock],
    field_name: Optional[str],
    *,
    blocks: Optional[List[LayoutBlock]] = None,
    nearest_label: Optional[str] = None,
) -> Tuple[LayoutBlock, Optional[LayoutBlock], float, str, float, bool]:
    """Pick the best candidate block among ≥2 candidates.

    Returns ``(chosen_block, nearest_anchor, y_distance, outcome_label,
    label_score, label_tiebreak_used)``.

    Outcome label is one of:
      ``repointed_with_anchor``                 — anchor-driven selection
      ``repointed_with_anchor_label_tiebreak``  — y-band tied; LLM-supplied
                                                  ``nearest_label`` broke
                                                  the tie
      ``repointed_no_anchor``                   — fell back to overlap

    Wave 2B+: the LLM-supplied ``nearest_label`` participates ONLY as a
    tiebreaker. Sort key is widened from
    ``(|Δy|, -overlap, granularity)`` to
    ``(|Δy|, -label_score, -overlap, granularity)`` so y-band remains the
    primary signal. The label score never overrides a smaller |Δy|.
    """
    blocks_pool: List[LayoutBlock] = blocks if blocks is not None else []
    # Compute (candidate, overlap, anchor, dy, anchor_compatible, label_score) tuples.
    enriched: List[
        Tuple[LayoutBlock, int, Optional[LayoutBlock], float, bool, float]
    ] = []
    for cand, overlap in candidates:
        anchor, dy = _nearest_anchor(cand, anchors)
        compat = anchor is not None and _anchor_text_compatible(anchor, field_name)
        label_score = _label_match_score(cand, blocks_pool, nearest_label)
        enriched.append((cand, overlap, anchor, dy, compat, label_score))

    def _label_tiebreak_used(
        sorted_set: List[Tuple[LayoutBlock, int, Optional[LayoutBlock], float, bool, float]],
    ) -> bool:
        """True iff the chosen candidate's |Δy| equals the runner-up's
        AND the chosen had a strictly higher label_score. This is the
        only situation where the label hint actually changed the outcome.
        """
        if len(sorted_set) < 2 or not nearest_label:
            return False
        head, second = sorted_set[0], sorted_set[1]
        return head[3] == second[3] and head[5] > second[5]

    # Step 1: prefer field-compatible anchored candidates if any exist.
    compat_set = [e for e in enriched if e[4]]
    if compat_set:
        # Smallest |Δy| wins; tiebreak by label_score (desc), then overlap
        # (desc), then LINE granularity.
        compat_set.sort(
            key=lambda e: (
                e[3],
                -e[5],
                -e[1],
                0 if _is_line_granularity(e[0]) else 1,
            )
        )
        chosen, _ov, anchor, dy, _c, label_score = compat_set[0]
        used = _label_tiebreak_used(compat_set)
        outcome = (
            "repointed_with_anchor_label_tiebreak"
            if used
            else "repointed_with_anchor"
        )
        return (chosen, anchor, dy, outcome, label_score, used)

    # Step 2: if any anchor exists at all, prefer the candidate whose
    # nearest anchor is closest, regardless of text content.
    if anchors:
        enriched_sorted = sorted(
            enriched,
            key=lambda e: (
                e[3],
                -e[5],
                -e[1],
                0 if _is_line_granularity(e[0]) else 1,
            ),
        )
        chosen, _ov, anchor, dy, _c, label_score = enriched_sorted[0]
        used = _label_tiebreak_used(enriched_sorted)
        outcome = (
            "repointed_with_anchor_label_tiebreak"
            if used
            else "repointed_with_anchor"
        )
        return (chosen, anchor, dy, outcome, label_score, used)

    # Step 3: no anchors detected — fall back to legacy overlap-then-LINE
    # tiebreak. This preserves pre-2B behavior on documents that have no
    # structural section headers (e.g. plain free-text scans).
    legacy_sorted = sorted(
        enriched,
        key=lambda e: (
            -e[1],
            -e[5],
            0 if _is_line_granularity(e[0]) else 1,
        ),
    )
    chosen, _ov, _a, _dy, _c, label_score = legacy_sorted[0]
    return (chosen, None, float("inf"), "repointed_no_anchor", label_score, False)


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
    chosen, _anchor, _dy, _outcome, _ls, _lt = _select_best_candidate(
        cands, anchor_pool, field_name, blocks=blocks
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

    nearest_label = getattr(cit, "nearest_label", None)
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
                "nearest_label_score": 0.0,
                "nearest_label_used": False,
            },
        )
        return cit.model_copy(
            update={
                "bbox": cited_block.bbox,
                "page": cited_block.page,
                "polygon": _block_polygon_list(cited_block),
            }
        )

    if value:
        cands = _candidate_blocks_for_value(value, blocks)
        # Multi-line span fallback: when no single block fully contains
        # the value (i.e. the best single-block candidate's overlap is
        # strictly less than the normalized value length), look for a
        # contiguous run of LINE blocks whose concatenated text DOES
        # contain the full value. Prefer the smallest such run. This
        # handles the chief-concern paragraph case where the value is
        # split across 2-3 OCR line blocks — without it, the repointer
        # snaps to whichever single line happens to have the most
        # surface overlap (often the middle line) and the bbox covers
        # only that fragment.
        nv_len = len(_normalize_for_match(value))
        best_single_overlap = max((ov for _b, ov in cands), default=0)
        if nv_len > 0 and best_single_overlap < nv_len:
            spans = _spans_for_value(value, blocks)
            full_spans = [s for s in spans if s[1] >= nv_len]
            if full_spans:
                full_spans.sort(key=lambda s: (len(s[0]), -s[1]))
                best_run, best_overlap = full_spans[0]
                span_block = _synthesize_span_block(best_run)
                agent_citation_repoint_total.labels(
                    field=field_name or "unknown", outcome="repointed_span"
                ).inc()
                logger.info(
                    "extractor_citation_repointed",
                    extra={
                        "tool": "intake",
                        "field_name": field_name,
                        "outcome": "repointed_span",
                        "from": cit.field_or_chunk_id,
                        "to": span_block.bbox_id,
                        "candidate_count": len(spans),
                        "chosen_bbox_id": span_block.bbox_id,
                        "chosen_granularity": _block_granularity(span_block),
                        "span_run_size": len(best_run),
                        "span_overlap": best_overlap,
                        "value_preview": (value or "")[:32],
                    },
                )
                return cit.model_copy(
                    update={
                        "field_or_chunk_id": span_block.bbox_id,
                        "quote_or_value": span_block.text or cit.quote_or_value,
                        "bbox": span_block.bbox,
                        "page": span_block.page,
                        "polygon": None,
                    }
                )
        if cands:
            if len(cands) == 1:
                target = cands[0][0]
                outcome = "repointed_no_anchor"
                anchor: Optional[LayoutBlock] = None
                dy = float("inf")
                label_score = _label_match_score(target, blocks, nearest_label)
                label_used = False
            else:
                target, anchor, dy, outcome, label_score, label_used = (
                    _select_best_candidate(
                        cands,
                        anchor_pool,
                        field_name,
                        blocks=blocks,
                        nearest_label=nearest_label,
                    )
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
                    "nearest_label_score": round(label_score, 3),
                    "nearest_label_used": label_used,
                },
            )
            return cit.model_copy(
                update={
                    "field_or_chunk_id": target.bbox_id,
                    "quote_or_value": target.text or cit.quote_or_value,
                    "bbox": target.bbox,
                    "page": target.page,
                    "polygon": _block_polygon_list(target),
                }
            )

        # No single block cleared the floor — try multi-line spans for
        # multi-paragraph values (chief concern, address, long
        # narratives). Pick the smallest span (fewest blocks) that
        # qualifies; ties broken by highest overlap.
        spans = _spans_for_value(value, blocks)
        if spans:
            spans.sort(key=lambda s: (len(s[0]), -s[1]))
            best_run, best_overlap = spans[0]
            span_block = _synthesize_span_block(best_run)
            agent_citation_repoint_total.labels(
                field=field_name or "unknown", outcome="repointed_span"
            ).inc()
            logger.info(
                "extractor_citation_repointed",
                extra={
                    "tool": "intake",
                    "field_name": field_name,
                    "outcome": "repointed_span",
                    "from": cit.field_or_chunk_id,
                    "to": span_block.bbox_id,
                    "candidate_count": len(spans),
                    "chosen_bbox_id": span_block.bbox_id,
                    "chosen_granularity": _block_granularity(span_block),
                    "span_run_size": len(best_run),
                    "span_overlap": best_overlap,
                    "value_preview": (value or "")[:32],
                },
            )
            return cit.model_copy(
                update={
                    "field_or_chunk_id": span_block.bbox_id,
                    "quote_or_value": span_block.text or cit.quote_or_value,
                    "bbox": span_block.bbox,
                    "page": span_block.page,
                    "polygon": None,
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
                "nearest_label_score": 0.0,
                "nearest_label_used": False,
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
    return cit.model_copy(
        update={
            "bbox": cited_block.bbox,
            "page": cited_block.page,
            "polygon": _block_polygon_list(cited_block),
        }
    )


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
    return cit.model_copy(
        update={
            "bbox": block.bbox,
            "page": block.page,
            "polygon": _block_polygon_list(block),
        }
    )


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


def _tighten_bbox_to_value(
    pdf_bytes: Optional[bytes],
    page: int,
    bbox: Tuple[float, float, float, float],
    value: Optional[str],
) -> Optional[Tuple[float, float, float, float]]:
    """Crop a citation bbox to the smallest rectangle covering only the
    words that ground ``value``.

    OCR line-blocks for tabular intake forms aggregate label + value
    horizontally (e.g. one block covers "LEGAL NAME" at x≈75 PLUS
    "Chen, Margaret L." at x≈216, all on the same y-row). Drawing the
    full line bbox makes the citation overlay look "wide on target".
    This helper goes back to the PDF text layer via pymupdf
    ``page.get_text("words")`` and finds the contiguous run of words
    inside the bbox whose concatenated text contains ``value``,
    returning the union of those word rects.

    Returns ``None`` when:

    - ``pdf_bytes`` or ``value`` is empty (DOCX / image paths skip
      tightening),
    - the page is out of range,
    - no word run inside the bbox matches the value above the floor.

    On ``None`` the caller keeps the original bbox.
    """
    if not pdf_bytes or not value:
        return None
    nv = _normalize_for_match(value)
    if not nv:
        return None
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            if page < 1 or page > doc.page_count:
                return None
            words = doc[page - 1].get_text("words")
    except Exception:
        return None
    bx, by, bw, bh = bbox
    bx_max = bx + bw
    by_max = by + bh
    PAD = 2.0
    in_box: List[Tuple[float, float, float, float, str]] = []
    for w in words:
        if len(w) < 5:
            continue
        x0, y0, x1, y1, text = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
        if y1 < by - PAD or y0 > by_max + PAD:
            continue
        if x1 < bx - PAD or x0 > bx_max + PAD:
            continue
        in_box.append((x0, y0, x1, y1, text))
    if not in_box:
        return None
    # Word-membership filter: keep only words whose normalized text
    # appears as a substring of nv. This survives multi-column layouts
    # where contiguous reading-order would interleave the value's
    # words with adjacent-column labels (e.g. LEGAL NAME / Chen, /
    # DATE OF BIRTH / 1967-08-14 all on the same y-row).
    norm_words = [_normalize_for_match(w[4]) for w in in_box]
    selected: List[Tuple[float, float, float, float, str]] = []
    sel_norm: List[str] = []
    for w, nw in zip(in_box, norm_words):
        if not nw:
            continue
        if nw in nv:
            selected.append(w)
            sel_norm.append(nw)
    if not selected:
        return None
    # Coverage check: the concatenated normalized text of the kept
    # words must reach the floor relative to the value. Without this,
    # a 1-letter word coincidence (e.g. value contains "L." and the
    # row's LIVING column also has an "L") would tighten the bbox to
    # an unrelated single character.
    coverage = sum(len(s) for s in sel_norm)
    floor = _min_overlap_floor(len(nv))
    if coverage < floor:
        return None
    xs = [w[0] for w in selected]
    ys = [w[1] for w in selected]
    rights = [w[2] for w in selected]
    bottoms = [w[3] for w in selected]
    x = min(xs)
    y = min(ys)
    return (float(x), float(y), float(max(rights) - x), float(max(bottoms) - y))


def _maybe_tighten(
    cit: Citation,
    value: Optional[str],
    pdf_bytes: Optional[bytes],
) -> Citation:
    """Apply :func:`_tighten_bbox_to_value` to the chosen citation when a
    PDF is available. Returns the original citation when tightening is
    a no-op or the PDF source is missing."""
    if not pdf_bytes:
        return cit
    bbox = cit.bbox
    page = cit.page
    if bbox is None or page is None:
        return cit
    if len(bbox) != 4:
        return cit
    tight = _tighten_bbox_to_value(
        pdf_bytes, int(page), tuple(bbox), value,  # type: ignore[arg-type]
    )
    if tight is None:
        return cit
    return cit.model_copy(update={"bbox": tight})


def _tighten_citations_list(
    citations: List[Citation],
    value: Optional[str],
    pdf_bytes: Optional[bytes],
) -> List[Citation]:
    return [_maybe_tighten(c, value, pdf_bytes) for c in citations]


def _hydrate_intake_form_citations(
    form: IntakeForm,
    blocks: List[LayoutBlock],
    pdf_bytes: Optional[bytes] = None,
) -> IntakeForm:
    """Walk every cite-bearing IntakeForm field and stamp bbox/page.

    When ``pdf_bytes`` is supplied (PDF/PNG ingest path), an additional
    bbox-tightening step crops each chosen line-level bbox down to just
    the words that ground the value. DOCX / synthetic paths pass
    ``None`` and skip tightening.
    """
    block_index = _index_blocks(blocks)
    anchors = _detect_section_anchors(blocks)

    # Demographics — TextField sub-fields each carry citations.
    demographics = form.demographics
    if demographics is not None:
        demo_updates: dict[str, Any] = {}
        for attr in ("name", "dob", "sex", "mrn", "address"):
            tf = getattr(demographics, attr, None)
            if tf is not None:
                cits = _repoint_citations_list(
                    tf.citations,
                    tf.value,
                    blocks,
                    block_index,
                    field_name=attr,
                    anchors=anchors,
                )
                cits = _tighten_citations_list(cits, tf.value, pdf_bytes)
                demo_updates[attr] = tf.model_copy(update={"citations": cits})
        if demo_updates:
            demographics = demographics.model_copy(update=demo_updates)

    chief = form.chief_concern
    if chief is not None:
        cits = _repoint_citations_list(
            chief.citations,
            chief.value,
            blocks,
            block_index,
            field_name="chief_concern",
            anchors=anchors,
        )
        cits = _tighten_citations_list(cits, chief.value, pdf_bytes)
        chief = chief.model_copy(update={"citations": cits})

    meds = []
    for m in form.current_medications:
        cits = _repoint_citations_list(
            m.citations,
            m.name,
            blocks,
            block_index,
            field_name="medication",
            anchors=anchors,
        )
        cits = _tighten_citations_list(cits, m.name, pdf_bytes)
        meds.append(m.model_copy(update={"citations": cits}))
    allergies = []
    for a in form.allergies:
        cits = _repoint_citations_list(
            a.citations,
            a.substance,
            blocks,
            block_index,
            field_name="allergy",
            anchors=anchors,
        )
        cits = _tighten_citations_list(cits, a.substance, pdf_bytes)
        allergies.append(a.model_copy(update={"citations": cits}))
    fam = []
    for f in form.family_history:
        cits = _repoint_citations_list(
            f.citations,
            f.condition,
            blocks,
            block_index,
            field_name="family",
            anchors=anchors,
        )
        cits = _tighten_citations_list(cits, f.condition, pdf_bytes)
        fam.append(f.model_copy(update={"citations": cits}))
    problems = []
    for pl in form.problem_list:
        cits = _repoint_citations_list(
            pl.citations,
            pl.condition,
            blocks,
            block_index,
            field_name="problem_list",
            anchors=anchors,
        )
        cits = _tighten_citations_list(cits, pl.condition, pdf_bytes)
        problems.append(pl.model_copy(update={"citations": cits}))
    code_status = form.code_status
    if code_status is not None:
        cits = _hydrate_citations_list(code_status.citations, block_index)
        cits = _tighten_citations_list(cits, code_status.value, pdf_bytes)
        code_status = code_status.model_copy(update={"citations": cits})

    return form.model_copy(
        update={
            "demographics": demographics,
            "chief_concern": chief,
            "current_medications": meds,
            "allergies": allergies,
            "family_history": fam,
            "problem_list": problems,
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
                temperature=0,
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
        pdf_bytes=pdf_bytes,
    )
    # ICD-10 hallucination guardrail — must run BEFORE the dispatcher
    # stages anything. Walks final.problem_list and drops any
    # icd10_code that doesn't ground literally in the OCR layout's
    # source text. The source-text composition mirrors what the eval
    # rubric uses (concatenated block text); guardrail and rubric
    # share the same predicate (validate_icd10_grounded).
    if final.problem_list:
        source_text = "\n".join((b.text or "") for b in blocks)
        # apply_icd10_guardrail mutates the list in place AND returns
        # it; we re-bind via model_copy so the rest of the form is
        # untouched. Pydantic v2 frozen-ish handling: the list itself
        # is mutable, but reassigning through model_copy keeps the
        # IntakeForm instance immutable in the right places.
        guarded = apply_icd10_guardrail(
            list(final.problem_list),
            source_text,
            document_reference_id=document_reference_id,
        )
        final = final.model_copy(update={"problem_list": guarded})

    # Family-history column-shift repair — moves "Alive"/"Living"/
    # "Deceased" out of age_at_onset and into status.
    if final.family_history:
        repaired = apply_family_history_status_guardrail(
            list(final.family_history),
            document_reference_id=document_reference_id,
        )
        final = final.model_copy(update={"family_history": repaired})

    logger.info(
        "extractor_intake_ok",
        extra={
            "document_reference_id": document_reference_id,
            "n_meds": len(final.current_medications),
            "n_allergies": len(final.allergies),
            "n_problems": len(final.problem_list),
            "classifier_confidence": final.classifier_confidence,
        },
    )
    return final


# --------------------------------------------------------------------------- #
# Phase 9 Slice 9.6 — DOCX prose-mode branch
# --------------------------------------------------------------------------- #
#
# Vision-mode (above) feeds Claude per-page PNGs + an OCR layout JSON. DOCX
# has no images and no bboxes — only paragraphs and runs. The prose-mode
# branch swaps:
#
#   _render_pages_to_png          → dropped (no image blocks)
#   _build_user_content           → _build_user_content_prose
#   prompt key "intake_form"      → "intake_form_prose"
#
# Same ``_MODEL_CANDIDATES`` chain, same ``IntakeForm`` schema, same
# ``submit_intake_form`` tool. The vision branch is unchanged for
# back-compat (50 W2 eval cases reference its bbox_id contract).


_PROMPT_PROSE = get_prompt("intake_form_prose")


def _paragraphs_to_prompt_json(paragraphs: List[DocxParagraph]) -> str:
    """Serialize paragraphs into the JSON the prose prompt expects.

    Each entry carries the synthetic locator (``para=N`` for paragraph,
    plus the per-run locator), the section name resolved by the leading-
    bold forward pass, the paragraph style, and the rendered text. Empty
    paragraphs are omitted — they're noise for the LLM and a hallucination
    surface."""
    out: list[dict[str, Any]] = []
    for para in paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        runs_payload = [
            {
                "locator": format_locator(para.para_idx, r.run_idx),
                "run_idx": r.run_idx,
                "text": r.text,
                "bold": r.bold,
            }
            for r in para.runs
            if (r.text or "").strip()
        ]
        out.append(
            {
                "locator": format_locator(para.para_idx),
                "para_idx": para.para_idx,
                "section": para.section,
                "style": para.style,
                "text": text,
                "runs": runs_payload,
            }
        )
    return json.dumps(out, ensure_ascii=False)


def _build_user_content_prose(
    paragraphs: List[DocxParagraph],
    patient_id: str,
    document_reference_id: str,
    *,
    tracked_changes_present: bool = False,
) -> List[dict[str, Any]]:
    """Build the Claude user-content list for DOCX prose-mode extraction.

    Sibling of ``_build_user_content``. NO image blocks (DOCX has no
    images). One text block carrying the paragraph + run JSON, the
    patient_id / document_reference_id / current UTC, and a
    ``tracked_changes_present`` soft-warn flag when relevant.
    """
    body = (
        f"patient_id = {patient_id}\n"
        f"document_reference_id = {document_reference_id}\n"
        f"current_utc = {datetime.now(timezone.utc).isoformat()}\n"
        f"tracked_changes_present = {str(tracked_changes_present).lower()}\n\n"
        "DOCX paragraph + run JSON (the only source of truth for "
        "para=N|run=M locators):\n"
        f"{_paragraphs_to_prompt_json(paragraphs)}"
    )
    return [{"type": "text", "text": body}]


async def _call_claude_extract_prose(
    client: anthropic.AsyncAnthropic,
    user_content: List[dict[str, Any]],
) -> dict[str, Any]:
    """Sibling of ``_call_claude_extract`` using the prose-mode system prompt."""
    tool = {
        "name": "submit_intake_form",
        "description": (
            "Submit the structured IntakeForm extracted from the DOCX prose."
        ),
        "input_schema": IntakeForm.model_json_schema(),
    }
    last_err: Exception | None = None
    for model in _MODEL_CANDIDATES:
        try:
            t0 = time.monotonic()
            resp = await client.messages.create(
                model=model,
                max_tokens=4096,
                temperature=0,
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_intake_form"},
                system=_PROMPT_PROSE,
                messages=[{"role": "user", "content": user_content}],
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "extractor_claude_call_ok",
                extra={
                    "model": model,
                    "duration_ms": duration_ms,
                    "tool": "intake_prose",
                },
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
                extra={"model": model, "tool": "intake_prose"},
            )
        except anthropic.NotFoundError as e:
            last_err = e
            logger.warning(
                "extractor_claude_model_unavailable",
                extra={"model": model, "tool": "intake_prose"},
            )
            continue
        except Exception as e:  # noqa: BLE001 — boundary, re-wrapped below
            last_err = e
            logger.error(
                "extractor_claude_call_failed",
                extra={
                    "model": model,
                    "tool": "intake_prose",
                    "error_type": type(e).__name__,
                },
            )
            break

    raise ExtractionFailed("vision call failed") from last_err


async def extract_intake_from_docx(
    docx_bytes: bytes,
    *,
    patient_id: str,
    document_reference_id: str,
) -> IntakeForm | UnknownDocument:
    """Run the DOCX prose-mode intake extractor.

    Mirrors ``extract_intake`` but drives off ``extract_docx_paragraphs``
    instead of ``extract_layout`` (which would lose the run granularity)
    and skips the vision content blocks entirely. Returns the same
    ``IntakeForm | UnknownDocument`` discriminated union the vision
    branch returns; raises ``ExtractionFailed`` only on terminal LLM
    failure or schema rejection.

    Edge cases:
      - Empty paragraph list → ``UnknownDocument`` summary stub, no LLM call.
      - Tracked changes detected → soft-warn flag passed into the prompt
        (``docx_tracked_changes_present`` in the user content) so the
        downstream critic can surface the warning without us having to
        re-parse the OOXML.
      - Embedded images: dropped at the loader level; per-image
        ``docx_image_dropped`` log already emitted.
    """
    paragraphs, meta = extract_docx_paragraphs(docx_bytes)
    if not paragraphs:
        logger.error(
            "extractor_no_layout_blocks",
            extra={
                "document_reference_id": document_reference_id,
                "tool": "intake_prose",
            },
        )
        # Return UnknownDocument rather than raise — empty DOCX is a
        # degraded but non-terminal state (some referrals arrive empty
        # from the fax stack).
        return UnknownDocument(
            kind="unknown",
            schema_version="1.0",
            patient_id=patient_id,
            document_reference_id=document_reference_id,
            document_kind_guess="docx_empty",
            summary="DOCX contained no extractable paragraphs.",
            key_facts=[
                KeyFact(
                    text="(empty document)",
                    citations=[
                        Citation(
                            source_type="document",
                            source_id=document_reference_id,
                            page_or_section="prose",
                            field_or_chunk_id=format_locator(1),
                            quote_or_value="(empty)",
                        )
                    ],
                )
            ],
            classifier_confidence=0.0,
            ocr_confidence_range=(1.0, 1.0),
            extracted_at=datetime.now(timezone.utc),
        )

    client = anthropic.AsyncAnthropic()
    user_content = _build_user_content_prose(
        paragraphs,
        patient_id,
        document_reference_id,
        tracked_changes_present=bool(meta.get("tracked_changes_present")),
    )
    tool_input = await _call_claude_extract_prose(client, user_content)

    try:
        form = IntakeForm.model_validate_json(json.dumps(tool_input))
    except Exception as e:  # noqa: BLE001 — boundary
        logger.error(
            "extractor_validation_failed",
            extra={
                "document_reference_id": document_reference_id,
                "error_type": type(e).__name__,
                "tool": "intake_prose",
            },
        )
        raise ExtractionFailed("vision call failed") from e

    # Force the structural fields the schema requires that the LLM may
    # not have populated (vision mode hydrates these from layout
    # confidence; prose mode has no OCR so we hardcode 1.0).
    final = form.model_copy(
        update={
            "patient_id": patient_id,
            "document_reference_id": document_reference_id,
            "ocr_confidence_range": (1.0, 1.0),
            # If the LLM didn't set classifier_confidence, fall back to a
            # mid-band default — the prose extractor has no classifier
            # tier (classifier_confidence is a vision-mode artifact).
            "classifier_confidence": (
                form.classifier_confidence
                if form.classifier_confidence > 0.0
                else 0.85
            ),
        }
    )
    # ICD-10 hallucination guardrail — same as the vision/PDF path.
    # Source text for DOCX is the concatenation of every paragraph's
    # rendered text; ICD-10 codes printed inside table cells survive
    # the loader (extract_docx_paragraphs flattens table-cell prose
    # into the same paragraph stream).
    if final.problem_list:
        source_text = "\n".join((p.text or "") for p in paragraphs)
        guarded = apply_icd10_guardrail(
            list(final.problem_list),
            source_text,
            document_reference_id=document_reference_id,
        )
        final = final.model_copy(update={"problem_list": guarded})

    # Family-history column-shift repair (prose path) — same fix as the
    # vision path. The prose extractor sees the same column-shift bug
    # when the source narrates "Mother (Alive)" inline.
    if final.family_history:
        repaired = apply_family_history_status_guardrail(
            list(final.family_history),
            document_reference_id=document_reference_id,
        )
        final = final.model_copy(update={"family_history": repaired})

    logger.info(
        "extractor_intake_prose_ok",
        extra={
            "document_reference_id": document_reference_id,
            "n_meds": len(final.current_medications),
            "n_allergies": len(final.allergies),
            "n_problems": len(final.problem_list),
            "n_paragraphs": meta.get("n_paragraphs"),
            "tracked_changes_present": meta.get("tracked_changes_present"),
            "embedded_images_dropped": meta.get("embedded_images_dropped"),
        },
    )
    return final
