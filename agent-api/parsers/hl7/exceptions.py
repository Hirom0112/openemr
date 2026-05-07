"""HL7 v2 parser exceptions (Phase 9 Slice 9.4).

Both subclass :class:`ValueError` so existing FastAPI error handlers in
``main.py`` that translate ``ValueError`` to HTTP 4xx still apply without
new wiring. Identity-resolution and ingest paths surface these via the
existing audit pipeline (PHI-safe ``code`` + ``control_id`` only — never
the raw HL7 message).
"""

from __future__ import annotations

from typing import Optional


class ParserMalformedError(ValueError):
    """Raised when an HL7 v2 message is structurally invalid.

    Examples: missing MSH, missing PID on a message that requires one,
    field count below the minimum required by the segment grammar.
    """

    def __init__(self, code: str, *, control_id: Optional[str] = None) -> None:
        self.code = code
        self.control_id = control_id
        super().__init__(f"hl7_parser_malformed:{code} (control_id={control_id or '<unparsed>'})")


class ParserUnsupportedError(ValueError):
    """Raised when MSH-9 message type is not in the supported set.

    v1 supports ``ADT^A08`` and ``ORU^R01`` only. Other message types are
    rejected at dispatch with this exception so the ingest layer can audit
    the rejection without attempting a partial parse.
    """

    def __init__(self, message_type: str, *, control_id: Optional[str] = None) -> None:
        self.message_type = message_type
        self.control_id = control_id
        super().__init__(
            f"hl7_parser_unsupported:{message_type} (control_id={control_id or '<unparsed>'})"
        )
