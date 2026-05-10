"""Phase 3 Type-A — under-escalation regression.

The critic systematically returned ``pass`` for ~35 fixture cases that
expected ``soft_warn``. The buckets uncovered by existing detectors:

* ``intra_doc_conflict`` — a single document carries contradictory values
  for the same test (e.g. lactate 4.2 on page 1, 2.4 on page 3; XLSX
  Labs_Trend with the same loinc twice).
* ``wrong_type_hint`` — the type hint disagrees with what the classifier
  actually emitted (``intake_form`` hint, ``lab_report`` content).
* ``mixed_content`` — packet appears to mix more than one patient.

The rubric ``correct_critic_decision`` (rubrics_mechanical.py:713) checks
only the decision string, so the assertions below pin ``soft_warn`` and
additionally verify the soft-warn code so downstream UI can differentiate.

Each detector has at least one positive (triggers) and one negative
(must not false-positive) test.
"""

from __future__ import annotations

from typing import Any

import pytest

from graph.nodes.critic import critic_node
from graph.state import make_initial_state

pytestmark = pytest.mark.hard_failure


def _state(**overrides: Any) -> dict[str, Any]:
    state = make_initial_state(
        request_id="req-undr", session_id="sess-undr", provider_id="prov-undr"
    )
    state.update(overrides)  # type: ignore[arg-type]
    return state  # type: ignore[return-value]


def _layout_block(bbox_id: str, text: str, conf: float = 1.0) -> dict[str, Any]:
    return {
        "bbox_id": bbox_id,
        "page": 1,
        "bbox": [0.0, 0.0, 100.0, 20.0],
        "text": text,
        "ocr_confidence": conf,
    }


def _lab_value(
    *,
    test_name: str,
    value: str,
    bbox_id: str,
    quote: str,
) -> dict[str, Any]:
    return {
        "test_name": test_name,
        "normalized_test_name": test_name.lower(),
        "value": value,
        "unit": "mmol/L",
        "normalized_unit": "mmol/l",
        "reference_range": "0.5-2.0",
        "collection_date": "2024-01-15",
        "abnormal_flag": "high",
        "citations": [
            {
                "source_type": "document",
                "source_id": "DocumentReference/abc",
                "page_or_section": "p1",
                "field_or_chunk_id": bbox_id,
                "quote_or_value": quote,
            }
        ],
    }


def _lab_report(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/abc",
        "collection_facility": None,
        "values": values,
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


# ── intra_doc_conflict ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_intra_doc_conflict_two_values_same_test_softwarns() -> None:
    """Lactate 4.2 (p1) vs 2.4 (p3) -> soft_warn intra_doc_conflict."""
    extraction = _lab_report(
        [
            _lab_value(test_name="Lactate", value="4.2", bbox_id="p1-b001", quote="4.2"),
            _lab_value(test_name="Lactate", value="2.4", bbox_id="p3-b001", quote="2.4"),
        ]
    )
    layout = [
        _layout_block("p1-b001", "Lactate 4.2 mmol/L"),
        _layout_block("p3-b001", "Lactate 2.4 mmol/L"),
    ]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    assert out["critic_decision"] == "soft_warn"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "intra_doc_conflict" in codes


@pytest.mark.asyncio
async def test_intra_doc_conflict_negative_identical_repeats() -> None:
    """Two rows with the same value are not a conflict."""
    extraction = _lab_report(
        [
            _lab_value(test_name="Lactate", value="4.2", bbox_id="p1-b001", quote="4.2"),
            _lab_value(test_name="Lactate", value="4.2", bbox_id="p3-b001", quote="4.2"),
        ]
    )
    layout = [
        _layout_block("p1-b001", "Lactate 4.2 mmol/L"),
        _layout_block("p3-b001", "Lactate 4.2 mmol/L"),
    ]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    assert out["critic_decision"] == "pass"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "intra_doc_conflict" not in codes


@pytest.mark.asyncio
async def test_intra_doc_conflict_negative_distinct_tests() -> None:
    """Different tests with different values are not a conflict."""
    extraction = _lab_report(
        [
            _lab_value(test_name="Lactate", value="4.2", bbox_id="p1-b001", quote="4.2"),
            _lab_value(test_name="HbA1c", value="6.8", bbox_id="p2-b001", quote="6.8"),
        ]
    )
    layout = [
        _layout_block("p1-b001", "Lactate 4.2 mmol/L"),
        _layout_block("p2-b001", "HbA1c 6.8"),
    ]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    assert out["critic_decision"] == "pass"


# ── wrong_type_hint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_type_hint_intake_hint_lab_content_softwarns() -> None:
    """doc_type_hint=intake_form, classifier emitted lab_report -> soft_warn."""
    extraction = _lab_report(
        [_lab_value(test_name="Lactate", value="4.2", bbox_id="p1-b001", quote="4.2")]
    )
    layout = [_layout_block("p1-b001", "Lactate 4.2 mmol/L")]
    out = await critic_node(
        _state(
            extraction=extraction,
            ocr_layout=layout,
            doc_type_hint="intake_form",
        )
    )
    assert out["critic_decision"] == "soft_warn"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "classifier_confidence_low" in codes


@pytest.mark.asyncio
async def test_wrong_type_hint_negative_matching_hint() -> None:
    """doc_type_hint=lab_report on a lab_report -> no soft-warn."""
    extraction = _lab_report(
        [_lab_value(test_name="Lactate", value="4.2", bbox_id="p1-b001", quote="4.2")]
    )
    layout = [_layout_block("p1-b001", "Lactate 4.2 mmol/L")]
    out = await critic_node(
        _state(
            extraction=extraction,
            ocr_layout=layout,
            doc_type_hint="lab_report",
        )
    )
    assert out["critic_decision"] == "pass"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "classifier_confidence_low" not in codes


@pytest.mark.asyncio
async def test_wrong_type_hint_negative_no_hint() -> None:
    """No hint -> can't disagree, no soft-warn."""
    extraction = _lab_report(
        [_lab_value(test_name="Lactate", value="4.2", bbox_id="p1-b001", quote="4.2")]
    )
    layout = [_layout_block("p1-b001", "Lactate 4.2 mmol/L")]
    out = await critic_node(
        _state(extraction=extraction, ocr_layout=layout, doc_type_hint=None)
    )
    assert out["critic_decision"] == "pass"


# ── mixed_content ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_content_via_summary_softwarns() -> None:
    """Unknown extraction whose summary says 'mixed content' -> soft_warn."""
    extraction = {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/mixed",
        "document_kind_guess": "mixed_content",
        "summary": "Packet contains pages from two patients (Webb, Olsson).",
        "key_facts": [
            {
                "text": "Packet appears to contain Webb labs + Olsson intake.",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/mixed",
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-b001",
                        "quote_or_value": "Olsson",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.4,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }
    layout = [_layout_block("p1-b001", "Patient: Olsson, Yara")]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    assert out["critic_decision"] == "soft_warn"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "mixed_content_detected" in codes


@pytest.mark.asyncio
async def test_intra_doc_conflict_negative_trended_labs_distinct_dates() -> None:
    """Same test name on two different ``collection_date`` values must NOT fire.

    This is the realistic table_heavy / scanned_pdf trended-labs pattern
    (HbA1c on visit 1 = 8.2 collected 2024-01-15, visit 2 = 7.6 collected
    2024-04-15). The original Phase-3 detector fired on this — the
    rgression-narrowing fix in critic.py now scopes "conflict" to
    duplicate name + same date.
    """
    v1 = _lab_value(test_name="HbA1c", value="8.2", bbox_id="p1-b001", quote="8.2")
    v1["collection_date"] = "2024-01-15"
    v2 = _lab_value(test_name="HbA1c", value="7.6", bbox_id="p2-b001", quote="7.6")
    v2["collection_date"] = "2024-04-15"
    extraction = _lab_report([v1, v2])
    layout = [
        _layout_block("p1-b001", "HbA1c 8.2 (2024-01-15)"),
        _layout_block("p2-b001", "HbA1c 7.6 (2024-04-15)"),
    ]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "intra_doc_conflict" not in codes
    assert out["critic_decision"] == "pass"


@pytest.mark.asyncio
async def test_intra_doc_conflict_positive_same_date_different_values() -> None:
    """Two HbA1c rows with the same collection_date but different values.

    The narrowed detector still fires when the dates match — same draw,
    contradictory values is a real conflict.
    """
    v1 = _lab_value(test_name="HbA1c", value="8.2", bbox_id="p1-b001", quote="8.2")
    v1["collection_date"] = "2024-01-15"
    v2 = _lab_value(test_name="HbA1c", value="6.4", bbox_id="p2-b001", quote="6.4")
    v2["collection_date"] = "2024-01-15"
    extraction = _lab_report([v1, v2])
    layout = [
        _layout_block("p1-b001", "HbA1c 8.2"),
        _layout_block("p2-b001", "HbA1c 6.4"),
    ]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    assert out["critic_decision"] == "soft_warn"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "intra_doc_conflict" in codes


@pytest.mark.asyncio
async def test_wrong_type_hint_negative_actual_unknown() -> None:
    """Hint=lab_report on an ``unknown`` extraction must NOT fire.

    ``unknown`` is the classifier's "no opinion" verdict (keyword match
    below threshold). It's not a disagreement with the hint — the hint
    may well be right. Firing here was the table_heavy / blank_noise
    regression: every bbox_gt_table_* synthetic fixture (12 cases) hits
    this path because the synthetic surface lacks lab keyword density.
    """
    extraction = {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/synth",
        "document_kind_guess": "unknown",
        "summary": "Synthetic table content; classifier had no opinion.",
        "key_facts": [
            {
                "text": "Sodium 139 mEq/L",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/synth",
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-b001",
                        "quote_or_value": "Sodium",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.4,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }
    layout = [_layout_block("p1-b001", "Sodium 139 mEq/L")]
    out = await critic_node(
        _state(
            extraction=extraction,
            ocr_layout=layout,
            doc_type_hint="lab_report",
        )
    )
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "classifier_confidence_low" not in codes
    assert out["critic_decision"] == "pass"


@pytest.mark.asyncio
async def test_wrong_type_hint_unknown_kind_with_concrete_guess_fires() -> None:
    """Refinement: when extractor falls back to ``kind=unknown`` but the
    keyword classifier produced a concrete non-lab verdict that's carried
    on ``document_kind_guess``, that's a real classifier disagreement
    with the hint — fire ``classifier_confidence_low``.

    Recovers multi_column wrong_type_hint / mixed_content cases where the
    lab extractor falls back to UnknownDocument but the keyword classifier
    confidently identified the content as ``intake_form`` (carried on
    ``document_kind_guess``). Without this, the table_heavy narrowing
    silently swallowed those soft_warn signals.
    """
    extraction = {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/x",
        "document_kind_guess": "intake_form",  # classifier IS confident here
        "summary": "Intake form content surfaced through the lab extractor fallback.",
        "key_facts": [
            {
                "text": "Allergies: penicillin",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/x",
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-b001",
                        "quote_or_value": "Allergies",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.8,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }
    layout = [_layout_block("p1-b001", "Allergies: penicillin")]
    out = await critic_node(
        _state(
            extraction=extraction,
            ocr_layout=layout,
            doc_type_hint="lab_report",  # hint mismatches concrete guess
        )
    )
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "classifier_confidence_low" in codes
    assert out["critic_decision"] == "soft_warn"


@pytest.mark.asyncio
async def test_mixed_content_negative_clean_unknown() -> None:
    """A normal UnknownDocument must not trip the mixed-content detector."""
    extraction = {
        "kind": "unknown",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/clean",
        "document_kind_guess": "consultant_note",
        "summary": "Cardiology consult letter discussing recent MI.",
        "key_facts": [
            {
                "text": "Cardiology consult.",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/clean",
                        "page_or_section": "p1",
                        "field_or_chunk_id": "p1-b001",
                        "quote_or_value": "Cardiology",
                    }
                ],
            }
        ],
        "classifier_confidence": 0.7,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }
    layout = [_layout_block("p1-b001", "Cardiology consult — recent MI")]
    out = await critic_node(_state(extraction=extraction, ocr_layout=layout))
    assert out["critic_decision"] == "pass"
    codes = [w.get("code") for w in out["soft_warns"]]
    assert "mixed_content_detected" not in codes
