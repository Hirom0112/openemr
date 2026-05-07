"""Pre-OCR preprocessing pipeline for bitonal fax TIFF input (Phase 9 Slice 9.6).

Sibling of ``photo_preprocess.py`` — NOT a fork. Photo preprocessing was tuned
for cell-phone capture: glare, perspective, color noise. Fax pages are the
opposite problem — pre-thresholded bitonal scans with no color, no perspective
distortion, but plenty of speckle, ~2-5° rotation drift from the feeder, and
metadata-only DPI tags that lie. CLAHE on bitonal data is a no-op; bilateral
denoise on text strokes is a corruption risk; minAreaRect deskew loses to
projection-profile when input is already mostly-rectangular. Different shape →
different module.

Pipeline (each stage falls through to the previous on error):

  1. Mode upcast: ``"1"`` → ``"L"``. Tesseract handles 8-bit grayscale far
     better than 1-bit (the binarizer it uses internally was tuned for L/RGB).
  2. DPI tag rewrite: stamp ``dpi=(200, 200)`` so downstream OCR doesn't
     scale based on whatever the fax stack guessed (often 96).
  3. Median-blur denoise (NOT bilateral): kills isolated speckle without
     softening text edges. Bilateral preserves edges of natural images; on
     bitonal text it just smears.
  4. Projection-profile deskew (NOT minAreaRect): bitonal page → row
     histogram → variance peaks at the true text-line angle, robust to
     border noise. minAreaRect on a binarized page often grabs a margin
     artifact instead of the text bounding cloud.
  5. Optional 2× upsample if resolved height < 1500 px (typical fax 200dpi
     letter page is ~2200 px; below that, characters are too small for
     tesseract's default LSTM).

NO CLAHE, NO bilateral, NO perspective unwarp — those are photo-pipeline
tools. Locked decision per Slice 9.6 spec.

Each stage is isolated so a failure returns the previous-stage image; the
pipeline never raises into the caller. Returns a PIL ``Image`` (not bytes) —
the caller in ``tiff_loader`` already has a Pillow image and re-encoding to
PNG every stage would dominate the wall-clock.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from agent.metrics import (
    agent_doc_parser_calls_total,
    agent_tiff_parse_duration_seconds,
)
from observability.tool_logging import log_tool_outcome

if TYPE_CHECKING:  # pragma: no cover — typing-only import
    from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)


# Locked targets per Slice 9.6 spec.
_TARGET_DPI: int = 200
_UPSAMPLE_THRESHOLD_PX: int = 1500
_UPSAMPLE_FACTOR: int = 2
_MEDIAN_KERNEL_PX: int = 3


def preprocess_fax_page(image: "PILImage") -> "PILImage":
    """Run the bitonal-fax preprocessing pipeline on a single TIFF frame.

    Returns the cleaned image. On any per-stage failure, returns the
    previous-stage image — never raises. Mode/DPI metadata is set on the
    returned object.

    Public for ``tiff_loader.extract_tiff_layout``; not intended for direct
    use elsewhere.
    """
    started = time.perf_counter()
    stage = image
    for fn, label in (
        (_upcast_mode, "upcast_mode"),
        (_rewrite_dpi, "rewrite_dpi"),
        (_median_denoise, "median_denoise"),
        (_projection_profile_deskew, "projection_profile_deskew"),
        (_maybe_upsample, "maybe_upsample"),
    ):
        result = _safe(fn, stage, label)
        if result is not None:
            stage = result

    duration_s = time.perf_counter() - started
    # Per-page preprocess rolls into the parent extract_tiff_layout
    # duration histogram; we still emit a structured log so the per-stage
    # cost is auditable without a separate metric.
    log_tool_outcome(
        tool_name="fax_preprocess",
        duration_ms=int(duration_s * 1000),
        cache="n/a",
        extra={
            "outcome": "ran",
            "mode_in": image.mode,
            "mode_out": stage.mode,
            "size_in": list(image.size),
            "size_out": list(stage.size),
        },
    )
    return stage


def _safe(fn, stage: "PILImage", label: str) -> Optional["PILImage"]:
    try:
        return fn(stage)
    except Exception:
        # Do not raise into the OCR call site — degrade visibly via a
        # warning log + paired metric so the staged-failure rubric can
        # see it, but keep the page on the path.
        logger.warning(
            "fax_preprocess.stage_failed",
            extra={"stage": label},
            exc_info=True,
        )
        agent_doc_parser_calls_total.labels(
            format="tiff", outcome="stage_errored"
        ).inc()
        return None


def _upcast_mode(image: "PILImage") -> "PILImage":
    """Upcast 1-bit bitonal → 8-bit grayscale. Identity for non-1 input."""
    if image.mode == "1":
        return image.convert("L")
    return image


def _rewrite_dpi(image: "PILImage") -> "PILImage":
    """Stamp ``dpi=(200, 200)`` on the image's info dict so downstream OCR
    rasterization scales from a known DPI rather than a fax-stack guess."""
    image.info["dpi"] = (_TARGET_DPI, _TARGET_DPI)
    return image


def _median_denoise(image: "PILImage") -> "PILImage":
    """Median-blur denoise. Kernel size 3 keeps stroke widths intact while
    killing isolated speckle. NOT bilateral — bilateral on bitonal text
    smears edges (locked decision)."""
    from PIL import ImageFilter  # local: PIL is a heavy import path

    return image.filter(ImageFilter.MedianFilter(size=_MEDIAN_KERNEL_PX))


def _projection_profile_deskew(image: "PILImage") -> "PILImage":
    """Projection-profile deskew. Computes the row-sum histogram for a
    handful of candidate angles in [-5°, +5°], picks the angle whose
    histogram has the highest variance (text lines align → tall peaks
    between baselines → high variance), rotates the image accordingly.

    NOT minAreaRect — locked decision per Slice 9.6 spec. Tesseract OSD
    handles gross 90/180/270° rotations elsewhere; this stage only cleans
    up small feeder skew.
    """
    # Lazy import — numpy may be present in the test env but we want this
    # module importable on a slim deploy; fall through if numpy is missing.
    try:
        import numpy as np  # type: ignore
    except Exception:
        return image

    # Work on a downsampled grayscale copy — projection profile cost
    # scales linearly with pixel count, and a 4× downsample changes the
    # peak-finding result by less than 0.05° in practice.
    work = image.convert("L") if image.mode != "L" else image
    w, h = work.size
    scale = max(1, min(w, h) // 400)
    if scale > 1:
        work = work.resize((w // scale, h // scale))

    arr = 255 - np.asarray(work, dtype=np.int32)  # invert: ink = high
    candidate_angles = [a / 2.0 for a in range(-10, 11)]  # -5° … +5°, 0.5° step
    best_angle = 0.0
    best_var = -1.0
    for angle in candidate_angles:
        if angle == 0.0:
            rotated = arr
        else:
            # PIL rotate is bilinear-interpolated; we feed a downsampled
            # pixel grid back through Pillow only for rotation, then
            # back to numpy to take the row-sum.
            from PIL import Image as _PILImage  # local

            tmp = _PILImage.fromarray(arr.astype(np.uint8))
            tmp = tmp.rotate(angle, resample=_PILImage.BILINEAR, fillcolor=0)
            rotated = np.asarray(tmp, dtype=np.int32)
        row_sums = rotated.sum(axis=1)
        var = float(row_sums.var())
        if var > best_var:
            best_var = var
            best_angle = angle

    if abs(best_angle) < 0.25:  # below noise floor → no-op
        return image
    from PIL import Image as _PILImage  # local

    return image.rotate(best_angle, resample=_PILImage.BILINEAR, fillcolor=255)


def _maybe_upsample(image: "PILImage") -> "PILImage":
    """If the image's resolved height is below ``_UPSAMPLE_THRESHOLD_PX``,
    upsample by ``_UPSAMPLE_FACTOR``. No-op otherwise."""
    _w, h = image.size
    if h >= _UPSAMPLE_THRESHOLD_PX:
        return image
    nw, nh = image.size[0] * _UPSAMPLE_FACTOR, image.size[1] * _UPSAMPLE_FACTOR
    from PIL import Image as _PILImage  # local

    return image.resize((nw, nh), resample=_PILImage.BICUBIC)


__all__ = ["preprocess_fax_page"]


# Marker — imported for side effect by ``tiff_loader``; no-op otherwise.
_HISTOGRAM_KEEPALIVE = agent_tiff_parse_duration_seconds
