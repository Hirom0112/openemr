"""Keyword fast-path document classifier (W2_ARCHITECTURE.md §5.3 step 2).

This is a deterministic, dependency-free first pass over the OCR layout that
short-circuits the expensive LLM classifier when high-signal keywords are
present.  Returns ``None`` when nothing fires so the caller can fall back to
the LLM classifier or to ``UnknownDocument``.

PSR-3 logging only (no prompt or completion text); no PHI emission — keyword
matches are coarse document-class signals, not patient values.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Literal, Optional

from documents.ocr import LayoutBlock

logger = logging.getLogger(__name__)


_LAB_RE = re.compile(
    r"\b(LABORATORY|LAB REPORT|LACTATE|LOINC|REFERENCE RANGE|HEMATOLOGY|CHEMISTRY)\b",
    re.IGNORECASE,
)
_INTAKE_RE = re.compile(
    r"\b(ADMISSION|INTAKE|TRIAGE|"
    r"HISTORY OF PRESENT ILLNESS|HPI|"
    r"CHIEF COMPLAINT|CHIEF CONCERN|"
    r"PATIENT DEMOGRAPHICS|"
    r"PROBLEM LIST|PMH|PAST MEDICAL HISTORY|"
    r"SOCIAL HISTORY|FAMILY HISTORY|"
    r"REVIEW OF SYSTEMS|ROS|"
    r"ADVANCE DIRECTIVE|CODE STATUS)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClassifierVerdict:
    """The output of the keyword fast-path classifier."""

    kind: Literal["lab_report", "intake_form", "unknown"]
    confidence: float
    reason: str


def _confidence_for(n_matches: int) -> float:
    """0.95 if 2+ matches, 0.80 if exactly 1."""
    if n_matches >= 2:
        return 0.95
    return 0.80


def _count_matches(blocks: Iterable[LayoutBlock], regex: re.Pattern[str]) -> List[str]:
    """Return the list of matched keyword strings across all blocks."""
    found: List[str] = []
    for b in blocks:
        for m in regex.finditer(b.text):
            found.append(m.group(0).upper())
    return found


def classify_keywords(layout: List[LayoutBlock]) -> Optional[ClassifierVerdict]:
    """Run the fast-path keyword classifier over an OCR layout.

    Returns a verdict with ``kind`` of ``"lab_report"`` or ``"intake_form"``
    if high-signal keywords are present, otherwise ``None``.  The caller is
    responsible for the fallback path (LLM classifier or UnknownDocument).
    """
    lab_hits = _count_matches(layout, _LAB_RE)
    intake_hits = _count_matches(layout, _INTAKE_RE)

    # Prefer the kind with more matches; ties go to lab_report (clinically
    # higher value for triage), but only when lab also has at least one hit.
    if lab_hits and len(lab_hits) >= len(intake_hits):
        confidence = _confidence_for(len(lab_hits))
        verdict = ClassifierVerdict(
            kind="lab_report",
            confidence=confidence,
            reason=f"matched {len(lab_hits)} lab keyword(s)",
        )
        logger.info(
            "extractor_classifier_verdict",
            extra={
                "kind": verdict.kind,
                "confidence": verdict.confidence,
                "n_matches": len(lab_hits),
            },
        )
        return verdict

    if intake_hits:
        confidence = _confidence_for(len(intake_hits))
        verdict = ClassifierVerdict(
            kind="intake_form",
            confidence=confidence,
            reason=f"matched {len(intake_hits)} intake keyword(s)",
        )
        logger.info(
            "extractor_classifier_verdict",
            extra={
                "kind": verdict.kind,
                "confidence": verdict.confidence,
                "n_matches": len(intake_hits),
            },
        )
        return verdict

    logger.info(
        "extractor_classifier_verdict",
        extra={"kind": None, "confidence": 0.0, "n_matches": 0},
    )
    return None
