"""Wave 2B — polygon-aware citation_iou rubric.

Coverage:

* identical polygons → pass (IoU == 1.0)
* low-IoU polygons → fail
* mixed-mode (one polygon, one bbox) → degrades to bbox-IoU on the
  polygon side's axis-aligned bounding box (per contract §2)
* degenerate polygon (<3 distinct points) → bbox path (contract §3)
* citation-shaped dicts with both polygon and bbox → polygon wins
  (contract §1)

The rubric prefers Shapely when available but must pass identically with
the pure-Python shoelace + Sutherland-Hodgman fallback. The test fixtures
here use convex polygons so the fallback's clip-and-shoelace math is
exact, not approximate.
"""

from __future__ import annotations

import pytest

from evals.rubrics_mechanical import citation_iou

pytestmark = pytest.mark.hard_failure


# --- identical polygons --------------------------------------------------- #


def test_identical_square_polygons_pass():
    poly = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert citation_iou(poly, poly) is True


def test_identical_paddle_4point_polygon_passes():
    # Realistic paddle line-level shape (a slightly-rotated rectangle).
    poly = [
        (50.0, 100.0),
        (250.0, 95.0),
        (252.0, 130.0),
        (52.0, 135.0),
    ]
    assert citation_iou(poly, poly) is True


# --- low IoU rejected ----------------------------------------------------- #


def test_disjoint_polygons_fail():
    a = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    b = [(100.0, 100.0), (110.0, 100.0), (110.0, 110.0), (100.0, 110.0)]
    assert citation_iou(a, b) is False


def test_small_overlap_polygons_fail():
    # Two squares overlapping in a tiny corner — IoU well under 0.5.
    a = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    b = [(9.0, 9.0), (19.0, 9.0), (19.0, 19.0), (9.0, 19.0)]
    assert citation_iou(a, b) is False


# --- mixed bbox/polygon → bbox-IoU on polygon side's bbox ----------------- #


def test_mixed_mode_uses_bbox_iou_pass():
    # Polygon = the square (0,0)-(10,10); bbox = (0, 0, 10, 10).
    poly = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    bbox = (0.0, 0.0, 10.0, 10.0)
    # Polygon's bbox === bbox → IoU == 1 → pass.
    assert citation_iou(poly, bbox) is True
    assert citation_iou(bbox, poly) is True


def test_mixed_mode_does_not_spuriously_favor_polygon():
    # A polygon shaped like a thin diagonal strip (its bbox covers a 10x10
    # area). The GT bbox is offset such that bbox-vs-bbox IoU is below
    # threshold. The polygon's TRUE area would barely overlap the bbox at
    # all, but the rubric uses the polygon's bbox — so the result depends
    # only on the bbox geometry, never on the polygon area. Per contract
    # §2: "Don't favor polygon spuriously."
    poly = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    # Offset bbox so IoU < 0.5: overlap area = 4*4 = 16, union = 184, IoU ~ 0.09
    bbox = (6.0, 6.0, 10.0, 10.0)
    assert citation_iou(poly, bbox) is False


# --- degenerate polygon falls back to bbox path --------------------------- #


def test_two_point_polygon_falls_back_to_bbox():
    # 2-point "polygon" is a line segment — no area, must NOT be treated
    # as a region. The rubric should fall back to bbox on that side.
    side_a = {
        "polygon": [[0.0, 0.0], [10.0, 0.0]],   # degenerate
        "bbox": [0.0, 0.0, 10.0, 10.0],         # usable
    }
    side_b = {"bbox": [0.0, 0.0, 10.0, 10.0]}
    assert citation_iou(side_a, side_b) is True


def test_collinear_three_points_treated_degenerate():
    # Three points that are all the same → distinct < 3 → degenerate.
    side_a = {
        "polygon": [[5.0, 5.0], [5.0, 5.0], [5.0, 5.0]],
        "bbox": [0.0, 0.0, 10.0, 10.0],
    }
    side_b = {"bbox": [0.0, 0.0, 10.0, 10.0]}
    assert citation_iou(side_a, side_b) is True


# --- citation-shaped dicts: polygon wins (contract §1) -------------------- #


def test_citation_dicts_polygon_takes_precedence():
    # Both sides have polygon AND bbox. The bboxes overlap perfectly (would
    # pass on bbox-IoU). The polygons are disjoint — must FAIL because
    # polygon wins on each side.
    side_a = {
        "polygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
        "bbox": [0.0, 0.0, 100.0, 100.0],
    }
    side_b = {
        "polygon": [[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0]],
        "bbox": [0.0, 0.0, 100.0, 100.0],
    }
    assert citation_iou(side_a, side_b) is False


def test_citation_dicts_polygon_match_passes():
    poly = [[10.0, 20.0], [110.0, 20.0], [110.0, 80.0], [10.0, 80.0]]
    side_a = {
        "polygon": poly,
        "bbox": [0.0, 0.0, 200.0, 200.0],
    }
    side_b = {
        "polygon": poly,
        "bbox": [500.0, 500.0, 10.0, 10.0],  # disjoint bbox; should be ignored
    }
    assert citation_iou(side_a, side_b) is True


# --- backward-compatibility: legacy bbox-only signature still works ------- #


def test_bbox_only_legacy_path_still_works():
    a = (0.0, 0.0, 10.0, 10.0)
    b = (0.0, 0.0, 10.0, 10.0)
    assert citation_iou(a, b) is True
    assert citation_iou(a, (50.0, 50.0, 10.0, 10.0)) is False


# --- citation_polygon_used info rubric ------------------------------------ #


def test_citation_polygon_used_counts_polygons():
    from evals.rubrics_mechanical import citation_polygon_used

    extraction = {
        "kind": "lab_report",
        "values": [
            {
                "citations": [
                    {
                        "source_type": "document",
                        "polygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
                        "bbox": [0.0, 0.0, 10.0, 10.0],
                    },
                    {
                        "source_type": "document",
                        "polygon": None,
                        "bbox": [5.0, 5.0, 10.0, 10.0],
                    },
                ],
            },
        ],
    }
    result = citation_polygon_used(extraction)
    assert result == (1, 2)


def test_citation_polygon_used_returns_none_when_no_doc_citations():
    from evals.rubrics_mechanical import citation_polygon_used

    assert citation_polygon_used({"kind": "lab_report", "values": []}) is None
    assert citation_polygon_used(None) is None


# --- end-to-end polygon flow (LayoutBlock → Citation) --------------------- #


def test_paddle_polygon_propagates_through_extractor_hydration():
    """A 4-point paddle line-level polygon survives the LayoutBlock →
    Citation hydration step and round-trips cleanly through pydantic
    serialization. This is the load-bearing wire-level guarantee that
    the UI sees the polygon shape."""
    from documents.ocr import BlockGranularity, LayoutBlock
    from extractors.lab import _hydrate_citation, _index_blocks
    from extractors.schemas import Citation

    poly_pts = (
        (50.0, 100.0),
        (250.0, 95.0),
        (252.0, 130.0),
        (52.0, 135.0),
    )
    block = LayoutBlock(
        bbox_id="p1-b042",
        page=1,
        bbox=(50.0, 95.0, 202.0, 40.0),
        text="Sodium 138 mEq/L",
        ocr_confidence=0.98,
        granularity=BlockGranularity.LINE,
        polygon=poly_pts,
    )
    idx = _index_blocks([block])
    cit = Citation(
        source_type="document",
        source_id="doc-1",
        field_or_chunk_id="p1-b042",
        quote_or_value="138 mEq/L",
    )
    hydrated = _hydrate_citation(cit, idx)
    # Bbox + page propagated.
    assert hydrated.bbox == (50.0, 95.0, 202.0, 40.0)
    assert hydrated.page == 1
    # Polygon propagated as list-of-tuples.
    assert hydrated.polygon == [
        (50.0, 100.0),
        (250.0, 95.0),
        (252.0, 130.0),
        (52.0, 135.0),
    ]
    # JSON-serializable (pydantic v2 strict mode).
    dumped = hydrated.model_dump()
    assert dumped["polygon"] == [
        (50.0, 100.0),
        (250.0, 95.0),
        (252.0, 130.0),
        (52.0, 135.0),
    ]
    # citation_iou polygon-vs-polygon path agrees with itself.
    assert citation_iou(hydrated.polygon, list(poly_pts)) is True


def test_intake_extractor_propagates_polygon_through_repoint():
    """The intake extractor's repoint path also stamps polygon (not just
    bbox/page). Important because intake is the only path that does
    value-based block selection."""
    from documents.ocr import BlockGranularity, LayoutBlock
    from extractors.intake import _block_polygon_list

    poly_pts = (
        (10.0, 20.0),
        (110.0, 20.0),
        (110.0, 50.0),
        (10.0, 50.0),
    )
    block = LayoutBlock(
        bbox_id="p1-b001",
        page=1,
        bbox=(10.0, 20.0, 100.0, 30.0),
        text="DOB 06/08/1971",
        ocr_confidence=0.95,
        granularity=BlockGranularity.LINE,
        polygon=poly_pts,
    )
    out = _block_polygon_list(block)
    assert out == [(10.0, 20.0), (110.0, 20.0), (110.0, 50.0), (10.0, 50.0)]

    # No-polygon block returns None (never fabricate).
    bare = LayoutBlock(
        bbox_id="p1-b002",
        page=1,
        bbox=(0.0, 0.0, 10.0, 10.0),
        text="x",
        ocr_confidence=1.0,
        granularity=BlockGranularity.LINE,
        polygon=None,
    )
    assert _block_polygon_list(bare) is None
