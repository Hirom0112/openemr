"""Multimodal magic-byte format detection (Phase 3 Part B' refactor).

Promoted out of ``main.py`` so non-HTTP callers (the LangGraph
multimodal-extractor node, the eval runner, parser tests) can detect a
buffer's format without importing the FastAPI module. The function and
all magic-byte constants are byte-identical to the original
``main._detect_ingest_format`` — this module is the single source of
truth; ``main.py`` re-exports for backwards compatibility.

Order of detection matters: HL7 v2 ("MSH|") is text-prefixed and easy to
discriminate up-front; TIFF has a fixed 4-byte signature; DOCX and XLSX
both start with the generic Zip ``PK`` magic, so we peek inside the
zip's central directory for "word/" vs "xl/" markers to disambiguate.
PDF/PNG fall through to the existing extract_layout pipeline upstream —
the PDF page-guard in ``document_ingest`` is the single source of truth
for those.

All probes operate on the leading ~1 KB only — never load the full
upload to detect format.
"""
from __future__ import annotations

from typing import Final, Literal, Tuple

DetectedFormat = Literal[
    "hl7", "xlsx", "docx", "tiff", "pdf", "png", "unknown"
]

HL7_MAGIC: Final[bytes] = b"MSH|"
PDF_MAGIC: Final[bytes] = b"%PDF-"
PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
TIFF_MAGIC_LE: Final[bytes] = b"II*\x00"
TIFF_MAGIC_BE: Final[bytes] = b"MM\x00*"
ZIP_MAGIC: Final[Tuple[bytes, ...]] = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
)


def detect_ingest_format(data: bytes) -> DetectedFormat:
    """Return one of ``hl7|xlsx|docx|tiff|pdf|png|unknown`` from magic bytes.

    ``data`` is the (possibly truncated) uploaded byte string. Only the
    leading prefix is inspected for fixed magic; for Zip-prefixed inputs
    we crack the central directory to differentiate XLSX (``xl/``) from
    DOCX (``word/``). On any failure to introspect the zip we return
    ``"unknown"`` rather than guess — the caller surfaces unknowns as
    415 (or routes them through the legacy PDF path).
    """
    if not data:
        return "unknown"
    if data.startswith(HL7_MAGIC):
        return "hl7"
    if data.startswith(PDF_MAGIC):
        return "pdf"
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(TIFF_MAGIC_LE) or data.startswith(TIFF_MAGIC_BE):
        return "tiff"
    if any(data.startswith(m) for m in ZIP_MAGIC):
        try:
            import io as _io
            import zipfile as _zipfile

            with _zipfile.ZipFile(_io.BytesIO(data)) as zf:
                names = zf.namelist()
        except Exception:
            return "unknown"
        for name in names:
            if name.startswith("word/"):
                return "docx"
            if name.startswith("xl/"):
                return "xlsx"
        return "unknown"
    return "unknown"


__all__ = [
    "DetectedFormat",
    "detect_ingest_format",
    "HL7_MAGIC",
    "PDF_MAGIC",
    "PNG_MAGIC",
    "TIFF_MAGIC_LE",
    "TIFF_MAGIC_BE",
    "ZIP_MAGIC",
]
