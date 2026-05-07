"""XLSX workbook parser (4-sheet importers) — Phase 9 Slice 9.5.

Public surface: :func:`parse_xlsx` (sync, bytes → ``ParsedWorkbook``),
:func:`parse_and_stage` (async helper that fans out to the
``observations.writer`` staging entry points), :func:`probe_identity`
(light Patient-sheet identity probe for the demographics resolver).
"""

from .exceptions import (
    XlsxMacroRejected,
    XlsxMalformedError,
    XlsxMergedCellsRejected,
)
from .parser import parse_and_stage, parse_xlsx
from .probe import probe_identity
from .types import CandidateHints, ParsedWorkbook

__all__ = [
    "CandidateHints",
    "ParsedWorkbook",
    "XlsxMacroRejected",
    "XlsxMalformedError",
    "XlsxMergedCellsRejected",
    "parse_and_stage",
    "parse_xlsx",
    "probe_identity",
]
