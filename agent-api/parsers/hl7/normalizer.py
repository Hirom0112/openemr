"""Pre-parse HL7 v2 segment-terminator normalizer (Phase 9 Slice 9.4).

The Cohort-5 Week-2 fixtures arrive as a single byte sequence with the
HL7 segment terminator (``\\r``) stripped on disk. ``hl7apy`` requires
``\\r`` to delimit segments; without it the message is treated as a
single oversized MSH and the parse fails with ``UnsupportedVersion``.

This module re-injects ``\\r`` immediately before each known segment
header — but **only** when the input contains neither ``\\r`` nor ``\\n``.
Well-formed messages (with either terminator already present) pass
through untouched. The regex is anchored to a closing alphanumeric
character so a free-text field whose value happens to contain a token
like ``OBX|`` (e.g. an EVN-6 reason "OBX|missing in feed") is not
misinterpreted as a new segment boundary.

This is the single source of truth for the segment-terminator quirk;
``parsers.hl7.dispatch`` calls this once before handing bytes to
``hl7apy.parser.parse_message``.
"""

from __future__ import annotations

import re

# Known HL7 v2 segment headers used by ADT^A08 and ORU^R01 in v1. Keep
# this list narrow — adding speculative segments widens the false-match
# surface. Add explicitly when a new message type lands.
_SEGMENT_HEADERS = (
    "MSH",
    "EVN",
    "PID",
    "PD1",
    "PV1",
    "NK1",
    "GT1",
    "IN1",
    "AL1",
    "ORC",
    "OBR",
    "OBX",
    "NTE",
)

# Anchor to a closing alphanumeric (or '.') character so we only fire
# at boundaries inside a single-line concat, never inside a free-text
# value containing the literal substring "OBX|". The MSH prefix is also
# matched, but at offset 0 there is no preceding character — handled by
# the explicit MSH-at-start guard below.
_BOUNDARY_RE = re.compile(
    rb"(?<=[A-Za-z0-9.])(" + b"|".join(h.encode("ascii") for h in _SEGMENT_HEADERS) + rb")\|"
)


def normalize_segment_terminators(raw: bytes) -> bytes:
    """Inject ``\\r`` before known segment headers when neither terminator
    is present in *raw*. Otherwise return *raw* unchanged.

    The gate condition is intentionally strict: even one ``\\n`` or
    ``\\r`` in the byte stream means the source already has *some*
    terminator strategy and we should not rewrite it. The downstream
    parser will raise on truly mixed/malformed input rather than have
    this function silently corrupt it.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("normalize_segment_terminators expects bytes")
    if b"\r" in raw or b"\n" in raw:
        return bytes(raw)
    return _BOUNDARY_RE.sub(rb"\r\1|", bytes(raw))
