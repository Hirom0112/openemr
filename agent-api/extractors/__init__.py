"""Document extractors package (W2_ARCHITECTURE.md §5.3).

Public API for Slice 1.3:

- ``extract`` — async pipeline: OCR → keyword classify → (Claude vision |
  UnknownDocument fallback) → validated ``ExtractionResult``.
- ``ExtractionFailed`` — generic failure exception (no PHI, no vendor text).
- ``classify_keywords`` / ``ClassifierVerdict`` — fast-path keyword classifier.
"""

from extractors.classifier import ClassifierVerdict, classify_keywords
from extractors.lab import ExtractionFailed, extract

__all__ = [
    "ClassifierVerdict",
    "ExtractionFailed",
    "classify_keywords",
    "extract",
]
