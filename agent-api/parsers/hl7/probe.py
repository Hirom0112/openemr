"""Pre-dispatch identity probe (Phase 9 Slice 9.4).

A lightweight regex pass over an HL7 v2 byte stream that extracts just
enough identity to feed Slice 9.2's resolver: MRN (PID-3.1), name
family + given (PID-5.1, PID-5.2), DOB (PID-7). Returns ``None`` when no
PID segment is present — callers should treat that as "send to
quarantine".

This deliberately does NOT use ``hl7apy``: it's run pre-dispatch on
every inbound message and the goal is sub-millisecond throughput. The
heavy structural parse happens later in :mod:`parsers.hl7.dispatch`.
"""

from __future__ import annotations

import re
from typing import Optional

from .normalizer import normalize_segment_terminators
from .types import CandidateHints

# Minimal regex: locate PID segment and split it on '|'. Tolerant of
# either '\r' or '\n' as the segment terminator since the normalizer
# may have run already.
_PID_RE = re.compile(rb"(?:^|[\r\n])PID\|([^\r\n]*)", re.DOTALL)


def _split_components(field: bytes) -> list[bytes]:
    return field.split(b"^") if field else []


def _decode(value: bytes) -> Optional[str]:
    if not value:
        return None
    try:
        out = value.decode("utf-8").strip()
    except UnicodeDecodeError:
        out = value.decode("utf-8", errors="replace").strip()
    return out or None


def probe_identity(raw: bytes) -> Optional[CandidateHints]:
    """Extract MRN / family-name / given-name / DOB from the PID segment.

    Returns ``None`` when no PID segment is present. Always returns a
    ``CandidateHints`` (with possibly-None members) when one is found —
    this keeps the contract symmetric with Slice 9.2's resolver, which
    treats "PID present, MRN missing" differently from "no PID at all".
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("probe_identity expects bytes")
    normalized = normalize_segment_terminators(bytes(raw))
    match = _PID_RE.search(normalized)
    if match is None:
        return None
    # Split on '|' — PID-1 is the set-id, PID-2 patient external id (deprecated),
    # PID-3 patient identifier list (CX), PID-5 patient name (XPN), PID-7 DOB (TS).
    fields = match.group(1).split(b"|")
    # Index 0 corresponds to PID-1 (set ID) since the leading "PID|" is consumed.
    pid_3 = fields[2] if len(fields) > 2 else b""
    pid_5 = fields[4] if len(fields) > 4 else b""
    pid_7 = fields[6] if len(fields) > 6 else b""

    # PID-3 is a repeating CX field; first repetition (split on '~') drives the MRN.
    pid_3_first = pid_3.split(b"~", 1)[0] if pid_3 else b""
    pid_3_components = _split_components(pid_3_first)
    mrn = _decode(pid_3_components[0]) if pid_3_components else None

    pid_5_first = pid_5.split(b"~", 1)[0] if pid_5 else b""
    pid_5_components = _split_components(pid_5_first)
    name_family = _decode(pid_5_components[0]) if len(pid_5_components) > 0 else None
    name_given = _decode(pid_5_components[1]) if len(pid_5_components) > 1 else None

    dob = _decode(pid_7)

    return CandidateHints(
        mrn=mrn,
        name_family=name_family,
        name_given=name_given,
        dob=dob,
    )
