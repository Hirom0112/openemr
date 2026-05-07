"""DOCX paragraph + run extractor for referral letters (Phase 9 Slice 9.6).

Walks a ``python-docx`` Document into an ordered list of paragraphs, each
carrying its 1-based ``para_idx`` (continuing across body + tables) and the
list of runs inside it. Section-heading attribution is decided by a
forward-pass leading-bold rule: a paragraph whose FIRST run is bold and
short (≤ 6 words) is treated as a section header; subsequent paragraphs
inherit that section name until the next leading-bold paragraph rolls it
forward.

Synthetic locator grammar (Slice 9.6 §592):

    para={N}                — paragraph-level citation
    para={N}|run={M}        — run-level citation (preferred when the value
                              lives inside a single run; this is how the
                              prose extractor cites e.g. "LDL-C at 142
                              mg/dL" inside a multi-run HPI paragraph)

``N`` and ``M`` are 1-based, document-order. ``bbox`` and ``page`` stay
``None`` for DOCX-sourced citations.

Edge-case handling per spec:

- Tables: paragraphs inside ``tables[].rows[].cells[].paragraphs[]`` are
  walked AFTER body paragraphs, continuing the same ``para_idx`` counter
  with style ``TableCell:rR:cC:pN`` so the locator stays unambiguous.
- Embedded images: dropped; we log ``docx_image_dropped`` once per image so
  a missing inline figure is auditable.
- Tracked changes: consolidated to the rendered text (python-docx returns
  the accepted/visible text); a ``docx_tracked_changes_present`` flag fires
  when the document's XML carries any ``w:ins`` / ``w:del`` element so the
  caller can soft-warn.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from agent.metrics import (
    agent_doc_parser_calls_total,
    agent_docx_parse_duration_seconds,
)
from observability.tool_logging import log_tool_outcome

logger = logging.getLogger(__name__)

# DOCX is a Zip64 archive; both the regular and (rare) empty-archive
# signatures get accepted because python-docx will reject malformed
# packages itself. The two-signature strategy is intentional — restricting
# to PK\x03\x04 misses the empty/end-of-central-directory variant some
# fax-stack DOCX exporters emit.
_DOCX_MAGIC_PRIMARY = b"PK\x03\x04"
_DOCX_MAGIC_EMPTY = b"PK\x05\x06"
_DOCX_MAGIC_SPANNED = b"PK\x07\x08"


def is_docx(data: bytes) -> bool:
    """True iff ``data`` begins with a Zip-archive magic signature.

    NOTE: this is a *necessary* but not *sufficient* check — every DOCX is a
    zip but not every zip is a DOCX. The caller in ``ocr.extract_layout`` is
    expected to dispatch by extension/MIME first; this function is the
    fallback for naked-bytes inputs where the loader will trust python-docx
    to reject non-DOCX zips.
    """
    return (
        data.startswith(_DOCX_MAGIC_PRIMARY)
        or data.startswith(_DOCX_MAGIC_EMPTY)
        or data.startswith(_DOCX_MAGIC_SPANNED)
    )


@dataclass(frozen=True)
class DocxRun:
    """One run inside a DOCX paragraph. ``run_idx`` is 1-based within its
    parent paragraph; ``bold`` mirrors python-docx's tristate (None when
    unset, inheriting from the paragraph style)."""

    run_idx: int
    text: str
    bold: Optional[bool]


@dataclass(frozen=True)
class DocxParagraph:
    """One paragraph in a DOCX, body or table-cell. ``para_idx`` is 1-based
    document-order (continuing across body + tables). ``style`` is either
    the paragraph's python-docx style name OR ``"TableCell:rR:cC:pN"`` for
    paragraphs inside a table cell. ``section`` is the closest preceding
    leading-bold heading (or ``None`` for paragraphs above the first
    heading)."""

    para_idx: int
    style: str
    text: str
    runs: Tuple[DocxRun, ...]
    section: Optional[str] = None
    # 1-based row/col when this paragraph lives inside a table cell;
    # ``None`` for body paragraphs. Useful for round-tripping locator
    # grammar without re-parsing ``style``.
    table_row: Optional[int] = None
    table_col: Optional[int] = None


def _paragraph_runs(paragraph) -> List[DocxRun]:
    runs: List[DocxRun] = []
    for j, r in enumerate(paragraph.runs, start=1):
        runs.append(DocxRun(run_idx=j, text=r.text or "", bold=r.bold))
    return runs


def _is_leading_bold_heading(runs: List[DocxRun], text: str) -> bool:
    """A paragraph qualifies as a section heading iff:

    - it has at least one run AND
    - the first run is bold (run.bold is True, NOT None) AND
    - the paragraph has at most 6 whitespace tokens (headings are short).

    The 6-token cap intentionally rejects the HPI / Reason for Referral
    style paragraphs whose leading bold run is ONLY the field label
    ("History of Present Illness:") followed by a long body run — those
    are field-label-prefix paragraphs, not section headings.

    Decision is text-tokens-on-paragraph-as-a-whole, NOT runs-on-first-run,
    so a heading split across two bold runs ("PAST" + " MEDICAL HISTORY")
    still resolves correctly.
    """
    if not runs:
        return False
    first = runs[0]
    if first.bold is not True:
        return False
    tokens = text.split()
    return 1 <= len(tokens) <= 6


def _has_tracked_changes(doc) -> bool:
    """True iff the document's XML carries any ``w:ins`` or ``w:del``
    element. python-docx surfaces the rendered (accepted) text, but the
    raw OOXML still carries the change history; we surface the signal so
    the caller can soft-warn."""
    try:
        body_xml = doc.element.body.xml  # type: ignore[attr-defined]
    except Exception:
        return False
    return ("w:ins" in body_xml) or ("w:del" in body_xml)


def _count_inline_images(paragraph) -> int:
    """Count inline-image relationships referenced by this paragraph's XML.

    We don't extract the images — they're explicitly dropped per Slice 9.6
    spec — but counting them lets us emit one ``docx_image_dropped`` log
    per image so the audit trail captures what we ignored.
    """
    try:
        xml = paragraph._p.xml  # type: ignore[attr-defined]
    except Exception:
        return 0
    # Each inline / floating image is wrapped in a ``w:drawing`` element.
    return xml.count("<w:drawing") + xml.count("<w:drawing ")


def extract_docx_paragraphs(
    docx_bytes: bytes,
) -> Tuple[List[DocxParagraph], dict]:
    """Parse a DOCX byte stream into an ordered ``(paragraphs, meta)`` tuple.

    ``meta`` carries audit-relevant flags:
        ``tracked_changes_present`` : bool — see :func:`_has_tracked_changes`.
        ``embedded_images_dropped`` : int — total inline-image count we skipped.
        ``n_paragraphs``            : int — len(paragraphs) (convenience).

    On failure to load (corrupt zip, non-DOCX bytes), returns
    ``([], {"tracked_changes_present": False, "embedded_images_dropped": 0,
    "n_paragraphs": 0, "load_error": True})`` and emits a warning log.
    Never raises.
    """
    started = time.perf_counter()
    paragraphs: List[DocxParagraph] = []
    meta: dict = {
        "tracked_changes_present": False,
        "embedded_images_dropped": 0,
        "n_paragraphs": 0,
        "load_error": False,
    }

    try:
        # Local import — python-docx is a soft requirement; the module
        # should still import in a slim env without it.
        import docx as _docx  # type: ignore

        doc = _docx.Document(io.BytesIO(docx_bytes))
    except Exception:
        logger.warning("docx_loader.load_failed", exc_info=True)
        meta["load_error"] = True
        agent_doc_parser_calls_total.labels(
            format="docx", outcome="errored"
        ).inc()
        agent_docx_parse_duration_seconds.labels(outcome="errored").observe(
            time.perf_counter() - started
        )
        return paragraphs, meta

    meta["tracked_changes_present"] = _has_tracked_changes(doc)

    counter = 0
    current_section: Optional[str] = None
    images_dropped = 0

    # Body paragraphs first.
    for paragraph in doc.paragraphs:
        counter += 1
        runs = _paragraph_runs(paragraph)
        text = paragraph.text or ""
        n_images = _count_inline_images(paragraph)
        for _ in range(n_images):
            logger.info(
                "docx_image_dropped",
                extra={"para_idx": counter, "context": "body"},
            )
        images_dropped += n_images

        if _is_leading_bold_heading(runs, text):
            # Strip the trailing colon often present on referral-letter
            # headings ("Past Medical History:" → "Past Medical History").
            stripped = text.rstrip(":").strip()
            current_section = stripped or None

        style_name = "Normal"
        try:
            style_name = paragraph.style.name or "Normal"
        except Exception:
            pass

        paragraphs.append(
            DocxParagraph(
                para_idx=counter,
                style=style_name,
                text=text,
                runs=tuple(runs),
                section=current_section,
            )
        )

    # Table cell paragraphs second; continue para_idx counter so locator
    # grammar stays document-order-unique.
    for table in doc.tables:
        for r_idx, row in enumerate(table.rows, start=1):
            for c_idx, cell in enumerate(row.cells, start=1):
                for p_in_cell, paragraph in enumerate(cell.paragraphs, start=1):
                    counter += 1
                    runs = _paragraph_runs(paragraph)
                    text = paragraph.text or ""
                    n_images = _count_inline_images(paragraph)
                    for _ in range(n_images):
                        logger.info(
                            "docx_image_dropped",
                            extra={
                                "para_idx": counter,
                                "context": "table",
                                "row": r_idx,
                                "col": c_idx,
                            },
                        )
                    images_dropped += n_images

                    if _is_leading_bold_heading(runs, text):
                        stripped = text.rstrip(":").strip()
                        current_section = stripped or None

                    style = f"TableCell:r{r_idx}:c{c_idx}:p{p_in_cell}"
                    paragraphs.append(
                        DocxParagraph(
                            para_idx=counter,
                            style=style,
                            text=text,
                            runs=tuple(runs),
                            section=current_section,
                            table_row=r_idx,
                            table_col=c_idx,
                        )
                    )

    meta["embedded_images_dropped"] = images_dropped
    meta["n_paragraphs"] = len(paragraphs)

    duration_s = time.perf_counter() - started
    outcome = "success" if paragraphs else "empty"
    agent_doc_parser_calls_total.labels(format="docx", outcome=outcome).inc()
    agent_docx_parse_duration_seconds.labels(outcome=outcome).observe(duration_s)
    log_tool_outcome(
        tool_name="docx_loader",
        duration_ms=int(duration_s * 1000),
        cache="n/a",
        extra={
            "outcome": outcome,
            "n_paragraphs": len(paragraphs),
            "tracked_changes_present": meta["tracked_changes_present"],
            "embedded_images_dropped": meta["embedded_images_dropped"],
        },
    )
    return paragraphs, meta


def format_locator(para_idx: int, run_idx: Optional[int] = None) -> str:
    """Produce the synthetic locator string for a (paragraph, run) pair.

    ``para=N`` when ``run_idx`` is None; ``para=N|run=M`` otherwise. Both
    indices are 1-based per Slice 9.6 spec.
    """
    if run_idx is None:
        return f"para={para_idx}"
    return f"para={para_idx}|run={run_idx}"


__all__ = [
    "DocxParagraph",
    "DocxRun",
    "extract_docx_paragraphs",
    "format_locator",
    "is_docx",
]
