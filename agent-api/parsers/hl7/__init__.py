"""HL7 v2 parser package (Phase 9 Slice 9.4).

Public surface (importable directly from ``parsers.hl7``):

* :func:`parse_hl7` — top-level dispatcher: bytes → ``LabReport`` (ORU^R01)
  or ``DemographicUpdateEvent`` (ADT^A08).
* :func:`probe_identity` — pre-dispatch regex probe for MRN/name/DOB,
  used by Slice 9.2's identity resolver.
* :class:`CandidateHints` — return type of ``probe_identity``.
* :class:`DemographicUpdateEvent` — structured ADT^A08 output type.
* :class:`ParserMalformedError`, :class:`ParserUnsupportedError` —
  taxonomy for the ingest layer to surface.
* :func:`normalize_segment_terminators` — exposed for testing the
  single-line fixture quirk in isolation.

Per todo.md Slice 9.4 and importlinter contract
``parsers-hl7-isolated``: this package is a structured-lane leaf. It
must not depend on FastAPI, the LangGraph topology, the dispatcher tool
surface, or any clinical sibling.
"""

from .dispatch import parse_hl7
from .exceptions import ParserMalformedError, ParserUnsupportedError
from .normalizer import normalize_segment_terminators
from .probe import probe_identity
from .types import CandidateHints, DemographicUpdateEvent

__all__ = [
    "CandidateHints",
    "DemographicUpdateEvent",
    "ParserMalformedError",
    "ParserUnsupportedError",
    "normalize_segment_terminators",
    "parse_hl7",
    "probe_identity",
]
