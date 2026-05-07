"""Tests for the DOCX paragraph + run extractor (Phase 9 Slice 9.6).

Covers:

- Magic-byte recognition (Zip archive headers).
- Chen referral fixture: para_idx=10 carries the demographic line bold
  run; para_idx=13 carries the leading-bold "History of Present Illness:"
  label and a body run containing "LDL-C at 142 mg/dL". These are the
  spec's required citation anchors.
- Section attribution: a paragraph following "Past Medical History:"
  inherits that section.
- Locator grammar: ``format_locator`` yields the right shape.
- Embedded-image drop log.
- Tracked-changes flag.
"""

from __future__ import annotations

import io
import logging
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.docx_loader import (  # noqa: E402
    extract_docx_paragraphs,
    format_locator,
    is_docx,
)
from documents.ocr import _is_docx  # noqa: E402

pytestmark = pytest.mark.hard_failure

DOCX_DIR = ROOT / "tests" / "fixtures" / "w2" / "multimodal" / "docx"
CHEN_DOCX = DOCX_DIR / "p01-chen-referral.docx"


# --------------------------------------------------------------------------- #
# Magic-byte recognition
# --------------------------------------------------------------------------- #


def test_is_docx_accepts_zip_signatures() -> None:
    assert is_docx(b"PK\x03\x04rest...")
    assert is_docx(b"PK\x05\x06rest...")
    assert is_docx(b"PK\x07\x08rest...")
    assert not is_docx(b"%PDF-1.4")
    assert not is_docx(b"")


def test_ocr_dispatch_recognizes_docx() -> None:
    assert _is_docx(CHEN_DOCX.read_bytes())


# --------------------------------------------------------------------------- #
# Locator grammar
# --------------------------------------------------------------------------- #


def test_format_locator_grammar() -> None:
    assert format_locator(1) == "para=1"
    assert format_locator(13, 2) == "para=13|run=2"
    assert format_locator(99, None) == "para=99"


# --------------------------------------------------------------------------- #
# Chen referral content
# --------------------------------------------------------------------------- #


def test_chen_para_10_demographic_line() -> None:
    """Spec asserts para=10|run=1 carries the bold demographic identifier
    line ("RE: Margaret Chen | DOB: 03/12/1968 | MRN: BHS-2847163").
    """
    paragraphs, meta = extract_docx_paragraphs(CHEN_DOCX.read_bytes())
    by_idx = {p.para_idx: p for p in paragraphs}
    assert 10 in by_idx, "expected paragraph 10 in chen referral"
    p10 = by_idx[10]
    assert len(p10.runs) >= 1
    r1 = p10.runs[0]
    assert r1.run_idx == 1
    assert r1.bold is True
    assert "Margaret Chen" in r1.text
    assert "BHS-2847163" in r1.text


def test_chen_para_13_run_2_contains_ldl_142() -> None:
    """Spec asserts para=13|run=2 contains the LDL-C 142 mg/dL fact —
    the prose extractor's required citable LabValue anchor."""
    paragraphs, _meta = extract_docx_paragraphs(CHEN_DOCX.read_bytes())
    by_idx = {p.para_idx: p for p in paragraphs}
    assert 13 in by_idx, "expected paragraph 13 in chen referral"
    p13 = by_idx[13]
    # Run 1 = leading bold "History of Present Illness: " label.
    assert p13.runs[0].run_idx == 1
    assert p13.runs[0].bold is True
    assert "History of Present Illness" in p13.runs[0].text
    # Run 2 = the body containing the LDL fact.
    assert len(p13.runs) >= 2
    r2 = p13.runs[1]
    assert r2.run_idx == 2
    assert "LDL-C at 142 mg/dL" in r2.text


def test_chen_section_attribution_inherits_past_medical_history() -> None:
    """Bullets following "Past Medical History:" should inherit it as
    their ``section``. Spec: leading-bold forward pass."""
    paragraphs, _meta = extract_docx_paragraphs(CHEN_DOCX.read_bytes())
    pmh_paras = [
        p for p in paragraphs
        if p.section and "Past Medical History" in p.section
    ]
    assert pmh_paras, "no paragraphs attributed to PMH section"
    # At least one bullet must mention an ICD-style code from the bullets.
    assert any("E78.5" in p.text or "I10" in p.text for p in pmh_paras)


def test_chen_referral_meta_flags() -> None:
    """Embedded-image + tracked-changes flags must be present (booleans,
    not None) — even if both happen to be zero/False on this fixture."""
    _paragraphs, meta = extract_docx_paragraphs(CHEN_DOCX.read_bytes())
    assert "tracked_changes_present" in meta
    assert "embedded_images_dropped" in meta
    assert isinstance(meta["tracked_changes_present"], bool)
    assert isinstance(meta["embedded_images_dropped"], int)


# --------------------------------------------------------------------------- #
# Tracked changes — synthetic DOCX with a w:ins element.
# --------------------------------------------------------------------------- #


def test_tracked_changes_flag_fires(tmp_path: Path) -> None:
    """Build a minimal DOCX containing a ``w:ins`` element and check the
    flag fires. python-docx itself can't author tracked changes; we
    splice the XML in via the zip archive directly."""
    import docx

    src = tmp_path / "src.docx"
    out = tmp_path / "tracked.docx"
    d = docx.Document()
    d.add_paragraph("Original sentence.")
    d.save(src)

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "word/document.xml":
                # Splice a <w:ins> element into the body so the flag fires.
                marker = b"</w:body>"
                ins_xml = (
                    b'<w:ins w:id="1" w:author="t" w:date="2026-05-06T00:00:00Z">'
                    b'<w:r><w:t>inserted</w:t></w:r></w:ins>'
                )
                data = data.replace(marker, ins_xml + marker)
            zout.writestr(item, data)

    paragraphs, meta = extract_docx_paragraphs(out.read_bytes())
    assert paragraphs, "synthetic doc should still have its original para"
    assert meta["tracked_changes_present"] is True


# --------------------------------------------------------------------------- #
# Embedded-image drop log
# --------------------------------------------------------------------------- #


def test_embedded_image_drop_emits_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Build a tiny DOCX with one inline image, run the loader, and
    assert (a) the image was NOT extracted (paragraph text empty / image-
    only), and (b) ``docx_image_dropped`` was logged."""
    import docx
    from PIL import Image

    img_path = tmp_path / "stub.png"
    Image.new("RGB", (4, 4), (255, 255, 255)).save(img_path, format="PNG")

    src = tmp_path / "with_image.docx"
    d = docx.Document()
    p = d.add_paragraph()
    p.add_run().add_picture(str(img_path))
    d.add_paragraph("Body text after image.")
    d.save(src)

    caplog.set_level(logging.INFO)
    paragraphs, meta = extract_docx_paragraphs(src.read_bytes())
    assert meta["embedded_images_dropped"] >= 1
    drop_records = [
        r for r in caplog.records if r.message == "docx_image_dropped"
    ]
    assert drop_records, "expected at least one docx_image_dropped log record"
