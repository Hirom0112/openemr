"""Phase 1 of problem_list build — classifier tie-break contract tests.

The legacy classifier sent ties (`len(lab_hits) >= len(intake_hits)`) to
``lab_report``. The 2026-05-08 problem_list build flipped this to strict
greater-than so intake wins ties — PMH-bearing intake forms that also
mention "lab" / "chemistry" anywhere in their HPI prose stay on the
intake schema (which has problem_list, family_history, chief_concern,
pertinent_labs), instead of being silently routed to LabReport (which
has only ``values: List[LabValue]`` and would drop every other clinical
signal).

These tests are pinned ``hard_failure`` per the project marker rule;
they must pass on every CI run.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.classifier import classify_keywords  # noqa: E402


pytestmark = pytest.mark.hard_failure


@dataclass
class _Block:
    text: str
    page: int = 1
    bbox: tuple = (0.0, 0.0, 100.0, 20.0)
    bbox_id: str = "p1-b001"
    ocr_confidence: float = 0.95
    granularity: Optional[str] = "LINE"


def _blocks(lab_terms: list[str], intake_terms: list[str]) -> list[_Block]:
    out: list[_Block] = []
    idx = 0
    for term in lab_terms + intake_terms:
        out.append(_Block(text=term, bbox_id=f"p1-b{idx:03d}"))
        idx += 1
    return out


def test_classifier_intake_wins_ties() -> None:
    """Equal lab + intake hit count → intake_form (Phase 1 fix).

    Pre-2026-05-08 behavior: this scenario routed to lab_report. Post-
    fix: intake wins ties because intake schemas carry the richer
    clinical surface (problem_list, family_history, chief_concern,
    pertinent_labs); lab schema has only LabValue entries and would
    drop every PMH/family/chief signal.
    """
    blocks = _blocks(
        lab_terms=["LABORATORY REPORT", "REFERENCE RANGE"],
        intake_terms=["CHIEF CONCERN", "PAST MEDICAL HISTORY"],
    )
    verdict = classify_keywords(blocks)
    assert verdict is not None
    assert verdict.kind == "intake_form", (
        f"Expected intake_form (intake wins ties post-2026-05-08), "
        f"got {verdict.kind} (reason: {verdict.reason})"
    )


def test_classifier_pure_lab_unchanged() -> None:
    """No intake keywords → lab_report stays lab_report."""
    blocks = _blocks(
        lab_terms=["LABORATORY REPORT", "REFERENCE RANGE", "HEMATOLOGY"],
        intake_terms=[],
    )
    verdict = classify_keywords(blocks)
    assert verdict is not None
    assert verdict.kind == "lab_report"


def test_classifier_pure_intake_unchanged() -> None:
    """No lab keywords → intake_form stays intake_form."""
    blocks = _blocks(
        lab_terms=[],
        intake_terms=[
            "CHIEF CONCERN",
            "PAST MEDICAL HISTORY",
            "FAMILY HISTORY",
        ],
    )
    verdict = classify_keywords(blocks)
    assert verdict is not None
    assert verdict.kind == "intake_form"


def test_classifier_lab_majority_unchanged() -> None:
    """Strictly more lab hits than intake → still lab_report."""
    blocks = _blocks(
        lab_terms=[
            "LABORATORY REPORT",
            "REFERENCE RANGE",
            "HEMATOLOGY",
            "CHEMISTRY",
            "LOINC",
        ],
        intake_terms=["CHIEF CONCERN"],
    )
    verdict = classify_keywords(blocks)
    assert verdict is not None
    assert verdict.kind == "lab_report"


def test_classifier_intake_majority_unchanged() -> None:
    """Strictly more intake hits than lab → intake_form."""
    blocks = _blocks(
        lab_terms=["LABORATORY REPORT"],
        intake_terms=[
            "CHIEF CONCERN",
            "PAST MEDICAL HISTORY",
            "FAMILY HISTORY",
            "ALLERGIES",
            "ADMISSION",
        ],
    )
    verdict = classify_keywords(blocks)
    assert verdict is not None
    assert verdict.kind == "intake_form"


def test_classifier_no_keywords_returns_none() -> None:
    """Plain prose → None (caller falls back to other_narrative)."""
    blocks = _blocks(
        lab_terms=[],
        intake_terms=[],
    )
    blocks.append(_Block(text="The patient came in today for a checkup.", bbox_id="p1-b099"))
    verdict = classify_keywords(blocks)
    assert verdict is None
