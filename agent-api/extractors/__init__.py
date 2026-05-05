"""Document extractors package (W2_ARCHITECTURE.md §5.3).

Public API:

- ``extract`` — async lab-extractor pipeline (Slice 1.3).
- ``extract_intake`` — async intake-form extractor pipeline (Slice 4.5).
- ``ExtractionFailed`` — generic failure exception (no PHI, no vendor text).
- ``classify_keywords`` / ``ClassifierVerdict`` — fast-path keyword classifier.
- ``IntakeForm`` — re-exported intake schema for downstream consumers.
"""

from extractors.classifier import ClassifierVerdict, classify_keywords
from extractors.intake import extract_intake
from extractors.lab import ExtractionFailed, extract
from extractors.schemas import IntakeForm

__all__ = [
    "ClassifierVerdict",
    "ExtractionFailed",
    "IntakeForm",
    "classify_keywords",
    "extract",
    "extract_intake",
]
