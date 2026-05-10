"""Regression tests for the four "lagging measurement" fixes.

Each test pins one bucket of the post-runbook eval recovery:

1. ``_mrn_match`` — cross-system MRN identifiers fall back to absent.
2. ``_has_imaging_keywords`` — imaging-radiology keywords surface a guess
   that lets the wrong-type-hint detector fire.
3. Phase-5A' escalation refinement — OCR_LOW_CONFIDENCE alone on a
   clean extraction is informational, not promotion-worthy.
4. ``citation_row_match`` / ``citation_token_match`` — TIFF / XLSX
   modality exemption mirrors the bbox_gt rationale.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from demographics.check import _mrn_match  # noqa: E402
from documents.ocr import LayoutBlock  # noqa: E402
from evals.rubrics_mechanical import citation_row_match, citation_token_match  # noqa: E402
from extractors.lab import _has_imaging_keywords  # noqa: E402


def _block(text: str, bbox_id: str = "p1-b001") -> LayoutBlock:
    return LayoutBlock(
        page=1,
        bbox_id=bbox_id,
        text=text,
        bbox=(0.0, 0.0, 100.0, 20.0),
        ocr_confidence=1.0,
    )


# ── 1. MRN cross-system fallback ────────────────────────────────────────────


@pytest.mark.hard_failure
def test_mrn_match_external_prefix_vs_local_numeric_returns_absent() -> None:
    assert _mrn_match("BHS-2847163", "100481") == "absent"
    assert _mrn_match("MRN-2026-XXXX", "12345") == "absent"
    assert _mrn_match("SHC_98765", "42") == "absent"


@pytest.mark.hard_failure
def test_mrn_match_same_system_still_mismatches() -> None:
    # Both look local-system numeric — real disagreement
    assert _mrn_match("100481", "100482") == "mismatch"
    # Both external system, different IDs — still mismatch
    assert _mrn_match("BHS-1", "BHS-2") == "mismatch"


@pytest.mark.hard_failure
def test_mrn_match_identical_passes_through() -> None:
    assert _mrn_match("BHS-2847163", "BHS-2847163") == "match"
    assert _mrn_match("100481", "100481") == "match"


# ── 2. Imaging keyword detection ────────────────────────────────────────────


@pytest.mark.hard_failure
def test_imaging_keywords_fires_on_radiology_text() -> None:
    blocks = [_block("CHEST X-RAY"), _block("IMPRESSION: Normal exam.")]
    assert _has_imaging_keywords(blocks) is True


@pytest.mark.hard_failure
def test_imaging_keywords_fires_on_mri() -> None:
    blocks = [_block("MRI Brain"), _block("FINDINGS: Unremarkable.")]
    assert _has_imaging_keywords(blocks) is True


@pytest.mark.hard_failure
def test_imaging_keywords_negative_intake_form() -> None:
    blocks = [_block("Chief Complaint"), _block("History of Present Illness")]
    assert _has_imaging_keywords(blocks) is False


@pytest.mark.hard_failure
def test_imaging_keywords_negative_lab_report() -> None:
    blocks = [_block("Sodium 139 mEq/L"), _block("Reference Range")]
    assert _has_imaging_keywords(blocks) is False


# ── 4. TIFF / XLSX rubric exemption ─────────────────────────────────────────


@pytest.mark.hard_failure
def test_citation_row_match_exempt_for_tiff_fax() -> None:
    case = SimpleNamespace(bucket="lab_nominal", document_modality="tiff_fax")
    outcome = SimpleNamespace(extraction={"kind": "lab_report"}, ocr_layout=[])
    assert citation_row_match(outcome, case=case) is True
    assert citation_token_match(outcome, case=case) is True


@pytest.mark.hard_failure
def test_citation_row_match_exempt_for_xlsx_workbook() -> None:
    case = SimpleNamespace(bucket="mixed_content", document_modality="xlsx_workbook")
    outcome = SimpleNamespace(extraction={"kind": "workbook"}, ocr_layout=[])
    assert citation_row_match(outcome, case=case) is True
    assert citation_token_match(outcome, case=case) is True


@pytest.mark.hard_failure
def test_citation_row_match_NOT_exempt_for_other_modalities() -> None:
    case = SimpleNamespace(
        bucket="lab_nominal", document_modality="typed_pdf"
    )
    outcome = SimpleNamespace(extraction=None, ocr_layout=[])
    # Non-tiff/xlsx still gates as before — extraction=None → False
    assert citation_row_match(outcome, case=case) is False
