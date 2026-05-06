"""OCR engine dispatcher (Wave 2A).

Defines the ``OCREngine`` Protocol that every OCR backend must satisfy and
the ``get_engine`` dispatcher which selects an implementation based on
``settings.ocr_engine``.

The Protocol is intentionally narrow: a single ``extract_image`` method that
returns engine-agnostic ``LayoutBlock`` objects. Engine-specific data
(tesseract dicts, paddle line tuples, etc.) MUST be normalised inside the
adapter; nothing paddle- or tesseract-specific is allowed to leak past the
Protocol surface.

Adding a new engine = add an adapter that satisfies the Protocol, register
it in ``_ENGINES`` below, and document the env value in
``config.Settings.ocr_engine``.
"""

from __future__ import annotations

import logging
import time
from typing import List, Protocol, runtime_checkable

from agent.metrics import (
    agent_ocr_engine_invocations_total,
    agent_ocr_extraction_duration_seconds,
)
from documents.ocr import LayoutBlock
from observability.tool_logging import log_tool_outcome

logger = logging.getLogger(__name__)


@runtime_checkable
class OCREngine(Protocol):
    """Protocol every OCR backend must satisfy.

    ``name`` is a short string used as the Prometheus label and as the
    ``OCR_ENGINE`` env value. ``extract_image`` MUST return a list of
    ``LayoutBlock`` with WORD-level granularity at minimum; the dispatcher
    does not inspect the contents.
    """

    name: str

    def extract_image(self, image_bytes: bytes, *, filetype: str) -> List[LayoutBlock]:
        ...


# Valid env values. Kept here (not in config.py) so adding an engine touches
# one file. ``settings.ocr_engine`` is validated against this set.
VALID_ENGINES: frozenset[str] = frozenset({"tesseract", "paddleocr"})


def _build_engine(name: str) -> OCREngine:
    """Construct the named engine. Lazy: paddle is only imported on demand."""
    if name == "tesseract":
        from documents.ocr import TesseractEngine

        return TesseractEngine()
    if name == "paddleocr":
        from documents.ocr_paddle import PaddleEngine

        return PaddleEngine()
    raise ValueError(f"unknown OCR engine: {name!r}")


_engine_cache: dict[str, OCREngine] = {}


def get_engine(name: str | None = None) -> OCREngine:
    """Resolve the configured OCR engine.

    If ``name`` is None, reads ``settings.ocr_engine`` (case-insensitive).
    Unknown values log a warning and fall back to ``tesseract`` — never
    raise, because OCR is on the document-ingest hot path and a typo'd env
    var should not 500 every upload.
    """
    if name is None:
        # Local import — avoids a top-level import cycle if config.py grows
        # to reach back into documents at import time.
        from config import settings

        name = settings.ocr_engine

    requested = (name or "").strip().lower()
    if requested not in VALID_ENGINES:
        logger.warning(
            "ocr_engine_unknown_falling_back",
            extra={"requested": requested, "valid": sorted(VALID_ENGINES)},
        )
        requested = "tesseract"

    cached = _engine_cache.get(requested)
    if cached is not None:
        return cached

    try:
        engine = _build_engine(requested)
    except Exception:  # noqa: BLE001 — broad on purpose: never break ingest
        logger.exception(
            "ocr_engine_init_failed_falling_back",
            extra={"requested": requested},
        )
        if requested != "tesseract":
            engine = _build_engine("tesseract")
            requested = "tesseract"
        else:
            raise

    _engine_cache[requested] = engine
    return engine


def dispatch_extract_image(
    image_bytes: bytes, *, filetype: str
) -> List[LayoutBlock]:
    """Run the configured engine's ``extract_image`` with metrics + logs.

    Centralised so every engine gets identical instrumentation per
    CLAUDE.md §Observability — one structured log line + one Prometheus
    metric, never one without the other.
    """
    engine = get_engine()

    # Phase 3 Wave 2A — pre-OCR photo preprocessing. No-op when
    # ``settings.photo_preprocess`` is "off" (default); never raises into
    # the OCR call site (errors fall through to the original bytes).
    try:
        from documents.photo_preprocess import preprocess_photo
        image_bytes = preprocess_photo(image_bytes)
    except Exception:  # noqa: BLE001 — defensive: never break ingest
        logger.exception("photo_preprocess.unexpected_error_falling_through")

    started = time.perf_counter()
    outcome = "success"
    blocks: List[LayoutBlock] = []
    try:
        blocks = engine.extract_image(image_bytes, filetype=filetype)
    except Exception:
        outcome = "error"
        agent_ocr_engine_invocations_total.labels(
            engine=engine.name, outcome=outcome
        ).inc()
        duration_s = time.perf_counter() - started
        agent_ocr_extraction_duration_seconds.labels(engine=engine.name).observe(
            duration_s
        )
        log_tool_outcome(
            tool_name="ocr_extract_image",
            duration_ms=int(duration_s * 1000),
            cache="n/a",
            extra={
                "engine": engine.name,
                "filetype": filetype,
                "outcome": outcome,
                "n_blocks": 0,
            },
        )
        raise

    duration_s = time.perf_counter() - started
    agent_ocr_engine_invocations_total.labels(
        engine=engine.name, outcome=outcome
    ).inc()
    agent_ocr_extraction_duration_seconds.labels(engine=engine.name).observe(
        duration_s
    )
    log_tool_outcome(
        tool_name="ocr_extract_image",
        duration_ms=int(duration_s * 1000),
        cache="n/a",
        extra={
            "engine": engine.name,
            "filetype": filetype,
            "outcome": outcome,
            "n_blocks": len(blocks),
        },
    )
    return blocks
