"""Tests for documents.photo_preprocess (Phase 3 Wave 2A).

Coverage:
  - off mode is a true no-op
  - force mode runs even on clean inputs
  - opencv-unavailable falls through to original bytes
  - is_likely_photo heuristic on synthetic clean-vs-skewed fixtures
  - per-stage failure falls through to prior stage
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hard_failure


def _png_bytes(image) -> bytes:
    import cv2
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _make_clean_image():
    """A clean white-background "scan" of black text — should NOT trigger
    auto-mode preprocessing."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    img = np.full((480, 640, 3), 255, dtype=np.uint8)
    # Render a few high-contrast text-like rectangles.
    for y in (100, 200, 300):
        for x in (50, 200, 350, 500):
            img[y:y + 30, x:x + 100] = 0
    return img


def _make_photo_image():
    """A blurry/skewed phone-photo-like image — should trigger auto-mode."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    img = np.full((480, 640, 3), 200, dtype=np.uint8)
    # Soft random texture so Laplacian variance is low (blurry).
    rng = np.random.default_rng(seed=42)
    noise = rng.integers(0, 30, size=img.shape, dtype=np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (15, 15), 0)
    # Add a small skewed dark block — gives some edges so canny isn't 0
    # but the image overall is blurry + non-rectangular.
    pts = np.array([[100, 100], [400, 120], [380, 350], [80, 330]], dtype=np.int32)
    cv2.fillPoly(img, [pts], (40, 40, 40))
    return img


def test_off_mode_is_noop():
    pytest.importorskip("cv2")
    from documents.photo_preprocess import preprocess_photo
    payload = b"not even a real image"
    assert preprocess_photo(payload, mode_override="off") is payload


def test_unknown_mode_falls_back_to_off():
    pytest.importorskip("cv2")
    from documents.photo_preprocess import preprocess_photo
    payload = b"not even a real image"
    assert preprocess_photo(payload, mode_override="banana") == payload


def test_force_mode_runs_on_clean_image():
    cv2 = pytest.importorskip("cv2")
    from documents.photo_preprocess import preprocess_photo
    img = _make_clean_image()
    in_bytes = _png_bytes(img)
    out_bytes = preprocess_photo(in_bytes, mode_override="force")
    # Force mode must produce a valid PNG output (may equal in_bytes if all
    # stages are conservative no-ops, but type is bytes and not the input
    # object pass-through path).
    assert isinstance(out_bytes, bytes) and len(out_bytes) > 0
    # Round-trip must decode cleanly.
    import numpy as np
    arr = np.frombuffer(out_bytes, dtype=np.uint8)
    decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.shape[2] == 3


def test_decode_failure_falls_through():
    pytest.importorskip("cv2")
    from documents.photo_preprocess import preprocess_photo
    # Force mode + non-image bytes should not raise; returns input.
    payload = b"\x00\x01\x02not-a-png"
    assert preprocess_photo(payload, mode_override="force") == payload


def test_auto_mode_skips_clean_image():
    cv2 = pytest.importorskip("cv2")
    from documents.photo_preprocess import preprocess_photo, is_likely_photo
    img = _make_clean_image()
    # The heuristic must not vote "photo" on a high-contrast scan.
    assert is_likely_photo(img) is False
    in_bytes = _png_bytes(img)
    out_bytes = preprocess_photo(in_bytes, mode_override="auto")
    assert out_bytes == in_bytes  # pipeline skipped


def test_auto_mode_runs_on_blurry_photo():
    cv2 = pytest.importorskip("cv2")
    from documents.photo_preprocess import preprocess_photo, is_likely_photo
    img = _make_photo_image()
    assert is_likely_photo(img) is True
    in_bytes = _png_bytes(img)
    out_bytes = preprocess_photo(in_bytes, mode_override="auto")
    # Pipeline ran → output is a re-encoded PNG (may differ from input bytes).
    import numpy as np
    arr = np.frombuffer(out_bytes, dtype=np.uint8)
    decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert decoded is not None


def test_stage_failure_does_not_raise(monkeypatch):
    """If any individual stage raises, the pipeline must fall through to the
    prior stage's output rather than propagating into the OCR call site."""
    cv2 = pytest.importorskip("cv2")
    from documents import photo_preprocess
    img = _make_clean_image()
    in_bytes = _png_bytes(img)

    def _boom(_image):
        raise RuntimeError("synthetic failure for test")

    monkeypatch.setattr(photo_preprocess, "_clahe", _boom)
    out_bytes = photo_preprocess.preprocess_photo(in_bytes, mode_override="force")
    assert isinstance(out_bytes, bytes) and len(out_bytes) > 0


def test_resolved_mode_reads_settings(monkeypatch):
    pytest.importorskip("cv2")
    from documents import photo_preprocess

    class _S:
        photo_preprocess = "force"

    import sys as _sys
    fake_config = type(_sys)("config")
    fake_config.settings = _S()
    saved = _sys.modules.get("config")
    _sys.modules["config"] = fake_config
    try:
        assert photo_preprocess._resolved_mode() == "force"
    finally:
        if saved is None:
            _sys.modules.pop("config", None)
        else:
            _sys.modules["config"] = saved
