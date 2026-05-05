"""Wave 2D — Tesseract PSM/DPI tuning knobs.

Verifies that:

  * Defaults remain (PSM=3, DPI=300) and produce bit-identical
    ``image_to_data`` output to the Wave 2A baseline (we assert by spying
    on the ``pytesseract.image_to_data`` kwargs — defaults must NOT pass
    a ``config`` argument so the call is identical to history).
  * Non-default PSM is forwarded as ``--psm N`` in the config string.
  * Non-default DPI is forwarded as ``--dpi N``.
  * Invalid PSM raises ``ValueError`` at resolution time.
  * Out-of-range DPI is clamped (warning, not raise).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.hard_failure

_AGENT_API = Path(__file__).resolve().parents[1]
if str(_AGENT_API) not in sys.path:
    sys.path.insert(0, str(_AGENT_API))

from documents import ocr as ocr_mod  # noqa: E402


def _have_pytesseract() -> bool:
    try:
        import pytesseract  # type: ignore  # noqa: F401
        from PIL import Image  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


@pytest.fixture
def _reset_settings(monkeypatch: pytest.MonkeyPatch):
    """Reset tesseract_psm/tesseract_dpi between tests."""
    from config import settings

    orig_psm = settings.tesseract_psm
    orig_dpi = settings.tesseract_dpi
    yield settings
    settings.tesseract_psm = orig_psm
    settings.tesseract_dpi = orig_dpi


# ── _resolve_tesseract_tuning ─────────────────────────────────────────────


def test_resolve_default_values(_reset_settings) -> None:
    _reset_settings.tesseract_psm = 3
    _reset_settings.tesseract_dpi = 300
    psm, dpi = ocr_mod._resolve_tesseract_tuning()
    assert (psm, dpi) == (3, 300)


def test_resolve_custom_values(_reset_settings) -> None:
    _reset_settings.tesseract_psm = 6
    _reset_settings.tesseract_dpi = 400
    psm, dpi = ocr_mod._resolve_tesseract_tuning()
    assert (psm, dpi) == (6, 400)


def test_resolve_invalid_psm_raises(_reset_settings) -> None:
    _reset_settings.tesseract_psm = 99
    with pytest.raises(ValueError, match="invalid tesseract_psm"):
        ocr_mod._resolve_tesseract_tuning()


def test_resolve_negative_psm_raises(_reset_settings) -> None:
    _reset_settings.tesseract_psm = -1
    with pytest.raises(ValueError):
        ocr_mod._resolve_tesseract_tuning()


def test_resolve_dpi_clamped_low(_reset_settings, caplog: pytest.LogCaptureFixture) -> None:
    _reset_settings.tesseract_psm = 3
    _reset_settings.tesseract_dpi = 10  # below 72
    with caplog.at_level("WARNING"):
        psm, dpi = ocr_mod._resolve_tesseract_tuning()
    assert dpi == ocr_mod._MIN_DPI
    assert any("ocr_tesseract_dpi_out_of_range" in r.message for r in caplog.records)


def test_resolve_dpi_clamped_high(_reset_settings) -> None:
    _reset_settings.tesseract_psm = 3
    _reset_settings.tesseract_dpi = 9999
    _, dpi = ocr_mod._resolve_tesseract_tuning()
    assert dpi == ocr_mod._MAX_DPI


# ── End-to-end forwarding into pytesseract.image_to_data ──────────────────


def _tiny_png_bytes() -> bytes:
    """A 4x4 white PNG — enough for PyMuPDF + tesseract to chew on."""
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_default_passes_no_config_kwarg(_reset_settings) -> None:
    """Regression guard: at defaults we must NOT add a config kwarg.

    This keeps the call signature byte-identical to the Wave 2A baseline,
    so any change in tesseract output between then and now is attributable
    to a tesseract upgrade — not to our wrapper.
    """
    if not _have_pytesseract():
        pytest.skip("pytesseract / Pillow not available")

    _reset_settings.tesseract_psm = 3
    _reset_settings.tesseract_dpi = 300

    import pytesseract  # type: ignore

    captured: dict = {}

    def _spy(img, **kwargs):
        captured["kwargs"] = dict(kwargs)
        # Mimic the real shape minimally so the rest of the function works.
        return {
            "text": [], "conf": [], "left": [], "top": [],
            "width": [], "height": [],
            "block_num": [], "par_num": [], "line_num": [],
        }

    with patch.object(pytesseract, "image_to_data", side_effect=_spy):
        ocr_mod._tesseract_extract_image(_tiny_png_bytes(), filetype="png")

    kw = captured["kwargs"]
    assert "config" not in kw, f"defaults must not add config kwarg, got {kw!r}"
    assert kw.get("output_type") == pytesseract.Output.DICT


def test_custom_psm_forwards_config(_reset_settings) -> None:
    if not _have_pytesseract():
        pytest.skip("pytesseract / Pillow not available")

    _reset_settings.tesseract_psm = 6
    _reset_settings.tesseract_dpi = 300

    import pytesseract  # type: ignore

    captured: dict = {}

    def _spy(img, **kwargs):
        captured["kwargs"] = dict(kwargs)
        return {
            "text": [], "conf": [], "left": [], "top": [],
            "width": [], "height": [],
            "block_num": [], "par_num": [], "line_num": [],
        }

    with patch.object(pytesseract, "image_to_data", side_effect=_spy):
        ocr_mod._tesseract_extract_image(_tiny_png_bytes(), filetype="png")

    assert captured["kwargs"].get("config") == "--psm 6"


def test_custom_psm_and_dpi_forwards_both(_reset_settings) -> None:
    if not _have_pytesseract():
        pytest.skip("pytesseract / Pillow not available")

    _reset_settings.tesseract_psm = 4
    _reset_settings.tesseract_dpi = 400

    import pytesseract  # type: ignore

    captured: dict = {}

    def _spy(img, **kwargs):
        captured["kwargs"] = dict(kwargs)
        return {
            "text": [], "conf": [], "left": [], "top": [],
            "width": [], "height": [],
            "block_num": [], "par_num": [], "line_num": [],
        }

    with patch.object(pytesseract, "image_to_data", side_effect=_spy):
        ocr_mod._tesseract_extract_image(_tiny_png_bytes(), filetype="png")

    cfg = captured["kwargs"].get("config", "")
    assert "--psm 4" in cfg
    assert "--dpi 400" in cfg


def test_custom_dpi_only(_reset_settings) -> None:
    if not _have_pytesseract():
        pytest.skip("pytesseract / Pillow not available")

    _reset_settings.tesseract_psm = 3
    _reset_settings.tesseract_dpi = 200

    import pytesseract  # type: ignore

    captured: dict = {}

    def _spy(img, **kwargs):
        captured["kwargs"] = dict(kwargs)
        return {
            "text": [], "conf": [], "left": [], "top": [],
            "width": [], "height": [],
            "block_num": [], "par_num": [], "line_num": [],
        }

    with patch.object(pytesseract, "image_to_data", side_effect=_spy):
        ocr_mod._tesseract_extract_image(_tiny_png_bytes(), filetype="png")

    cfg = captured["kwargs"].get("config", "")
    assert cfg == "--dpi 200"
