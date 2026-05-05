"""Tests for the Wave 2B+ nearest_label tiebreaker in the y-band repointer.

Contract under test (extractors.intake._select_best_candidate):

  - y-band (|Δy| to nearest anchor) is the PRIMARY sort key.
  - The LLM-supplied ``nearest_label`` participates only as a secondary
    tiebreaker among candidates that already tie on |Δy|.
  - When the label tiebreaker fires, the outcome label flips from
    ``repointed_with_anchor`` to ``repointed_with_anchor_label_tiebreak``
    so observability can distinguish the two cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.ocr import BlockGranularity, LayoutBlock  # noqa: E402
from extractors.intake import (  # noqa: E402
    _label_match_score,
    _select_best_candidate,
)

pytestmark = pytest.mark.hard_failure


def _block(
    bbox_id: str,
    text: str,
    *,
    page: int = 1,
    x: float = 0.0,
    y: float = 0.0,
    w: float = 100.0,
    h: float = 12.0,
    granularity: BlockGranularity = BlockGranularity.LINE,
) -> LayoutBlock:
    return LayoutBlock(
        bbox_id=bbox_id,
        page=page,
        bbox=(x, y, w, h),
        text=text,
        ocr_confidence=0.95,
        granularity=granularity,
    )


# --------------------------------------------------------------------------- #
# Equal y-band — label tiebreaker decides
# --------------------------------------------------------------------------- #


def test_label_tiebreak_picks_candidate_near_label() -> None:
    """Two candidates with identical |Δy| to the anchor; nearest_label
    only matches the neighborhood of candidate A — A wins."""
    anchor = _block("p1-b000", "DEMOGRAPHICS:", x=0.0, y=100.0, h=14.0)
    # Both candidates contain the same value text and sit at identical y.
    cand_a = _block("p1-b010", "06/08/1971", x=0.0, y=200.0)
    cand_b = _block("p1-b020", "06/08/1971", x=400.0, y=200.0)
    # "DOB" sits next to candidate A only.
    label_near_a = _block("p1-b011", "DOB", x=0.0, y=205.0, w=30.0)
    # An unrelated label near candidate B (must not look like "DOB").
    label_near_b = _block("p1-b021", "Phone", x=400.0, y=205.0, w=40.0)

    blocks = [anchor, cand_a, cand_b, label_near_a, label_near_b]
    candidates = [(cand_a, 8), (cand_b, 8)]

    chosen, _anchor, _dy, outcome, score, used = _select_best_candidate(
        candidates,
        anchors=[anchor],
        field_name="dob",
        blocks=blocks,
        nearest_label="DOB",
    )

    assert chosen.bbox_id == "p1-b010"
    assert outcome == "repointed_with_anchor_label_tiebreak"
    assert used is True
    assert score == 1.0


def test_label_tiebreak_outcome_unchanged_without_label() -> None:
    """No nearest_label supplied — outcome must remain the legacy
    ``repointed_with_anchor`` even when y-band ties."""
    anchor = _block("p1-b000", "DEMOGRAPHICS:", x=0.0, y=100.0)
    cand_a = _block("p1-b010", "06/08/1971", x=0.0, y=200.0)
    cand_b = _block("p1-b020", "06/08/1971", x=400.0, y=200.0)

    chosen, _a, _dy, outcome, score, used = _select_best_candidate(
        [(cand_a, 8), (cand_b, 8)],
        anchors=[anchor],
        field_name="dob",
        blocks=[anchor, cand_a, cand_b],
        nearest_label=None,
    )

    assert outcome == "repointed_with_anchor"
    assert used is False
    assert score == 0.0
    # First candidate in stable-sorted ties wins (deterministic legacy behavior).
    assert chosen.bbox_id == "p1-b010"


# --------------------------------------------------------------------------- #
# Differing y-band — y-band still wins regardless of label
# --------------------------------------------------------------------------- #


def test_yband_still_wins_when_label_points_at_farther_candidate() -> None:
    """Candidate B sits farther from the anchor on |Δy| but the label
    matches its neighborhood. y-band wins — nearest_label CANNOT veto."""
    anchor = _block("p1-b000", "DEMOGRAPHICS:", x=0.0, y=100.0)
    # Candidate A: closer to anchor (|Δy|=50), no label nearby.
    cand_a = _block("p1-b010", "06/08/1971", x=0.0, y=150.0)
    # Candidate B: farther (|Δy|=400) but "DOB" sits beside it.
    cand_b = _block("p1-b020", "06/08/1971", x=0.0, y=500.0)
    label_near_b = _block("p1-b021", "DOB", x=0.0, y=500.0, w=30.0)

    blocks = [anchor, cand_a, cand_b, label_near_b]
    candidates = [(cand_a, 8), (cand_b, 8)]

    chosen, _anchor, dy, outcome, _score, used = _select_best_candidate(
        candidates,
        anchors=[anchor],
        field_name="dob",
        blocks=blocks,
        nearest_label="DOB",
    )

    assert chosen.bbox_id == "p1-b010", "y-band must remain primary"
    # Tiebreaker label flag must not fire when y-band actually decided.
    assert used is False
    assert outcome == "repointed_with_anchor"
    assert dy < 100.0


def test_label_tiebreak_no_match_when_label_misses_both() -> None:
    """Equal y-band, but the supplied label appears in neither
    neighborhood — legacy outcome (no tiebreak fired)."""
    anchor = _block("p1-b000", "DEMOGRAPHICS:", x=0.0, y=100.0)
    cand_a = _block("p1-b010", "06/08/1971", x=0.0, y=200.0)
    cand_b = _block("p1-b020", "06/08/1971", x=400.0, y=200.0)

    chosen, _a, _dy, outcome, score, used = _select_best_candidate(
        [(cand_a, 8), (cand_b, 8)],
        anchors=[anchor],
        field_name="dob",
        blocks=[anchor, cand_a, cand_b],
        nearest_label="ZZZ_NOT_PRESENT",
    )

    assert score == 0.0
    assert used is False
    assert outcome == "repointed_with_anchor"
    assert chosen.bbox_id == "p1-b010"


# --------------------------------------------------------------------------- #
# Helper-level tests for _label_match_score
# --------------------------------------------------------------------------- #


def test_label_match_score_self_text_hit() -> None:
    block = _block("p1-b001", "DOB: 06/08/1971", x=0.0, y=0.0, w=200.0)
    assert _label_match_score(block, [block], "DOB") == 1.0


def test_label_match_score_neighborhood_hit_within_window() -> None:
    cand = _block("p1-b001", "06/08/1971", x=0.0, y=0.0, w=80.0)
    near = _block("p1-b002", "Date of Birth", x=10.0, y=10.0, w=60.0)
    assert _label_match_score(cand, [cand, near], "Date of Birth") == 1.0


def test_label_match_score_outside_window_misses() -> None:
    cand = _block("p1-b001", "06/08/1971", x=0.0, y=0.0, w=80.0)
    far = _block("p1-b002", "DOB", x=500.0, y=500.0, w=30.0)
    assert _label_match_score(cand, [cand, far], "DOB") == 0.0


def test_label_match_score_other_page_misses() -> None:
    cand = _block("p1-b001", "06/08/1971", page=1, x=0.0, y=0.0, w=80.0)
    other = _block("p2-b001", "DOB", page=2, x=0.0, y=0.0, w=30.0)
    assert _label_match_score(cand, [cand, other], "DOB") == 0.0


def test_label_match_score_empty_label_returns_zero() -> None:
    cand = _block("p1-b001", "06/08/1971")
    assert _label_match_score(cand, [cand], None) == 0.0
    assert _label_match_score(cand, [cand], "") == 0.0
    assert _label_match_score(cand, [cand], "A") == 0.0  # too short


# --------------------------------------------------------------------------- #
# Log emission: structured ``extra`` carries nearest_label_score / used.
# --------------------------------------------------------------------------- #


def test_repoint_log_carries_nearest_label_fields(caplog) -> None:
    """The structured log event emitted by ``_repoint_citation`` must
    include ``nearest_label_score`` and ``nearest_label_used`` so
    downstream observability can audit tiebreaker activations."""
    import logging

    from extractors.intake import _repoint_citation
    from extractors.schemas import Citation

    anchor = _block("p1-b000", "DEMOGRAPHICS:", x=0.0, y=100.0)
    cand_a = _block("p1-b010", "06/08/1971", x=0.0, y=200.0)
    cand_b = _block("p1-b020", "06/08/1971", x=400.0, y=200.0)
    label_near_a = _block("p1-b011", "DOB", x=0.0, y=205.0, w=30.0)
    blocks = [anchor, cand_a, cand_b, label_near_a]
    block_index = {b.bbox_id: b for b in blocks}

    # Cite the section header so the "kept" early-return doesn't fire and we
    # exercise the multi-candidate repoint path.
    cit = Citation(
        source_type="document",
        source_id="doc-x",
        field_or_chunk_id="p1-b000",
        quote_or_value="DEMOGRAPHICS:",
        nearest_label="DOB",
    )

    with caplog.at_level(logging.INFO, logger="extractors.intake"):
        new_cit = _repoint_citation(
            cit, "06/08/1971", blocks, block_index,
            anchors=[anchor], field_name="dob",
        )

    assert new_cit.field_or_chunk_id == "p1-b010"
    repoint_records = [r for r in caplog.records
                       if r.message == "extractor_citation_repointed"]
    assert repoint_records, "expected an extractor_citation_repointed log record"
    rec = repoint_records[-1]
    assert getattr(rec, "nearest_label_used", None) is True
    assert getattr(rec, "nearest_label_score", None) == 1.0
    assert getattr(rec, "outcome", None) == "repointed_with_anchor_label_tiebreak"
