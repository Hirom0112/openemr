"""Tests for the Wave 2C synthetic eval corpus + bbox-GT rubrics.

Covers:
  - Every generated fixture has a ``<name>.gt.json`` sidecar.
  - Sidecar GT bboxes are non-degenerate (positive width + height; non-empty value).
  - ``citation_iou`` correctly accepts overlapping bboxes (IoU >= 0.5) and
    rejects shifted ones.
  - ``citation_pixel_distance`` returns plausible centroid distances and
    is None on missing input.

These tests are isolated — no Docker, no network. They re-invoke the
generator at module-collect time (deterministic, cheap) so a freshly
checked-out repo is enough to run them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.rubrics_mechanical import citation_iou, citation_pixel_distance
from tests.fixtures.eval._generate_synthetic_v2 import (
    OUT_DIR,
    all_fixture_keys,
    generate_all_v2,
)


pytestmark = pytest.mark.hard_failure


@pytest.fixture(scope="module")
def corpus() -> dict[str, Path]:
    return generate_all_v2()


# --------------------------------------------------------------------------- #
# Corpus completeness
# --------------------------------------------------------------------------- #


def test_corpus_emits_36_fixtures(corpus: dict[str, Path]) -> None:
    keys = sorted(corpus.keys())
    expected = sorted(all_fixture_keys())
    assert keys == expected, (
        f"corpus keys drifted; got {len(keys)} expected {len(expected)}"
    )
    assert len(keys) == 36


def test_every_fixture_has_a_sidecar(corpus: dict[str, Path]) -> None:
    missing: list[str] = []
    for key, path in corpus.items():
        sidecar = path.with_suffix(path.suffix + ".gt.json")
        if not sidecar.exists():
            missing.append(f"{key}: no sidecar at {sidecar}")
    assert not missing, "\n".join(missing)


def test_sidecar_bboxes_are_non_degenerate(corpus: dict[str, Path]) -> None:
    bad: list[str] = []
    for key, path in corpus.items():
        sidecar = path.with_suffix(path.suffix + ".gt.json")
        gt = json.loads(sidecar.read_text())
        if not gt.get("fields"):
            bad.append(f"{key}: zero fields in sidecar")
            continue
        for f in gt["fields"]:
            bbox = f["bbox"]
            if not f["value"]:
                bad.append(f"{key}/{f['name']}: empty value")
            if bbox["w"] <= 0 or bbox["h"] <= 0:
                bad.append(f"{key}/{f['name']}: degenerate bbox {bbox}")
        # Photo fixtures must include a warp matrix.
        if gt["modality"] == "photo_capture":
            warp = gt.get("warp")
            assert warp is not None, f"{key}: photo_capture missing warp meta"
            assert "matrix" in warp and "inverse" in warp, (
                f"{key}: warp meta incomplete"
            )
    assert not bad, "\n".join(bad)


def test_corpus_is_deterministic() -> None:
    """Re-running the generator must produce identical bytes."""
    import hashlib

    first: dict[str, str] = {}
    paths = generate_all_v2()
    for k, p in paths.items():
        first[k] = hashlib.sha256(p.read_bytes()).hexdigest()
    paths2 = generate_all_v2()
    for k, p in paths2.items():
        h2 = hashlib.sha256(p.read_bytes()).hexdigest()
        assert first[k] == h2, f"{k}: non-deterministic output"


def test_corpus_lives_in_synthetic_v2_directory(corpus: dict[str, Path]) -> None:
    for key, path in corpus.items():
        assert path.parent == OUT_DIR, f"{key}: lives outside synthetic_v2/"


# --------------------------------------------------------------------------- #
# citation_iou rubric
# --------------------------------------------------------------------------- #


def test_citation_iou_accepts_identical_bboxes() -> None:
    bbox = {"x": 100, "y": 200, "w": 80, "h": 20}
    assert citation_iou(bbox, bbox) is True


def test_citation_iou_accepts_high_overlap() -> None:
    gt = {"x": 100, "y": 200, "w": 100, "h": 20}
    # 90% overlap on x (10px shift), full overlap on y -> IoU ≈ 0.81
    extracted = {"x": 110, "y": 200, "w": 100, "h": 20}
    assert citation_iou(extracted, gt) is True


def test_citation_iou_rejects_shifted_bboxes() -> None:
    gt = {"x": 100, "y": 200, "w": 80, "h": 20}
    # Shifted entirely outside — zero overlap.
    far = {"x": 500, "y": 600, "w": 80, "h": 20}
    assert citation_iou(far, gt) is False


def test_citation_iou_rejects_just_below_threshold() -> None:
    # Construct two equal-area boxes whose IoU is just under 0.5.
    # Box A: (0, 0, 100, 100). Box B: (60, 0, 100, 100).
    # Intersection = 40 * 100 = 4000. Union = 100*100 + 100*100 - 4000 = 16000.
    # IoU = 4000/16000 = 0.25 (< 0.5).
    gt = {"x": 0, "y": 0, "w": 100, "h": 100}
    extracted = {"x": 60, "y": 0, "w": 100, "h": 100}
    assert citation_iou(extracted, gt) is False


def test_citation_iou_rejects_missing_or_degenerate() -> None:
    bbox = {"x": 0, "y": 0, "w": 10, "h": 10}
    assert citation_iou(None, bbox) is False
    assert citation_iou(bbox, None) is False
    assert citation_iou({"x": 0, "y": 0, "w": 0, "h": 10}, bbox) is False
    # Malformed input — fail-loud, no crash.
    assert citation_iou("nope", bbox) is False


def test_citation_iou_accepts_list_form() -> None:
    # Both forms should be accepted ([x,y,w,h] and {x,y,w,h}).
    bbox_list = [10, 20, 50, 30]
    bbox_dict = {"x": 10, "y": 20, "w": 50, "h": 30}
    assert citation_iou(bbox_list, bbox_dict) is True


# --------------------------------------------------------------------------- #
# citation_pixel_distance rubric
# --------------------------------------------------------------------------- #


def test_citation_pixel_distance_zero_for_identical() -> None:
    bbox = {"x": 0, "y": 0, "w": 10, "h": 10}
    assert citation_pixel_distance(bbox, bbox) == 0.0


def test_citation_pixel_distance_returns_centroid_euclidean() -> None:
    # Centroids: (5, 5) vs (8, 9). Distance = sqrt(9 + 16) = 5.0.
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    b = {"x": 3, "y": 4, "w": 10, "h": 10}
    d = citation_pixel_distance(a, b)
    assert d is not None
    assert abs(d - 5.0) < 1e-9


def test_citation_pixel_distance_none_on_missing_input() -> None:
    bbox = {"x": 0, "y": 0, "w": 10, "h": 10}
    assert citation_pixel_distance(None, bbox) is None
    assert citation_pixel_distance(bbox, None) is None
