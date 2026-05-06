"""Pre-OCR photo preprocessing pipeline (Phase 3 Wave 2A).

Cleans up cell-phone-photo intake images before they hit the OCR engine:
deskew → perspective unwarp → CLAHE on luminance → bilateral denoise.

Gated by ``settings.photo_preprocess`` ∈ {off, auto, force}:
  off    — no-op, returns input bytes verbatim (default)
  auto   — runs the heuristic ``is_likely_photo`` and only preprocesses
           when it votes yes
  force  — always preprocesses

Runs only on raster image input (PNG/JPEG); the caller in
``documents.ocr_engine.dispatch_extract_image`` routes PDFs around it.

Each step is wrapped so a failure returns the previous-stage image — the
pipeline never raises into the OCR call site.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Optional

from agent.metrics import (
    agent_photo_preprocess_duration_seconds,
    agent_photo_preprocess_runs_total,
)
from observability.tool_logging import log_tool_outcome

logger = logging.getLogger(__name__)


def preprocess_photo(image_bytes: bytes, *, mode_override: Optional[str] = None) -> bytes:
    """Entry point. Returns possibly-modified PNG bytes or the original input.

    ``mode_override`` is for tests; production reads ``settings.photo_preprocess``.
    """
    mode = (mode_override or _resolved_mode()).lower()

    if mode not in {"off", "auto", "force"}:
        # Unknown value — log once and treat as off so a typo'd env var
        # never breaks document ingest.
        logger.warning(
            "photo_preprocess.unknown_mode_falling_back",
            extra={"requested": mode},
        )
        mode = "off"

    if mode == "off":
        agent_photo_preprocess_runs_total.labels(outcome="skipped").inc()
        log_tool_outcome(
            tool_name="photo_preprocess",
            duration_ms=0,
            cache="n/a",
            extra={"outcome": "skipped", "reason": "mode_off"},
        )
        return image_bytes

    started = time.perf_counter()

    # Lazy imports — opencv + numpy are heavyweight and only needed when the
    # flag is on. Keeping them inside the function lets the rest of the
    # documents package stay importable when opencv-python-headless is not
    # installed (e.g. lightweight test environments).
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        agent_photo_preprocess_runs_total.labels(outcome="errored").inc()
        log_tool_outcome(
            tool_name="photo_preprocess",
            duration_ms=int((time.perf_counter() - started) * 1000),
            cache="n/a",
            extra={"outcome": "errored", "reason": "opencv_unavailable"},
        )
        return image_bytes

    # Decode → ndarray (BGR). Fall back to original bytes on decode failure.
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        image = None
    if image is None:
        agent_photo_preprocess_runs_total.labels(outcome="errored").inc()
        log_tool_outcome(
            tool_name="photo_preprocess",
            duration_ms=int((time.perf_counter() - started) * 1000),
            cache="n/a",
            extra={"outcome": "errored", "reason": "decode_failed"},
        )
        return image_bytes

    if mode == "auto" and not is_likely_photo(image):
        duration_ms = int((time.perf_counter() - started) * 1000)
        agent_photo_preprocess_runs_total.labels(outcome="skipped").inc()
        log_tool_outcome(
            tool_name="photo_preprocess",
            duration_ms=duration_ms,
            cache="n/a",
            extra={"outcome": "skipped", "reason": "auto_heuristic_voted_no"},
        )
        return image_bytes

    # Pipeline. Each stage falls through to the prior stage's output on error.
    # Use explicit ``is not None`` rather than ``or`` because the values are
    # ndarrays — Python's truthiness on ndarray raises ValueError.
    stage = image
    for fn, label in (
        (_deskew, "deskew"),
        (_perspective_unwarp, "perspective_unwarp"),
        (_clahe, "clahe"),
        (_bilateral_denoise, "bilateral_denoise"),
    ):
        result = _safe(fn, stage, label)
        if result is not None:
            stage = result

    # Re-encode to PNG so downstream OCR sees a stable container.
    try:
        ok, encoded = cv2.imencode(".png", stage)
        if not ok:
            raise RuntimeError("cv2.imencode returned False")
        out_bytes = encoded.tobytes()
    except Exception:
        agent_photo_preprocess_runs_total.labels(outcome="errored").inc()
        log_tool_outcome(
            tool_name="photo_preprocess",
            duration_ms=int((time.perf_counter() - started) * 1000),
            cache="n/a",
            extra={"outcome": "errored", "reason": "encode_failed"},
        )
        return image_bytes

    duration_s = time.perf_counter() - started
    agent_photo_preprocess_runs_total.labels(outcome="ran").inc()
    agent_photo_preprocess_duration_seconds.observe(duration_s)
    log_tool_outcome(
        tool_name="photo_preprocess",
        duration_ms=int(duration_s * 1000),
        cache="n/a",
        extra={"outcome": "ran", "mode": mode, "n_bytes_in": len(image_bytes), "n_bytes_out": len(out_bytes)},
    )
    return out_bytes


def is_likely_photo(image) -> bool:
    """Heuristic for ``auto`` mode. Returns True only when the image looks
    *unlike* a clean scan. We compare against three discriminators:

      1. Laplacian variance < 500 → blurry/soft (photos)
      2. Pixel-intensity histogram is NOT bimodal — scans are dominated by
         near-white background + near-black ink; a high background-mass ratio
         (>60% of pixels in [220, 255]) plus low mid-tone mass (<5% in
         [80, 200]) signals a scan and we vote NO.
      3. Edge density < 0.03 — sparse edges (photos); scanned text is dense.

    The image must satisfy (1) AND not be detected as a scan via (2) AND (3)
    must vote yes. ALL three must fire — false positives here cost OCR
    quality on scans, so we err on the side of skipping.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return False

    h, w = image.shape[:2]
    if h == 0 or w == 0:
        return False

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    blur_vote = laplacian_var < 500.0

    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    total = float(hist.sum()) or 1.0
    background_mass = float(hist[220:256].sum()) / total
    midtone_mass = float(hist[80:200].sum()) / total
    looks_like_scan = background_mass > 0.60 and midtone_mass < 0.05

    edges = cv2.Canny(gray, 50, 150)
    edge_density = float(np.count_nonzero(edges)) / (h * w)
    sparse_edge_vote = edge_density < 0.03

    return bool(blur_vote and (not looks_like_scan) and sparse_edge_vote)


def _resolved_mode() -> str:
    try:
        from config import settings
        return getattr(settings, "photo_preprocess", "off") or "off"
    except Exception:
        return "off"


def _safe(fn, stage, label: str):
    """Run ``fn(stage)``; on exception log and return None so the caller
    falls back to ``stage``."""
    try:
        return fn(stage)
    except Exception:
        logger.warning(
            "photo_preprocess.stage_failed",
            extra={"stage": label},
            exc_info=True,
        )
        return None


def _deskew(image):
    """Rotate so the dominant text-line angle is horizontal. Uses minAreaRect
    on a thresholded copy. Caps correction at ±20° to avoid pathological
    rotations on already-aligned scans."""
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _thr_v, thr = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    coords = np.column_stack(np.where(thr > 0))
    if coords.size == 0:
        return image
    angle = cv2.minAreaRect(coords)[-1]
    # cv2 returns [-90, 0); normalize to a rotation about horizontal.
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.5 or abs(angle) > 20:
        return image
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(
        image, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _perspective_unwarp(image):
    """If a clean 4-corner contour dominates the frame (>30% area), unwarp it
    to a rectangle. Returns the original image if no clean quad is found —
    this stage is conservative because a wrong unwarp is worse than none."""
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 75, 200)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return image
    h, w = image.shape[:2]
    img_area = float(h * w)
    best = None
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.contourArea(approx) > 0.30 * img_area:
            best = approx.reshape(4, 2).astype("float32")
            break
    if best is None:
        return image
    # Order points: TL, TR, BR, BL.
    s = best.sum(axis=1)
    diff = np.diff(best, axis=1).ravel()
    rect = np.zeros((4, 2), dtype="float32")
    rect[0] = best[np.argmin(s)]
    rect[2] = best[np.argmax(s)]
    rect[1] = best[np.argmin(diff)]
    rect[3] = best[np.argmax(diff)]
    (tl, tr, br, bl) = rect
    width = max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))
    height = max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))
    if width < 50 or height < 50:
        return image
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype="float32",
    )
    M = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(image, M, (int(width), int(height)))


def _clahe(image):
    """Contrast Limited Adaptive Histogram Equalization on the luminance
    channel of a LAB conversion — preserves color but lifts text contrast."""
    import cv2  # type: ignore

    if image.ndim != 3:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return clahe.apply(image)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_channel = clahe.apply(l_channel)
    merged = cv2.merge((l_channel, a_channel, b_channel))
    return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)


def _bilateral_denoise(image):
    """Edge-preserving denoise. Smaller diameter than the OpenCV default to
    keep text strokes sharp."""
    import cv2  # type: ignore

    return cv2.bilateralFilter(image, d=5, sigmaColor=50, sigmaSpace=50)
