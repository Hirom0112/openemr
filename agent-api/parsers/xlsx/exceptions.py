"""XLSX parser exceptions (Phase 9 Slice 9.5).

All subclass :class:`ValueError` so the existing FastAPI error handlers in
``main.py`` that translate ``ValueError`` to HTTP 4xx still apply without
new wiring. The ingest layer surfaces these via the existing audit
pipeline (PHI-safe ``code`` only — never raw cell contents).
"""

from __future__ import annotations

from typing import Optional


class XlsxMalformedError(ValueError):
    """Raised when an XLSX byte stream cannot be opened by openpyxl, or
    when none of the known sheets (Patient, Medications, Labs_Trend,
    Care_Gaps) are present.
    """

    def __init__(self, code: str, *, detail: Optional[str] = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"xlsx_parser_malformed:{code}" + (f" ({detail})" if detail else ""))


class XlsxMacroRejected(ValueError):
    """Raised at dispatch when ``xl/vbaProject.bin`` is detected in the
    workbook archive (a macro-enabled .xlsm that we refuse to execute).
    """

    def __init__(self) -> None:
        self.code = "xlsx_macro_rejected"
        super().__init__("xlsx_parser_malformed:xlsx_macro_rejected")


class XlsxMergedCellsRejected(ValueError):
    """Raised when any of the four known sheets contains merged cell
    ranges; the v1 parser cannot disambiguate header alignment in the
    presence of merges and rather than guess we reject upstream.
    """

    def __init__(self, sheet: str) -> None:
        self.code = "xlsx_merged_cells_rejected"
        self.sheet = sheet
        super().__init__(f"xlsx_parser_malformed:xlsx_merged_cells_rejected (sheet={sheet})")
