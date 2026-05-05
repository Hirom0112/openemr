"""Per-LabValue FHIR-Observation provenance writer (W2 Phase 2).

Surfaces ``write_observation`` and ``deterministic_observation_id`` so the
``/document/ingest`` route can persist one Observation per extracted lab
value, with ``derivedFrom`` referencing the source DocumentReference.

Reuses the custom JWT-protected upload endpoint in
``oe-module-clinical-copilot`` because OpenEMR's deployed FHIR layer does
not implement Observation write (POST /apis/default/fhir/Observation → 404).
"""

from observations.writer import (  # noqa: F401
    deterministic_observation_id,
    lookup_loinc,
    write_observation,
)

__all__ = [
    "deterministic_observation_id",
    "lookup_loinc",
    "write_observation",
]
