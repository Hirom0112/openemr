"""Wave 2A — OCR engine dispatcher contract tests.

These tests are isolated from paddlepaddle/paddleocr by design: the paddle
adapter is constructed via dependency injection in the smoke test so it
doesn't require the real wheels to be installed locally. The contract
itself (Protocol shape, dispatcher fall-back, instrumentation) is fully
exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.hard_failure

# tests/ live next to the package roots; make them importable.
_AGENT_API = Path(__file__).resolve().parents[1]
if str(_AGENT_API) not in sys.path:
    sys.path.insert(0, str(_AGENT_API))

from documents import ocr_engine  # noqa: E402
from documents.ocr import (  # noqa: E402
    BlockGranularity,
    LayoutBlock,
    TesseractEngine,
    extract_layout,
)
from documents.ocr_engine import (  # noqa: E402
    VALID_ENGINES,
    OCREngine,
    dispatch_extract_image,
    get_engine,
)


class _StubEngine:
    """Minimal Protocol-conforming engine for dispatcher tests."""

    name = "stub"

    def __init__(self, blocks: List[LayoutBlock] | None = None, raise_exc: Exception | None = None):
        self._blocks = blocks or []
        self._raise = raise_exc
        self.calls = 0

    def extract_image(self, image_bytes: bytes, *, filetype: str) -> List[LayoutBlock]:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return list(self._blocks)


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    """Each test gets a clean dispatcher cache."""
    ocr_engine._engine_cache.clear()
    yield
    ocr_engine._engine_cache.clear()


def test_protocol_conformance_tesseract() -> None:
    eng = TesseractEngine()
    assert isinstance(eng, OCREngine)
    assert eng.name == "tesseract"


def test_valid_engines_set() -> None:
    assert "tesseract" in VALID_ENGINES
    assert "paddleocr" in VALID_ENGINES


def test_get_engine_default_is_tesseract() -> None:
    eng = get_engine("tesseract")
    assert eng.name == "tesseract"


def test_get_engine_unknown_falls_back_to_tesseract(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        eng = get_engine("not-a-real-engine")
    assert eng.name == "tesseract"
    assert any("ocr_engine_unknown_falling_back" in r.message for r in caplog.records)


def test_get_engine_case_insensitive() -> None:
    eng = get_engine("TESSERACT")
    assert eng.name == "tesseract"


def test_dispatcher_emits_metric_and_log_on_success(caplog: pytest.LogCaptureFixture) -> None:
    stub_block = LayoutBlock(
        bbox_id="p1-b000",
        page=1,
        bbox=(0.0, 0.0, 10.0, 10.0),
        text="hello",
        ocr_confidence=0.9,
        granularity=BlockGranularity.WORD,
    )
    stub = _StubEngine(blocks=[stub_block])
    with patch.object(ocr_engine, "get_engine", return_value=stub):
        with caplog.at_level("INFO", logger="agent.tool"):
            blocks = dispatch_extract_image(b"x", filetype="png")
    assert blocks == [stub_block]
    assert stub.calls == 1
    # log_tool_outcome emits one INFO line on the "agent.tool" logger.
    tool_records = [r for r in caplog.records if r.name == "agent.tool"]
    assert len(tool_records) == 1
    rec = tool_records[0]
    assert getattr(rec, "engine", None) == "stub"
    assert getattr(rec, "outcome", None) == "success"
    assert getattr(rec, "n_blocks", None) == 1
    assert isinstance(getattr(rec, "duration_ms", None), int)


def test_dispatcher_emits_metric_and_log_on_error(caplog: pytest.LogCaptureFixture) -> None:
    stub = _StubEngine(raise_exc=RuntimeError("boom"))
    with patch.object(ocr_engine, "get_engine", return_value=stub):
        with caplog.at_level("INFO", logger="agent.tool"):
            with pytest.raises(RuntimeError, match="boom"):
                dispatch_extract_image(b"x", filetype="png")
    tool_records = [r for r in caplog.records if r.name == "agent.tool"]
    assert len(tool_records) == 1
    assert getattr(tool_records[0], "outcome", None) == "error"
    assert getattr(tool_records[0], "n_blocks", None) == 0


def test_extract_layout_image_path_uses_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify extract_layout (image branch) routes through the dispatcher.

    Uses a tiny PNG stub via PyMuPDF — but to avoid needing tesseract or paddle
    installed for this test we stub the dispatcher directly.
    """
    sentinel = LayoutBlock(
        bbox_id="p1-b000",
        page=1,
        bbox=(0.0, 0.0, 1.0, 1.0),
        text="x",
        ocr_confidence=0.5,
        granularity=BlockGranularity.WORD,
    )

    captured = {}

    def _fake_dispatch(image_bytes: bytes, *, filetype: str) -> List[LayoutBlock]:
        captured["filetype"] = filetype
        return [sentinel]

    monkeypatch.setattr(
        "documents.ocr_engine.dispatch_extract_image", _fake_dispatch
    )

    # Build a minimal valid PNG by reusing PIL (already a transitive dep).
    try:
        from PIL import Image
        import io
    except Exception:
        pytest.skip("Pillow not available")

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 255, 255)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    blocks = extract_layout(png_bytes)
    assert blocks == [sentinel]
    assert captured["filetype"] == "png"


def test_paddle_adapter_lazy_import() -> None:
    """PaddleEngine must not import paddleocr at construction time.

    Tesseract-only deployments should not pay the paddle cold-start.
    """
    # Wipe any cached paddleocr modules to make the assertion meaningful.
    for mod in list(sys.modules):
        if mod.startswith("paddleocr") or mod.startswith("paddlepaddle"):
            del sys.modules[mod]

    from documents.ocr_paddle import PaddleEngine

    eng = PaddleEngine()
    assert eng.name == "paddleocr"
    # Construction did NOT pull paddleocr in.
    assert "paddleocr" not in sys.modules
