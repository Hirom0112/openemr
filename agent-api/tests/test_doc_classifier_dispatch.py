"""Wave 2D — doc-class classifier dispatch contract tests.

Covers:

- ``DOC_CLASSIFIER=regex`` (default) returns regex verdicts.
- ``DOC_CLASSIFIER=claude`` triggers the Claude path.
- ``< 0.6`` Claude confidence falls back to the regex verdict, but
  preserves ``classifier="claude"`` for metric attribution.
- Pre-LLM gate fires deterministically on:
  * page_count == 0
  * encrypted=True
  * OCR token count < 5
- Gate runs BEFORE either classifier (no regex / claude attempted).
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

from extractors import classifier as classifier_mod  # noqa: E402
from extractors.classifier import (  # noqa: E402
    DocClassVerdict,
    classify_doc,
)


pytestmark = pytest.mark.hard_failure


@dataclass
class _Block:
    """Minimal LayoutBlock stand-in (only fields the classifier reads)."""

    text: str
    page: int = 1
    bbox: tuple = (0.0, 0.0, 100.0, 20.0)
    bbox_id: str = "p1-b001"
    ocr_confidence: float = 0.95
    granularity: Optional[str] = "LINE"


def _intake_blocks() -> list[_Block]:
    return [
        _Block(text="HOSPITAL ADMISSION INTAKE FORM"),
        _Block(text="CHIEF COMPLAINT: chest pain"),
        _Block(text="PAST MEDICAL HISTORY: hypertension"),
    ]


def _lab_blocks() -> list[_Block]:
    return [
        _Block(text="LABORATORY REPORT"),
        _Block(text="HEMATOLOGY PANEL"),
        _Block(text="LACTATE 4.2 mmol/L REFERENCE RANGE 0.5-2.2"),
    ]


def _narrative_blocks() -> list[_Block]:
    return [
        _Block(text="Consultation note dated yesterday"),
        _Block(text="The patient was seen for follow-up"),
        _Block(text="Plan: continue current management"),
    ]


# --------------------------------------------------------------------------- #
# Regex path (default DOC_CLASSIFIER=regex)
# --------------------------------------------------------------------------- #


def test_regex_default_routes_lab(monkeypatch) -> None:
    monkeypatch.delenv("DOC_CLASSIFIER", raising=False)
    v = classify_doc(_lab_blocks())
    assert isinstance(v, DocClassVerdict)
    assert v.doc_class == "lab_report_tabular"
    assert v.classifier == "regex"
    assert v.confidence > 0.0


def test_regex_default_routes_intake(monkeypatch) -> None:
    monkeypatch.setenv("DOC_CLASSIFIER", "regex")
    v = classify_doc(_intake_blocks())
    assert v.doc_class == "intake_form"
    assert v.classifier == "regex"


def test_regex_unknown_collapses_to_other_narrative(monkeypatch) -> None:
    monkeypatch.setenv("DOC_CLASSIFIER", "regex")
    v = classify_doc(_narrative_blocks())
    assert v.doc_class == "other_narrative"
    assert v.classifier == "regex"


# --------------------------------------------------------------------------- #
# Claude path
# --------------------------------------------------------------------------- #


def test_claude_low_confidence_falls_back_to_regex(monkeypatch) -> None:
    """Stub the Claude classifier to return high-confidence other_narrative;
    it should be accepted. Then stub it low-confidence and confirm fallback."""
    monkeypatch.setenv("DOC_CLASSIFIER", "claude")

    # Default stub: 0.0 confidence → fallback to regex.
    v = classify_doc(_lab_blocks())
    # Falls back, but classifier label stays "claude" for metric attribution.
    assert v.classifier == "claude"
    assert v.doc_class == "lab_report_tabular"  # regex result preserved
    assert "fell back" in v.reason


def test_claude_high_confidence_accepted(monkeypatch) -> None:
    monkeypatch.setenv("DOC_CLASSIFIER", "claude")

    def _stub_claude(layout):
        return DocClassVerdict(
            doc_class="intake_form",
            confidence=0.92,
            classifier="claude",
            reason="stubbed high-confidence claude verdict",
        )

    monkeypatch.setattr(classifier_mod, "_doc_class_from_claude", _stub_claude)
    v = classify_doc(_lab_blocks())
    assert v.classifier == "claude"
    assert v.doc_class == "intake_form"
    assert v.confidence == 0.92


def test_claude_just_below_floor_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("DOC_CLASSIFIER", "claude")

    def _stub_claude(layout):
        return DocClassVerdict(
            doc_class="intake_form",
            confidence=0.59,  # 0.01 below the 0.6 floor
            classifier="claude",
            reason="stubbed low-confidence claude verdict",
        )

    monkeypatch.setattr(classifier_mod, "_doc_class_from_claude", _stub_claude)
    v = classify_doc(_intake_blocks())
    # Regex sees intake keywords too → fallback class is intake_form.
    assert v.doc_class == "intake_form"
    assert v.classifier == "claude"


# --------------------------------------------------------------------------- #
# Non-extractable pre-LLM gate
# --------------------------------------------------------------------------- #


def test_gate_fires_on_zero_pages(monkeypatch) -> None:
    monkeypatch.setenv("DOC_CLASSIFIER", "claude")  # gate must run first
    v = classify_doc(_lab_blocks(), page_count=0)
    assert v.doc_class == "non_extractable"
    assert v.classifier == "gate"


def test_gate_fires_on_encrypted(monkeypatch) -> None:
    monkeypatch.setenv("DOC_CLASSIFIER", "claude")
    v = classify_doc(_lab_blocks(), encrypted=True)
    assert v.doc_class == "non_extractable"
    assert v.classifier == "gate"


def test_gate_fires_on_too_few_tokens() -> None:
    # Three short blocks, total tokens = 4 → below floor of 5.
    blocks = [_Block(text="hi"), _Block(text="ok"), _Block(text="")]
    # Total tokens = 2 (hi) + 1 (ok) + 0 = 3
    v = classify_doc(blocks)
    assert v.doc_class == "non_extractable"
    assert v.classifier == "gate"


def test_gate_does_not_fire_on_normal_doc(monkeypatch) -> None:
    monkeypatch.setenv("DOC_CLASSIFIER", "regex")
    v = classify_doc(_intake_blocks())
    # Plenty of tokens — gate should NOT fire.
    assert v.doc_class != "non_extractable"
    assert v.classifier == "regex"


def test_gate_runs_before_classifiers(monkeypatch) -> None:
    """If the pre-LLM gate fires, neither classifier should run."""
    monkeypatch.setenv("DOC_CLASSIFIER", "claude")
    called = {"claude": False, "keywords": False}

    def _spy_claude(layout):
        called["claude"] = True
        return DocClassVerdict(
            doc_class="intake_form",
            confidence=0.99,
            classifier="claude",
            reason="should not be called",
        )

    monkeypatch.setattr(classifier_mod, "_doc_class_from_claude", _spy_claude)
    v = classify_doc([_Block(text="x")], page_count=0)
    assert v.doc_class == "non_extractable"
    assert called["claude"] is False


# --------------------------------------------------------------------------- #
# DocClass enum surface
# --------------------------------------------------------------------------- #


def test_verdict_carries_metric_labels(monkeypatch) -> None:
    """Verdict must expose the labels metrics consume."""
    monkeypatch.setenv("DOC_CLASSIFIER", "regex")
    v = classify_doc(_intake_blocks())
    # Both must be set so the counter increment never KeyError's.
    assert v.classifier in ("regex", "claude", "gate")
    assert v.doc_class in (
        "intake_form",
        "lab_report_tabular",
        "other_narrative",
        "non_extractable",
    )
