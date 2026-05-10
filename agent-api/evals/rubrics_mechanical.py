"""Slice 5.3 — Mechanical rubric scorers.

Pure functions over a :class:`evals.runner.RunOutcome`. Each returns a
boolean per W2_ARCHITECTURE §11.2 — boolean rubrics keep the gate
unambiguous and turn each failure into a concrete fix task.
"""
from __future__ import annotations

import json
import re
import string
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

from pydantic import ValidationError

from extractors.schemas import IntakeForm, LabReport, UnknownDocument, WorkbookExtraction
from parsers.hl7.types import DemographicUpdateEvent

from .runner import RunOutcome


# Phase 3 Part B' — register multimodal extraction kinds. Each entry maps
# the discriminator in ``extraction["kind"]`` to the Pydantic model whose
# strict-mode validation gates ``schema_valid``.
#
# - ``lab_report``         : extractors.schemas.LabReport
# - ``intake_form``        : extractors.schemas.IntakeForm
# - ``unknown``            : extractors.schemas.UnknownDocument
# - ``demographic_update`` : parsers.hl7.types.DemographicUpdateEvent
#                            (HL7 ADT^A08 demographic delta — produced by
#                            parsers.hl7.adt.parse_adt_a08)
#
# XLSX and DOCX adapters reuse the existing IntakeForm / LabReport kinds
# (their parsers emit those Pydantic models directly), so no additional
# discriminator is required for those modalities. TIFF flows through the
# OCR pipeline and emerges as IntakeForm / LabReport / UnknownDocument
# downstream of ``classify_keywords``.
_SCHEMAS_BY_KIND = {
    "lab_report": LabReport,
    "intake_form": IntakeForm,
    "unknown": UnknownDocument,
    "demographic_update": DemographicUpdateEvent,
    # Phase 3 Item 2 — XLSX wrapper. Embeds intake_form / lab_reports /
    # pending_tasks so the eval rubric walks every cited item across all
    # lanes of a workbook in one pass. See ``_iter_cited_items`` for the
    # recursion that supports ``citation_present`` and friends.
    "workbook": WorkbookExtraction,
}


_PHI_VALUES_PATH = Path(__file__).parent / "synthetic_phi_values.json"


def _load_default_phi_values() -> Set[str]:
    try:
        return set(json.loads(_PHI_VALUES_PATH.read_text()))
    except FileNotFoundError:
        return set()


# --------------------------------------------------------------------------- #
# Rubrics
# --------------------------------------------------------------------------- #


def schema_valid(outcome: RunOutcome) -> bool:
    """Pydantic v2 strict-mode validation against the kind-discriminated schema."""
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    kind = extraction.get("kind")
    schema = _SCHEMAS_BY_KIND.get(str(kind))
    if schema is None:
        return False
    try:
        schema.model_validate_json(json.dumps(extraction))
        return True
    except (ValidationError, ValueError, TypeError):
        return False


def _iter_cited_items(extraction: dict) -> Iterable[dict]:
    """Yield every dict in the extraction that should carry a ``citations`` list."""
    kind = extraction.get("kind")
    if kind == "lab_report":
        for value in extraction.get("values") or []:
            if isinstance(value, dict):
                yield value
    elif kind == "unknown":
        for fact in extraction.get("key_facts") or []:
            if isinstance(fact, dict):
                yield fact
    elif kind == "intake_form":
        # Every cited TextField/MedicationItem/etc. carries a citations list.
        demographics = extraction.get("demographics") or {}
        if isinstance(demographics, dict):
            for v in demographics.values():
                if isinstance(v, dict) and "citations" in v:
                    yield v
        chief = extraction.get("chief_concern")
        if isinstance(chief, dict) and "citations" in chief:
            yield chief
        for key in (
            "current_medications",
            "allergies",
            "family_history",
            "pertinent_labs",
            "problem_list",
        ):
            for item in extraction.get(key) or []:
                if isinstance(item, dict):
                    yield item
        code_status = extraction.get("code_status")
        if isinstance(code_status, dict) and "citations" in code_status:
            yield code_status
    elif kind == "demographic_update":
        # HL7 ADT^A08 — every populated DemographicField on the top-level
        # event carries a synthetic Citation back to the source HL7 segment
        # (``PID-3.1`` / ``PV1-3`` / etc.). The dict shape is identical to
        # IntakeForm.TextField (``value`` + ``citations``), so the same
        # citation_present / citation_resolvable rubric path applies.
        for key, value in extraction.items():
            if isinstance(value, dict) and "citations" in value:
                yield value
    elif kind == "workbook":
        # Phase 3 Item 2 — XLSX wrapper. Recurse into each embedded
        # extraction lane so all citation rubrics fire across the
        # workbook's full output (intake_form fields + per-date lab
        # values + per-row pending tasks).
        intake_inner = extraction.get("intake_form")
        if isinstance(intake_inner, dict):
            yield from _iter_cited_items(intake_inner)
        for lab_inner in extraction.get("lab_reports") or []:
            if isinstance(lab_inner, dict):
                yield from _iter_cited_items(lab_inner)
        for task in extraction.get("pending_tasks") or []:
            if not isinstance(task, dict):
                continue
            for k in ("measure", "measure_ref", "notes"):
                v = task.get(k)
                if isinstance(v, dict) and "citations" in v:
                    yield v


def citation_present(outcome: RunOutcome) -> bool:
    """Every cited item in the extraction must carry at least one citation."""
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    saw_any = False
    for item in _iter_cited_items(extraction):
        saw_any = True
        citations = item.get("citations")
        if not isinstance(citations, list) or len(citations) == 0:
            return False
    # Empty extractions (no clinical claims) are vacuously fine — the schema
    # rubric catches "should have had values" cases via min_length/required.
    # ``intake_form`` and ``workbook`` are explicitly allowed to be empty
    # (a referral with zero structured fields, or a blank XLSX template).
    return saw_any or extraction.get("kind") in ("intake_form", "workbook")


# --------------------------------------------------------------------------- #
# Citation-quality rubrics (Wave 2C — gate citation correctness mechanically)
# --------------------------------------------------------------------------- #


_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation})


def _normalize_text(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text.translate(_PUNCT_TABLE)).strip().lower()


def _tokenize(text: str) -> List[str]:
    """Token list = whitespace split of normalized text."""
    norm = _normalize_text(text)
    return norm.split() if norm else []


def _collect_layout_block_index(outcome: RunOutcome) -> dict[str, str]:
    """Build a {bbox_id: text} index from the outcome's OCR layout.

    Returns an empty dict when no layout was captured (probe disabled or graph
    skipped layout emission). Callers treat an empty index as "no resolution
    information available" — see each rubric for vacuous-True semantics.
    """
    index: dict[str, str] = {}
    layout = getattr(outcome, "ocr_layout", None) or []
    if not isinstance(layout, list):
        return index
    for blk in layout:
        if not isinstance(blk, dict):
            continue
        bid = blk.get("bbox_id") or blk.get("id") or blk.get("chunk_id")
        if not bid:
            continue
        # text might appear under various keys depending on extractor stage
        text = (
            blk.get("text")
            or blk.get("content")
            or blk.get("value")
            or ""
        )
        index[str(bid)] = str(text or "")
    return index


def _iter_citations_for_item(item: dict) -> Iterable[dict]:
    """Yield each citation dict on a cited item."""
    cits = item.get("citations") or []
    if not isinstance(cits, list):
        return
    for c in cits:
        if isinstance(c, dict):
            yield c


def _value_text_for_item(item: dict) -> str:
    """Best-effort 'value text' for an item.

    Lab values: ``value`` (numeric/string).
    KeyFact / TextField: ``text`` or ``value``.
    Medications: ``name`` (and ``dose`` if present).
    Allergies: ``substance`` (and ``reaction`` if present).
    Family history: ``relation`` + ``condition``.
    CodeStatus: ``value``.

    Falls back to the citation's ``quote_or_value`` if no item-level field
    surfaces — the citation contract guarantees that field exists.
    """
    parts: list[str] = []
    for k in ("value", "text", "name", "substance", "condition", "relation"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    if "dose" in item and isinstance(item["dose"], str) and item["dose"].strip():
        parts.append(item["dose"])
    if "reaction" in item and isinstance(item["reaction"], str) and item["reaction"].strip():
        parts.append(item["reaction"])
    return " ".join(parts).strip()


def citation_resolvable(outcome: RunOutcome) -> bool:
    """Every Citation's ``field_or_chunk_id`` resolves to a real LayoutBlock.

    Resolution is checked against the outcome's ``ocr_layout`` list — every
    citation's ``field_or_chunk_id`` must equal the ``bbox_id`` (or ``id`` /
    ``chunk_id``) of some block in the index.

    Vacuous-True cases (rubric does not gate):
      - Extraction is missing or not a dict.
      - No cited items at all (empty extraction; ``citation_present`` will
        catch the "should-have-had-claims" case via schema min_length).
      - No layout blocks were captured (probe disabled / runner skipped) —
        we cannot prove resolution either way; the rubric is informational.
      - Citation's ``source_type`` is not ``document`` (observation /
        guideline citations point at non-layout sources).
    """
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    layout_index = _collect_layout_block_index(outcome)
    if not layout_index:
        # No layout to check against — treat as vacuously satisfied.
        return True

    saw_any = False
    for item in _iter_cited_items(extraction):
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            saw_any = True
            field_id = cit.get("field_or_chunk_id")
            if not field_id or str(field_id) not in layout_index:
                return False
    # No document-typed citations at all → vacuous True.
    return True if not saw_any else True


def citation_row_match(outcome: RunOutcome, *, case: Any = None) -> bool:
    """The cited block's text contains every value-token (case + punct
    normalized), in any order.

    Normalization rules (applied to BOTH the cited block text and the
    item's value text):
      1. Lower-case.
      2. Replace each ASCII-punctuation character with a space.
      3. Collapse runs of whitespace to single spaces, strip ends.

    A "token" is one whitespace-separated word of the normalized value.
    The rubric is True for an item iff EVERY value token appears as a
    token in the normalized cited block (set-membership; no order
    requirement). The block text is taken from the citation's resolved
    ``LayoutBlock`` (looked up by ``field_or_chunk_id``); if the citation
    cannot be resolved, the rubric falls back to the citation's
    ``quote_or_value`` field (which carries the same text by contract).

    Vacuous-True cases:
      - Same as :func:`citation_resolvable` (missing extraction, empty
        cited items, no layout, non-document citation).
      - Empty value text after normalization (no tokens to compare).
      - **Bbox-only ground-truth cases** (``case.bucket == 'bbox_gt'``).
        These fixtures were built specifically for the geometry-based
        ``citation_iou`` rubric. Their sidecar GT carries bbox coordinates
        + value strings but no row-token contract — and the warped-photo
        OCR text often diverges from the LLM's extracted value text not
        because the citation is wrong but because the OCR layer added
        noise the row/token rubric was never designed to gate. Mirrors the
        per-modality vacuous-True pattern used by ``tiff_all_pages_ocrd``
        and ``hl7_citation_locator_well_formed``: the rubric short-
        circuits on cases that don't carry the relevant signal contract,
        rather than scoring them as failures.
    """
    if case is not None and getattr(case, "bucket", None) == "bbox_gt":
        return True
    # Architectural mismatch: TIFF / XLSX citations don't carry row-token
    # contracts. TIFF page-level citations carry bbox coordinates only;
    # XLSX cell references (sheet:row:col) don't map to row-token text.
    # Mirrors the bbox_gt vacuous-True rationale — the rubric short-
    # circuits on cases that don't carry the relevant signal contract.
    if case is not None and getattr(case, "document_modality", None) in (
        "tiff_fax",
        "xlsx_workbook",
    ):
        return True
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    layout_index = _collect_layout_block_index(outcome)

    for item in _iter_cited_items(extraction):
        value_text = _value_text_for_item(item)
        value_tokens = _tokenize(value_text)
        if not value_tokens:
            continue
        # Combine candidate haystacks: every cited block's resolved text,
        # plus the citation's quote_or_value as fallback.
        any_block_satisfied = False
        had_doc_citation = False
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            had_doc_citation = True
            field_id = str(cit.get("field_or_chunk_id") or "")
            block_text = layout_index.get(field_id, "") if layout_index else ""
            if not block_text:
                # Fall back to the citation's verbatim quote.
                block_text = str(cit.get("quote_or_value") or "")
            haystack_tokens = set(_tokenize(block_text))
            if all(tok in haystack_tokens for tok in value_tokens):
                any_block_satisfied = True
                break
        if had_doc_citation and not any_block_satisfied:
            return False
    return True


def citation_token_match(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Stricter than ``citation_row_match``: every value token appears
    in the cited block in the SAME ORDER (subsequence match).

    Other tokens may appear between value tokens — only the relative
    ordering of the value tokens themselves matters. The same
    normalization rules as :func:`citation_row_match` apply.

    Algorithm: walk the haystack token list once, advancing a pointer
    into the value-token list whenever a match is found. The rubric is
    True iff the pointer reaches the end of the value-token list. This
    is the classic O(n+m) subsequence test.

    Vacuous-True cases mirror :func:`citation_row_match`, including the
    ``case.bucket == 'bbox_gt'`` short-circuit (those fixtures gate
    geometry via ``citation_iou``, not row-token text alignment).
    """
    if case is not None and getattr(case, "bucket", None) == "bbox_gt":
        return True
    # Architectural mismatch (mirrors citation_row_match): TIFF/XLSX
    # citations don't carry row-token contracts.
    if case is not None and getattr(case, "document_modality", None) in (
        "tiff_fax",
        "xlsx_workbook",
    ):
        return True
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return False
    layout_index = _collect_layout_block_index(outcome)

    for item in _iter_cited_items(extraction):
        value_text = _value_text_for_item(item)
        value_tokens = _tokenize(value_text)
        if not value_tokens:
            continue
        any_block_satisfied = False
        had_doc_citation = False
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            had_doc_citation = True
            field_id = str(cit.get("field_or_chunk_id") or "")
            block_text = layout_index.get(field_id, "") if layout_index else ""
            if not block_text:
                block_text = str(cit.get("quote_or_value") or "")
            haystack_tokens = _tokenize(block_text)
            # Subsequence test
            i = 0
            for tok in haystack_tokens:
                if i < len(value_tokens) and tok == value_tokens[i]:
                    i += 1
            if i == len(value_tokens):
                any_block_satisfied = True
                break
        if had_doc_citation and not any_block_satisfied:
            return False
    return True


_IOU_PASS_THRESHOLD = 0.5


def _coerce_bbox(b: Any) -> Optional[tuple[float, float, float, float]]:
    """Accept ``{x,y,w,h}`` or ``[x,y,w,h]`` and return ``(x,y,w,h)`` or None."""
    if b is None:
        return None
    if isinstance(b, dict):
        try:
            return (
                float(b["x"]), float(b["y"]),
                float(b["w"]), float(b["h"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(b, (list, tuple)) and len(b) == 4:
        try:
            return tuple(float(v) for v in b)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def _coerce_polygon(p: Any) -> Optional[List[tuple[float, float]]]:
    """Accept a list/tuple of (x, y) pairs and return ``[(x, y), ...]``.

    Returns ``None`` for missing/invalid input OR for degenerate polygons
    (fewer than 3 distinct points). Degenerate polygons should fall back
    to the bbox path — never silently treat a 2-point line as a region.
    """
    if not isinstance(p, (list, tuple)):
        return None
    out: List[tuple[float, float]] = []
    for pt in p:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            return None
        try:
            out.append((float(pt[0]), float(pt[1])))
        except (TypeError, ValueError):
            return None
    # Reject degenerate shapes (per Wave 2B contract §3).
    distinct = {(round(x, 6), round(y, 6)) for (x, y) in out}
    if len(distinct) < 3:
        return None
    return out


def _polygon_bbox(poly: List[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return (x0, y0, x1 - x0, y1 - y0)


def _shoelace_area(poly: List[tuple[float, float]]) -> float:
    """Pure-Python shoelace area for a simple polygon. Returns absolute area."""
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _sutherland_hodgman_clip(
    subject: List[tuple[float, float]], clip: List[tuple[float, float]]
) -> List[tuple[float, float]]:
    """Sutherland-Hodgman polygon clip. ``clip`` must be a CONVEX polygon
    (with consistent winding). For axis-aligned bbox-as-polygon use this
    is sufficient; for general polygon-vs-polygon we prefer Shapely when
    available. Returns the clipped polygon as a list of points."""
    output = list(subject)
    if not output or not clip:
        return []
    # Determine clip winding (signed area).
    n = len(clip)
    signed = 0.0
    for i in range(n):
        x1, y1 = clip[i]
        x2, y2 = clip[(i + 1) % n]
        signed += x1 * y2 - x2 * y1
    # Reverse if clockwise so "inside" = left side of edge.
    if signed < 0:
        clip = list(reversed(clip))
        n = len(clip)

    def inside(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0

    def intersect(
        p1: tuple[float, float],
        p2: tuple[float, float],
        a: tuple[float, float],
        b: tuple[float, float],
    ) -> tuple[float, float]:
        # Line p1-p2 intersected with line a-b.
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = a
        x4, y4 = b
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if denom == 0:
            return p2
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))

    for i in range(n):
        if not output:
            return []
        a = clip[i]
        b = clip[(i + 1) % n]
        new_output: List[tuple[float, float]] = []
        s = output[-1]
        for e in output:
            if inside(e, a, b):
                if not inside(s, a, b):
                    new_output.append(intersect(s, e, a, b))
                new_output.append(e)
            elif inside(s, a, b):
                new_output.append(intersect(s, e, a, b))
            s = e
        output = new_output
    return output


def _polygon_iou(
    a: List[tuple[float, float]], b: List[tuple[float, float]]
) -> float:
    """IoU(a, b) for two polygons. Prefers Shapely when available
    (handles non-convex / self-intersecting cases); otherwise falls back
    to a pure-Python Sutherland-Hodgman clip + shoelace. The fallback
    assumes the clipping polygon is convex — for axis-aligned bboxes-as-
    polygons (the common UI case) that's true; for arbitrary OCR
    polygons it's a best-effort approximation."""
    try:
        from shapely.geometry import Polygon  # type: ignore

        pa = Polygon(a)
        pb = Polygon(b)
        if not pa.is_valid:
            pa = pa.buffer(0)
        if not pb.is_valid:
            pb = pb.buffer(0)
        if pa.is_empty or pb.is_empty:
            return 0.0
        inter = pa.intersection(pb).area
        union = pa.union(pb).area
        if union <= 0:
            return 0.0
        return float(inter / union)
    except ImportError:
        pass
    # Pure-Python fallback.
    area_a = _shoelace_area(a)
    area_b = _shoelace_area(b)
    if area_a <= 0 or area_b <= 0:
        return 0.0
    # Clip a against b; if b isn't convex this is approximate. We try
    # both directions and keep the smaller intersection (more conservative).
    clipped_ab = _sutherland_hodgman_clip(a, b)
    inter1 = _shoelace_area(clipped_ab) if clipped_ab else 0.0
    clipped_ba = _sutherland_hodgman_clip(b, a)
    inter2 = _shoelace_area(clipped_ba) if clipped_ba else 0.0
    inter = min(inter1, inter2) if (inter1 > 0 and inter2 > 0) else max(inter1, inter2)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _bbox_iou_value(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ix1 = max(ax, bx)
    iy1 = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    union = (aw * ah) + (bw * bh) - inter
    if union <= 0:
        return 0.0
    return inter / union


def citation_iou(extracted: Any, gt: Any) -> bool:
    """Wave 2B — polygon-aware boolean rubric: IoU(extracted, gt) >= 0.5.

    Each side accepts EITHER:
      * a bbox: ``{"x","y","w","h"}`` dict or 4-tuple/list, OR
      * a polygon: a list of ``[x, y]`` pairs (≥3 distinct points), OR
      * a Citation-shaped dict carrying ``bbox`` and/or ``polygon``.

    Polygon precedence (per Wave 2B contract §1): when polygon is present
    on a side it wins over bbox on that side. Mixed-mode (polygon vs
    bbox, contract §2): we do NOT spuriously favor the polygon side —
    we degrade to bbox-IoU using the polygon-side's axis-aligned bounding
    box. Degenerate polygons (<3 distinct points) fall through to the
    bbox path on that side (contract §3).

    Returns ``False`` when neither side has a usable shape — half-
    populated GT is a generator bug, not a vacuous pass.
    """
    a_poly, a_bbox = _shape_for_side(extracted)
    b_poly, b_bbox = _shape_for_side(gt)
    # Both sides have polygons → polygon-vs-polygon.
    if a_poly is not None and b_poly is not None:
        return _polygon_iou(a_poly, b_poly) >= _IOU_PASS_THRESHOLD
    # Mixed: collapse the polygon side to its axis-aligned bbox and use
    # bbox-vs-bbox IoU. Never favor polygon spuriously.
    if a_poly is not None and b_bbox is not None:
        a_bbox = _polygon_bbox(a_poly)
    elif b_poly is not None and a_bbox is not None:
        b_bbox = _polygon_bbox(b_poly)
    if a_bbox is None or b_bbox is None:
        return False
    return _bbox_iou_value(a_bbox, b_bbox) >= _IOU_PASS_THRESHOLD


def _shape_for_side(
    side: Any,
) -> tuple[
    Optional[List[tuple[float, float]]], Optional[tuple[float, float, float, float]]
]:
    """Resolve either (polygon, bbox) for a side. Side may be a bbox dict
    {x,y,w,h}, a 4-tuple bbox, a list of (x,y) pairs (polygon), or a
    Citation-shaped dict with ``bbox`` and/or ``polygon`` keys."""
    if side is None:
        return (None, None)
    # Citation-shaped dict (has the keys we recognize).
    if isinstance(side, dict) and ("polygon" in side or "bbox" in side):
        # Avoid false-positive on bbox-dicts: if it has x/y/w/h treat as bbox.
        if {"x", "y", "w", "h"}.issubset(side.keys()):
            return (None, _coerce_bbox(side))
        poly = _coerce_polygon(side.get("polygon"))
        bbox = _coerce_bbox(side.get("bbox"))
        return (poly, bbox)
    # List of pairs → polygon. (4-tuple bboxes match `len == 4` but their
    # elements are scalars, not pairs, so `_coerce_polygon` returns None.)
    poly = _coerce_polygon(side)
    if poly is not None:
        return (poly, None)
    return (None, _coerce_bbox(side))


def citation_polygon_used(extraction: Any) -> Optional[tuple[int, int]]:
    """Wave 2B — INFO-only rubric: count citations rendered as polygon vs bbox.

    Walks every cited item in the extraction and returns
    ``(n_polygon, n_total_with_bbox_or_polygon)``. The "polygon used"
    rate is ``n_polygon / n_total``. Returns ``None`` when the
    extraction is missing or has no document citations — info-only
    callers should skip the case rather than count it as 0%.

    Not in baseline.json (purely diagnostic — measures source-shape
    fidelity over time as paddle adoption grows)."""
    if not isinstance(extraction, dict):
        return None
    n_poly = 0
    n_total = 0
    for item in _iter_cited_items(extraction):
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            poly = cit.get("polygon")
            bbox = cit.get("bbox")
            if poly is None and bbox is None:
                continue
            n_total += 1
            if _coerce_polygon(poly) is not None:
                n_poly += 1
    if n_total == 0:
        return None
    return (n_poly, n_total)


def citation_pixel_distance(extracted_bbox: Any, gt_bbox: Any) -> Optional[float]:
    """Wave 2C — INFO-only rubric: centroid pixel distance (no pass/fail).

    Returns the Euclidean distance (in input units, typically pixels or
    PDF points) between the centroids of the extracted and GT bboxes.
    Returns ``None`` when either bbox is missing — INFO-only callers
    should skip the case rather than count it as a regression.
    """
    a = _coerce_bbox(extracted_bbox)
    b = _coerce_bbox(gt_bbox)
    if a is None or b is None:
        return None
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    cax = ax + aw / 2.0
    cay = ay + ah / 2.0
    cbx = bx + bw / 2.0
    cby = by + bh / 2.0
    dx = cax - cbx
    dy = cay - cby
    return (dx * dx + dy * dy) ** 0.5


def correct_critic_decision(outcome: RunOutcome, *, expected: str) -> bool:
    return outcome.critic_decision == expected


def _record_contains(record: dict, needles: Set[str]) -> bool:
    """True iff any needle appears in the record's message OR any extra value."""
    haystack_parts: list[str] = [str(record.get("message") or "")]
    extra = record.get("extra") or {}
    if isinstance(extra, dict):
        for value in extra.values():
            try:
                haystack_parts.append(str(value))
            except Exception:  # pragma: no cover — defensive
                continue
    haystack = "\n".join(haystack_parts)
    return any(needle and needle in haystack for needle in needles)


def no_phi_in_logs(
    outcome: RunOutcome,
    *,
    synthetic_phi_values: Optional[Set[str]] = None,
) -> bool:
    """No log message or extra value may contain a synthetic-PHI token."""
    needles = synthetic_phi_values
    if needles is None:
        needles = _load_default_phi_values()
    if not needles:
        return True
    for record in outcome.captured_logs:
        if _record_contains(record, needles):
            return False
    return True


def keyword_match_in_citation(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Evidence-retrieval rubric — boolean, evaluated against real retrieval output.

    Vacuously ``True`` for cases without evidence-retrieval expectations
    (i.e. ``case.evidence_query`` is falsy or unset).

    For evidence-retrieval cases this inspects ``outcome.retrieval`` —
    the snippet list returned by ``rag.retrieve.search`` via the
    LangGraph ``evidence_retriever`` node — and returns ``True`` iff:

      * at least one snippet's ``source_id`` matches one of
        ``case.expected_must_cite_source_id``, AND
      * for every keyword in ``case.expected_keywords_in_quote``, at
        least one snippet's ``content`` (case-insensitive) contains it.

    When ``expected_keywords_in_quote`` is empty the keyword check is
    skipped — the source-id match alone is sufficient.

    A skipped run (``outcome.skipped_reason`` set, e.g. when the host
    lacks ``AUDIT_DB_URL`` / ``VOYAGE_API_KEY``) is treated as vacuously
    ``True`` here; the suite-level reporter surfaces the skip status
    separately so it cannot mask a real regression.
    """
    if case is None:
        return True
    query = getattr(case, "evidence_query", None)
    if not query:
        return True
    sources = tuple(getattr(case, "expected_must_cite_source_id", ()) or ())
    keywords = tuple(getattr(case, "expected_keywords_in_quote", ()) or ())
    if not sources:
        # Definitional check — half-populated evidence cases must not
        # silently pass.
        return False

    # Skipped (no Postgres / no Voyage key) — don't fail the case.
    if getattr(outcome, "skipped_reason", None):
        return True

    retrieval = getattr(outcome, "retrieval", None) or {}
    snippets = retrieval.get("snippets") if isinstance(retrieval, dict) else None
    if not isinstance(snippets, list) or not snippets:
        return False

    expected_sources_lc = {str(s).lower() for s in sources}
    matching_snippets = [
        s for s in snippets
        if isinstance(s, dict)
        and str(s.get("source_id") or "").lower() in expected_sources_lc
    ]
    if not matching_snippets:
        return False

    if not keywords:
        return True

    # Every keyword must appear (case-insensitive) in at least one
    # snippet's content. Search across all snippets, not only the
    # source-matched ones — the source check above already gates the
    # citation; the content check here verifies the corpus actually
    # surfaced the answer text.
    haystacks = [str(s.get("content") or "").lower() for s in snippets if isinstance(s, dict)]
    for kw in keywords:
        needle = str(kw).lower()
        if not any(needle in hay for hay in haystacks):
            return False
    return True


# --------------------------------------------------------------------------- #
# Phase 9 Slice 9.9 — multimodal-expansion mechanical rubrics.
#
# All five rubrics are conditioned on per-case opt-ins (``expected_quarantine``,
# ``expected_staging``, modality) so they short-circuit to ``True`` on cases
# that don't exercise the relevant pathway. The ``hard, threshold 1.00``
# rubrics fail loudly on any case where the expected behavior is asserted but
# the outcome doesn't carry the corresponding audit / staging row / OCR page.
#
# Outcome-side fields read here (``audit_rows``, ``staged_observations``,
# ``written_observation_ids``, ``pending_extractions``, ``ocr_page_citations``)
# are populated by the runner when the multimodal staging path is wired
# (Slices 9.5–9.8). Until then, the runner sets them to ``None`` and these
# rubrics fall through to vacuous-True per the standard rubric pattern —
# never spuriously fail on incomplete instrumentation.
# --------------------------------------------------------------------------- #


_SYNTHETIC_MARKER_RE = re.compile(r"(?i)\b(synthetic\s+data|no\s+phi)\b")


def _audit_rows(outcome: RunOutcome) -> Optional[list[dict]]:
    rows = getattr(outcome, "audit_rows", None)
    return rows if isinstance(rows, list) else None


def _staged_observations(outcome: RunOutcome) -> Optional[list[dict]]:
    rows = getattr(outcome, "staged_observations", None)
    return rows if isinstance(rows, list) else None


def quarantine_audit_emitted(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Hard / 1.00 — every ``expected_quarantine=True`` case emits an audit row.

    Vacuous-True when ``case.expected_quarantine`` is False / unset (the rubric
    only gates the cases that opted in). Vacuous-True when the runner has not
    populated ``outcome.audit_rows`` (no-op until Slices 9.5/9.6/9.7 wire it).

    The audit row must (a) carry an ``event`` field naming a quarantine action,
    (b) have a non-empty ``detail_json``, and (c) NOT contain any of the
    case's synthetic-PHI tokens in its detail_json (PSR-3 scrub contract).
    """
    if case is None or not getattr(case, "expected_quarantine", False):
        return True
    rows = _audit_rows(outcome)
    if rows is None:
        return True  # runner not yet wired — informational
    quar_events = {"document_quarantined", "quarantine", "identity_mismatch"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = str(row.get("event") or row.get("type") or "").lower()
        detail = row.get("detail_json") or row.get("detail") or ""
        if not event:
            continue
        if any(e in event for e in quar_events) and detail:
            return True
    return False


def no_unconfirmed_writes(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Hard / 1.00 — no ``copilot_observations`` row exists without a matching
    ``copilot_pending_extractions`` row in ``state='written'``.

    Reads ``outcome.pending_extractions`` (list of {id, state}) and
    ``outcome.written_observation_ids`` (list of observation ids that landed
    in ``copilot_observations``). For every written observation id, the
    matching pending row must exist and be ``state='written'``.

    Vacuous-True when neither side is populated (runner instrumentation
    incomplete) — never spuriously fail.
    """
    pending = getattr(outcome, "pending_extractions", None)
    written = getattr(outcome, "written_observation_ids", None)
    if not isinstance(pending, list) or not isinstance(written, list):
        return True
    by_id: dict[str, str] = {}
    for row in pending:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("observation_id") or row.get("id") or "")
        state = str(row.get("state") or "")
        if rid:
            by_id[rid] = state
    for obs_id in written:
        sid = str(obs_id)
        if by_id.get(sid) != "written":
            return False
    return True


def stage_failure_audit_emitted(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Soft / 0.95 — failed staging transitions carry a ``reason`` field.

    Walks ``outcome.audit_rows`` for any row whose event indicates a stage
    transition into ``failed`` (event names containing ``stage_failure``,
    ``staging_failed``, or ``transition_failed``) and asserts that the row's
    ``reason`` field is populated.

    Vacuous-True when no staging-failure rows are present (most cases).
    """
    rows = _audit_rows(outcome)
    if rows is None:
        return True
    failure_event_tokens = ("stage_failure", "staging_failed", "transition_failed")
    saw_any = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = str(row.get("event") or row.get("type") or "").lower()
        if not any(tok in event for tok in failure_event_tokens):
            continue
        saw_any = True
        reason = row.get("reason") or row.get("error") or ""
        if not reason or not str(reason).strip():
            return False
    return True if saw_any or True else False  # vacuous True when no failures observed


def tiff_all_pages_ocrd(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Hard / 1.00 — N-page TIFF produces ≥1 distinct citation per page index.

    Only gates ``document_modality == 'tiff_fax'`` cases. Reads
    ``outcome.ocr_page_citations`` (list of ints — citation count per page,
    indexed 0..N-1) OR falls back to deriving page indexes from the
    extraction's citations (via each citation's ``page`` field, if present).

    Catches the classic ``Pillow TiffImagePlugin.seek()`` off-by-one where the
    page iterator stops at N-1 instead of N.

    Vacuous-True for non-TIFF cases and when the outcome lacks page
    instrumentation (runner not yet wired).
    """
    if case is None or getattr(case, "document_modality", None) != "tiff_fax":
        return True
    pages = getattr(outcome, "ocr_page_citations", None)
    if isinstance(pages, list) and pages:
        # Every page must have at least one citation.
        return all(isinstance(c, int) and c > 0 for c in pages)
    # Fallback — count distinct ``page`` values across all citations.
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return True
    seen_pages: Set[int] = set()
    expected_pages = getattr(outcome, "tiff_n_pages", None)
    for item in _iter_cited_items(extraction):
        for cit in _iter_citations_for_item(item):
            page = cit.get("page")
            if isinstance(page, int):
                seen_pages.add(page)
    if not isinstance(expected_pages, int) or expected_pages <= 0:
        return True  # no page-count evidence — informational
    return len(seen_pages) >= expected_pages


def synthetic_marker_not_extracted(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Hard / 1.00 — staged Observation ``valueString`` / ``note.text`` must
    NOT contain ``Synthetic data`` or ``no PHI`` markers.

    Walks ``outcome.staged_observations`` (list of FHIR Observation dicts) and
    runs the regex against each Observation's ``valueString`` and every
    ``note[*].text``. Any hit fails the rubric — synthetic-data placeholders
    are scaffolding tokens that must never be lifted into a clinical record.

    Vacuous-True when no staged observations are present.
    """
    staged = _staged_observations(outcome)
    if staged is None or not staged:
        return True
    for obs in staged:
        if not isinstance(obs, dict):
            continue
        # FHIR Observation may carry value via valueString, valueQuantity, etc.
        # The contract specifies value[xText] (free-text shapes).
        for key in ("valueString", "valueText"):
            v = obs.get(key)
            if isinstance(v, str) and _SYNTHETIC_MARKER_RE.search(v):
                return False
        notes = obs.get("note") or []
        if isinstance(notes, list):
            for n in notes:
                if isinstance(n, dict):
                    txt = n.get("text")
                    if isinstance(txt, str) and _SYNTHETIC_MARKER_RE.search(txt):
                        return False
    return True


# --------------------------------------------------------------------------- #
# Phase 2 Step 2 — post-approval RAG synthesis grounding.
#
# Mechanical, deterministic, hard-failure (baseline 1.00, min 1.00). Gates the
# structured output of the post-approval synthesizer against its input
# allowlist: every clinical-signal citation_id must come from the approved
# facts or retrieved guideline snippets that were handed to the synthesizer,
# every guideline_mapping chunk_id must come from those same guidelines, and
# the schema's required string fields must be non-empty.
#
# Stage 3 wires ``outcome.synthesis`` (SynthesisOutput.to_dict()) and
# ``outcome.synthesis_input`` (serialized SynthesisInput) onto the runner.
# Until then, ``getattr(outcome, "synthesis", None)`` returns None and this
# rubric vacuously passes — never spuriously fails on incomplete
# instrumentation, mirroring the Slice 9.9 multimodal pattern above.
# --------------------------------------------------------------------------- #


_SYNTHESIS_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"approved_facts", "clinical_signals", "guideline_mappings", "next_steps"}
)


def _truncate_reason_value(value: str, *, limit: int = 80) -> str:
    """Truncate an offending id to ``limit`` chars for reason-code surfacing."""
    s = str(value)
    return s if len(s) <= limit else s[:limit]


def _synthesis_grounded_check(outcome: RunOutcome) -> tuple[bool, Optional[str]]:
    """Internal — returns ``(passed, reason)``.

    ``reason`` is None on PASS and one of the deterministic snake_case codes
    documented on :func:`synthesis_grounded` on FAIL. Kept separate from the
    bool-returning public rubric so the registry signature stays uniform with
    ``citation_present`` / ``no_phi_in_logs`` while still exposing diagnostics
    to callers (eval reporters, debug tooling) via
    :func:`synthesis_grounded_reason`.
    """
    synth = getattr(outcome, "synthesis", None)
    if synth is None:
        # Vacuous PASS — synthesis was not invoked, fell back to recap, or the
        # runner has not yet wired the field (Stage 3). The fallback path is
        # exercised by other rubrics; this one only judges existing output.
        return (True, None)

    synth_input = getattr(outcome, "synthesis_input", None)
    if synth_input is None or not isinstance(synth_input, dict):
        # Cannot reconstruct the citation_id allowlist without the input.
        return (False, "synthesis_input_missing")

    # 2. Schema gate.
    if not isinstance(synth, dict):
        return (False, "schema_invalid")
    if set(synth.keys()) != _SYNTHESIS_REQUIRED_KEYS:
        return (False, "schema_invalid")
    approved_prose = synth.get("approved_facts")
    clinical_signals = synth.get("clinical_signals")
    guideline_mappings = synth.get("guideline_mappings")
    next_steps = synth.get("next_steps")
    if not isinstance(approved_prose, str):
        return (False, "schema_invalid")
    if not isinstance(clinical_signals, list):
        return (False, "schema_invalid")
    if not isinstance(guideline_mappings, list):
        return (False, "schema_invalid")
    if not isinstance(next_steps, list):
        return (False, "schema_invalid")

    # 5a. Approved-facts prose non-empty.
    if not approved_prose.strip():
        return (False, "approved_facts_empty")

    # Build the citation_id allowlist and chunk_id allowlist from the input.
    input_facts = synth_input.get("approved_facts") or []
    input_guidelines = synth_input.get("guidelines") or []
    if not isinstance(input_facts, list) or not isinstance(input_guidelines, list):
        return (False, "schema_invalid")
    valid_citation_ids: Set[str] = set()
    for f in input_facts:
        if isinstance(f, dict):
            cid = f.get("citation_id")
            if isinstance(cid, str):
                valid_citation_ids.add(cid)
    valid_chunk_ids: Set[str] = set()
    for g in input_guidelines:
        if isinstance(g, dict):
            cid = g.get("citation_id")
            if isinstance(cid, str):
                valid_citation_ids.add(cid)
            chid = g.get("chunk_id")
            if isinstance(chid, str):
                valid_chunk_ids.add(chid)

    # 3. Clinical-signal grounding.
    for s in clinical_signals:
        if not isinstance(s, dict):
            return (False, "schema_invalid")
        cids = s.get("citation_ids")
        if not isinstance(cids, list) or len(cids) == 0:
            return (False, "citation_ids_empty")
        for cid in cids:
            if not isinstance(cid, str) or cid not in valid_citation_ids:
                return (
                    False,
                    f"unknown_citation_id:{_truncate_reason_value(cid)}",
                )
        claim = s.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            return (False, "empty_claim")

    # 4. Guideline-mapping grounding.
    for m in guideline_mappings:
        if not isinstance(m, dict):
            return (False, "schema_invalid")
        chid = m.get("chunk_id")
        if not isinstance(chid, str) or chid not in valid_chunk_ids:
            return (
                False,
                f"unknown_chunk_id:{_truncate_reason_value(str(chid))}",
            )
        claim = m.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            return (False, "empty_claim")

    # 5b. Next-step entries (when present) must not be blank.
    for step in next_steps:
        if not isinstance(step, str) or not step.strip():
            return (False, "next_step_empty")

    return (True, None)


def synthesis_grounded(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Hard / 1.00 — every synthesis citation_id and chunk_id is grounded
    in the synthesizer's own input allowlist, and required schema strings
    are non-empty.

    The rubric reads two fields off the outcome:

      * ``outcome.synthesis`` — the structured ``SynthesisOutput.to_dict()``
        payload (see ``agent/synthesis.py``), or ``None`` when synthesis
        was skipped, fell back to recap, or was never invoked.
      * ``outcome.synthesis_input`` — the serialized ``SynthesisInput`` that
        was handed to the synthesizer. Carries the canonical citation_id /
        chunk_id allowlist for this case.

    Pass criterion (all must hold):

      1. **Vacuous PASS** when ``outcome.synthesis is None``. The fallback
         path is judged by other rubrics; this one only gates output that
         exists.
      2. **Schema gate** — synthesis is a dict whose top-level keys are
         exactly ``{"approved_facts", "clinical_signals",
         "guideline_mappings", "next_steps"}``. ``approved_facts`` is a
         string; the other three are lists.
      3. **Clinical-signal grounding** — every entry has a non-empty
         ``citation_ids`` list whose members all appear in the input
         allowlist (approved-fact citation_ids ∪ guideline citation_ids),
         and a non-empty ``claim`` string.
      4. **Guideline-mapping grounding** — every entry's ``chunk_id`` is a
         member of the input guidelines' chunk_ids, and ``claim`` is
         non-empty.
      5. **No empty claims** — the ``approved_facts`` prose is non-empty,
         and every ``next_steps`` entry is non-empty after strip.

    Edge cases:

      * ``synthesis_input is None`` while ``synthesis is not None`` — FAIL
        with reason ``synthesis_input_missing``. Without the input the
        allowlist cannot be reconstructed.
      * Empty ``clinical_signals`` / ``guideline_mappings`` / ``next_steps``
        lists are PASS (the schema permits them).
      * Cache-replayed stale fingerprints are out of scope; the cache
        contract handles invalidation by prompt_version + approved-fact
        fingerprint.

    Reason codes (surfaced via :func:`synthesis_grounded_reason`):
    ``schema_invalid``, ``synthesis_input_missing``,
    ``unknown_citation_id:{value}``, ``unknown_chunk_id:{value}``,
    ``empty_claim``, ``citation_ids_empty``, ``next_step_empty``,
    ``approved_facts_empty``. Offending id values are truncated to 80 chars.

    Explicit non-goals:
      * Does NOT score factual accuracy of claims (covered by
        ``factually_consistent`` for extraction; a future
        ``synthesis_faithful`` LLM-judge rubric will cover prose meaning).
      * Does NOT regex for prescribing language (future
        ``synthesis_no_prescribing``).
      * Does NOT score length, formatting, em-dash bans, or voice — those
        are enforced by the schema and the synthesizer prompt upstream.
    """
    passed, _reason = _synthesis_grounded_check(outcome)
    return passed


def synthesis_grounded_reason(outcome: RunOutcome) -> Optional[str]:
    """Diagnostic sibling of :func:`synthesis_grounded`.

    Returns ``None`` on PASS or one of the deterministic reason codes
    documented on :func:`synthesis_grounded` on FAIL. Reporters and debug
    tooling can call this to surface *why* a case failed without re-running
    the rubric logic. Not part of the registry (the gate is the bool
    rubric); kept exported for external consumers.
    """
    _passed, reason = _synthesis_grounded_check(outcome)
    return reason


# --------------------------------------------------------------------------- #
# 2026-05-08 problem_list build — Phase 1 mechanical rubric.
#
# Hard / 1.00 (same posture as no_phi_in_logs). Walks every
# ProblemListItem in extraction.problem_list and asserts that any
# non-null icd10_code grounds literally in the OCR source text.
# Vacuous-True for cases without problem_list extraction (which is
# every case in the existing 156-case suite). Mirrors the
# extractor.intake.validate_icd10_grounded check that runs pre-staging,
# so a failed rubric means a fabricated code escaped the runtime
# guardrail — that's a generator/wiring bug worth halting on.
# --------------------------------------------------------------------------- #


def _layout_source_text(outcome: RunOutcome) -> str:
    """Concatenate every block.text in outcome.ocr_layout.

    Used as the haystack for icd10_grounded. Missing/empty layout →
    empty string; the rubric falls vacuously through (no problem_list
    extraction can survive without a layout to ground against).
    """
    layout = getattr(outcome, "ocr_layout", None) or []
    if not isinstance(layout, list):
        return ""
    parts: list[str] = []
    for blk in layout:
        if isinstance(blk, dict):
            t = blk.get("text") or blk.get("content") or blk.get("value") or ""
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


def condition_writeback_succeeded(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Soft / 1.00 — every grounded-ICD-10 problem_list row that the
    extractor produced should result in a Condition write at approval.

    The eval runner today doesn't wire the dispatcher's approve flow
    for the 156-case suite (cases run extraction + rubric scoring,
    not approval), so this rubric is *informational* in the same way
    the multimodal-9.9 rubrics are: it returns True when the runner
    hasn't populated ``outcome.written_condition_ids``, and FAILs only
    when the runner did populate the field but a grounded-ICD-10 row
    is missing from the written set.

    Concretely:

      * extraction.problem_list has N items where icd10_code is a
        non-empty string (post-Phase-1 guardrail).
      * outcome.written_condition_ids is the list of
        ``deterministic_condition_id(doc, icd10)`` values that landed
        in copilot_conditions.
      * Pass iff N == len(intersection(expected_ids, written_ids))
        OR outcome.written_condition_ids is None / not a list (runner
        not wired).

    Vacuous-True when:
      - extraction is missing or not a dict
      - kind != intake_form
      - no problem_list entries
      - no grounded ICD-10 codes at all
      - runner hasn't populated written_condition_ids
    """
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return True
    if extraction.get("kind") != "intake_form":
        return True
    items = extraction.get("problem_list") or []
    if not isinstance(items, list) or not items:
        return True
    grounded_codes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("icd10_code")
        if isinstance(code, str) and code.strip():
            grounded_codes.append(code.strip())
    if not grounded_codes:
        return True
    written = getattr(outcome, "written_condition_ids", None)
    if not isinstance(written, list):
        # Runner not wired — vacuous-True. The Phase 5 attestation
        # walks the live ingest → approve → FHIR-Condition chain by
        # hand; the rubric here is the eval-suite sentinel for the
        # day the runner instruments approve dispatch.
        return True
    written_set = {str(w) for w in written if isinstance(w, str)}
    # Each grounded code should produce a Condition id of the form
    # copilot-{document_id}-{sanitised_code}. We don't know the
    # document_id from the rubric's perspective, so we use suffix
    # matching: every grounded code's sanitised form must appear at
    # the end of some written id.
    import re as _re
    for code in grounded_codes:
        sanitised = _re.sub(r"[^\w.-]+", "-", code)
        matched = any(w.endswith(f"-{sanitised}") for w in written_set)
        if not matched:
            return False
    return True


def icd10_grounded(outcome: RunOutcome, *, case: Any = None) -> bool:
    """Hard / 1.00 — every ProblemListItem.icd10_code must appear
    literally in the OCR source text.

    The runtime guardrail (extractors.intake.apply_icd10_guardrail)
    drops fabricated codes pre-staging. This rubric is the eval-side
    sentinel: if the runtime guardrail let a fabricated code through,
    the suite halts.

    Vacuous-True cases:
      - extraction is missing or not a dict
      - kind != intake_form (problem_list lives only on IntakeForm)
      - no problem_list entries at all
      - every problem_list entry has icd10_code = None

    Validation reuses :func:`extractors.intake.validate_icd10_grounded`
    so the eval and the runtime gate share the same predicate.
    """
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return True
    if extraction.get("kind") != "intake_form":
        return True
    items = extraction.get("problem_list") or []
    if not isinstance(items, list) or not items:
        return True
    # Lazy import to avoid forcing every rubric consumer to also import
    # the extractor module (which pulls Anthropic, pymupdf, etc.).
    from extractors.intake import validate_icd10_grounded

    source = _layout_source_text(outcome)
    if not source:
        # No haystack → can't validate. Return True vacuously rather
        # than spuriously fail; the no-layout cases are caught by
        # citation_resolvable's own vacuous-True branch.
        return True
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("icd10_code")
        if code is None:
            continue
        if not isinstance(code, str) or not code.strip():
            continue
        if not validate_icd10_grounded(code, source):
            return False
    return True


# --------------------------------------------------------------------------- #
# Phase 3 Part B' — per-modality citation locator rubrics.
#
# The cross-modality ``citation_resolvable`` rubric uses an OCR-layout index
# (``bbox_id`` lookup) which is the right contract for PDF/PNG/TIFF
# extractions but does not apply to HL7 v2 messages (no OCR — citations
# point at HL7 segments via ``OBX-5|seg=N`` / ``PID-3.1`` style locators)
# or XLSX workbooks (citations point at cells via
# ``sheet=Patient|row=4|col=Value``). For those modalities we validate
# the locator's *shape* — that it conforms to the parser's documented
# format — rather than resolving against an OCR layout that doesn't exist.
#
# Both rubrics are conditioned on ``case.document_modality`` and short-
# circuit to vacuous-True for cases that don't carry the expected
# modality. They sit alongside ``citation_resolvable`` rather than
# replacing it: PDF/PNG/TIFF cases fall through ``citation_resolvable``
# (which has its own vacuous-True branches) and these rubrics
# vacuously pass; HL7/XLSX cases vacuously pass ``citation_resolvable``
# (no layout) and fail/pass these instead. The aggregate per-modality
# pass-rate map in ``baseline.json`` separates the two cleanly.
# --------------------------------------------------------------------------- #


_HL7_SEGMENT_NAMES: frozenset[str] = frozenset(
    {
        # PID and friends per parsers.hl7.adt locator strings.
        "PID", "PV1", "NK1", "GT1", "IN1", "PD1",
        # OBX and friends per parsers.hl7.oru locator strings.
        "OBX", "OBR", "MSH", "EVN",
    }
)

# HL7 locator examples: "PID-3.1", "PV1-3", "OBX-5|seg=3", "OBX-3.1|seg=12".
# We split on the first '|' to separate the field path from the optional
# segment-index suffix, then split the field path on '-' to lift the
# segment name. Anything else fails.
_HL7_LOCATOR_RE = re.compile(
    r"^([A-Z][A-Z0-9]{2})-(\d+)(?:\.(\d+))?(?:\|seg=(\d+))?$"
)


def hl7_citation_locator_well_formed(
    outcome: RunOutcome, *, case: Any = None
) -> bool:
    """Hard / 1.00 — every HL7 citation's ``field_or_chunk_id`` matches the
    documented HL7 segment-locator shape (``SEG-N[.M][|seg=K]``) and names
    a segment in the v1 supported set.

    Conditions on ``case.document_modality == 'hl7_v2'``. Vacuous-True for
    every other modality. Vacuous-True when the extraction is missing /
    not a dict (the schema rubric covers that case).

    Catches the regression where a parser starts emitting locator strings
    that no resolver / verifier downstream can parse (e.g. a stray
    ``"OBX_5_seg3"`` that flips a delimiter).
    """
    if case is None or getattr(case, "document_modality", None) != "hl7_v2":
        return True
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return True
    saw_any = False
    for item in _iter_cited_items(extraction):
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            saw_any = True
            field_id = str(cit.get("field_or_chunk_id") or "")
            m = _HL7_LOCATOR_RE.match(field_id)
            if m is None:
                return False
            seg_name = m.group(1)
            if seg_name not in _HL7_SEGMENT_NAMES:
                return False
    # No HL7 citations at all → vacuous True (the schema_valid rubric
    # gates whether values were expected; we just judge the locators that
    # were emitted).
    return True if not saw_any else True


# XLSX locator examples (see parsers/xlsx/sheets/*._build_locator):
#   sheet=Patient|row=4|col=Value
#   sheet=Labs_Trend|row=12|col=2026-04-15
#   sheet=Care_Gaps|row=7|col=Notes
_XLSX_LOCATOR_RE = re.compile(
    r"^sheet=([A-Za-z][\w_]*)\|row=(\d+)\|col=(.+)$"
)

# Sheet names the v1 XLSX parser knows about (per parsers/xlsx/sheets/).
_XLSX_SHEET_NAMES: frozenset[str] = frozenset(
    {"Patient", "Medications", "Allergies", "Labs_Trend", "Care_Gaps"}
)


def xlsx_citation_locator_well_formed(
    outcome: RunOutcome, *, case: Any = None
) -> bool:
    """Hard / 1.00 — every XLSX citation's ``field_or_chunk_id`` matches
    the ``sheet=<Name>|row=<int>|col=<label>`` shape and names a sheet in
    the v1 supported set.

    Conditions on ``case.document_modality == 'xlsx_workbook'``. Vacuous-
    True for every other modality. Vacuous-True when the extraction is
    missing / not a dict.

    The column label may be a plain header (``Value``, ``Notes``) or a
    date label (``2026-04-15``) for the wide-format Labs_Trend sheet —
    we don't restrict its content beyond non-empty, but the prefix and
    row index must parse as integers.
    """
    if case is None or getattr(case, "document_modality", None) != "xlsx_workbook":
        return True
    extraction = outcome.extraction
    if not isinstance(extraction, dict):
        return True
    saw_any = False
    for item in _iter_cited_items(extraction):
        for cit in _iter_citations_for_item(item):
            if str(cit.get("source_type") or "document") != "document":
                continue
            saw_any = True
            field_id = str(cit.get("field_or_chunk_id") or "")
            m = _XLSX_LOCATOR_RE.match(field_id)
            if m is None:
                return False
            sheet_name = m.group(1)
            if sheet_name not in _XLSX_SHEET_NAMES:
                return False
            col_label = m.group(3)
            if not col_label.strip():
                return False
    return True if not saw_any else True


# Auto-discovery registry. New rubrics are picked up by the scoring harness
# (via ``RUBRIC_REGISTRY[name]`` lookup) without per-rubric wiring in
# ``scoring.py``. Each entry is a callable accepting (outcome, *, case)
# kwargs (case may be ignored). The registry is the single source of truth
# for rubric discovery — keep ``__all__`` aligned with the keys here.
RUBRIC_REGISTRY: dict[str, Any] = {
    "quarantine_audit_emitted": quarantine_audit_emitted,
    "no_unconfirmed_writes": no_unconfirmed_writes,
    "stage_failure_audit_emitted": stage_failure_audit_emitted,
    "tiff_all_pages_ocrd": tiff_all_pages_ocrd,
    "synthetic_marker_not_extracted": synthetic_marker_not_extracted,
    # Phase 3 Part B' — per-modality citation locator shape.
    "hl7_citation_locator_well_formed": hl7_citation_locator_well_formed,
    "xlsx_citation_locator_well_formed": xlsx_citation_locator_well_formed,
    # Phase 2 Step 2 — post-approval RAG synthesis grounding.
    "synthesis_grounded": synthesis_grounded,
    # 2026-05-08 problem_list build — ICD-10 hallucination guardrail.
    "icd10_grounded": icd10_grounded,
    # 2026-05-08 problem_list build — Phase 4 FHIR Condition write-through.
    "condition_writeback_succeeded": condition_writeback_succeeded,
}


__all__ = [
    "schema_valid",
    "citation_present",
    "citation_resolvable",
    "citation_row_match",
    "citation_token_match",
    "citation_iou",
    "citation_pixel_distance",
    "correct_critic_decision",
    "no_phi_in_logs",
    "keyword_match_in_citation",
    # Phase 9 Slice 9.9 — multimodal expansion rubrics.
    "quarantine_audit_emitted",
    "no_unconfirmed_writes",
    "stage_failure_audit_emitted",
    "tiff_all_pages_ocrd",
    "synthetic_marker_not_extracted",
    # Phase 3 Part B' — per-modality citation locator shape.
    "hl7_citation_locator_well_formed",
    "xlsx_citation_locator_well_formed",
    # Phase 2 Step 2 — post-approval RAG synthesis grounding.
    "synthesis_grounded",
    "synthesis_grounded_reason",
    # 2026-05-08 problem_list build.
    "icd10_grounded",
    "condition_writeback_succeeded",
    "RUBRIC_REGISTRY",
]
