"""``documents`` package — Path A/B PDF ingestion + extraction store.

Public surface (re-exported here so callers can ``from documents import …``):

* :class:`WriteResult`, :func:`write_document`, :class:`FhirWriteError` —
  FHIR Binary + DocumentReference writer (slice 1.5, W2 §4.2).
* ``store`` submodule remains importable directly for the
  ``copilot_doc_extractions`` claim/complete/fail surface.
"""

from documents.fhir_writer import FhirWriteError, WriteResult, write_document

__all__ = ["FhirWriteError", "WriteResult", "write_document"]
