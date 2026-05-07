"""Tests for parsers.hl7.normalizer (Phase 9 Slice 9.4)."""

from __future__ import annotations

import pytest

from parsers.hl7.normalizer import normalize_segment_terminators

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


def test_passthrough_when_cr_already_present():
    """Well-formed HL7 with \\r terminators must not be rewritten."""
    raw = (
        b"MSH|^~\\&|A|B|C|D|20260101000000||ORU^R01^ORU_R01|MSG-1|P|2.5.1\r"
        b"PID|1||MRN-1^^^MRN^MR||DOE^JANE||19700101|F\r"
    )
    out = normalize_segment_terminators(raw)
    assert out == raw, "passthrough must be byte-identical when \\r is present"


def test_passthrough_when_lf_present():
    """Even an \\n-only stream must pass through (let parser decide)."""
    raw = (
        b"MSH|^~\\&|A|B|C|D|20260101000000||ORU^R01^ORU_R01|MSG-1|P|2.5.1\n"
        b"PID|1||MRN-1^^^MRN^MR||DOE^JANE||19700101|F\n"
    )
    out = normalize_segment_terminators(raw)
    assert out == raw


def test_single_line_concat_gets_terminators_inserted():
    """The Cohort-5 fixture quirk: single-line concat → \\r injection."""
    raw = (
        b"MSH|^~\\&|A|B|C|D|20260101000000||ORU^R01^ORU_R01|MSG-1|P|2.5.1"
        b"PID|1||MRN-1^^^MRN^MR||DOE^JANE||19700101|F"
        b"OBR|1|ORD|FIL|57698-3^Lipid panel^LN"
        b"OBX|1|NM|2093-3^Total Cholesterol^LN||218|mg/dL|<200|H|||F"
    )
    out = normalize_segment_terminators(raw)
    # The MSH-leading byte sequence must NOT be prefixed (regex is anchored
    # to a preceding alphanumeric character).
    assert out.startswith(b"MSH|"), "MSH at offset 0 must not be prefixed"
    assert b"\rPID|" in out
    assert b"\rOBR|" in out
    assert b"\rOBX|" in out
    # No spurious double terminators
    assert b"\r\r" not in out


def test_evn_reason_containing_obx_pipe_substring_is_not_corrupted():
    """Regression: EVN-6 free-text reason containing the literal 'OBX|'
    substring must not trigger spurious terminator insertion mid-field."""
    # Construct a single-line ADT whose EVN-6 reason field contains 'OBX|'
    # as a literal substring (e.g. a clinician note about an OBX feed).
    raw = (
        b"MSH|^~\\&|A|B|C|D|20260101000000||ADT^A08^ADT_A01|MSG-1|P|2.5.1"
        b"EVN|A08|20260101000000|||1234567890^DOC^A|note about OBX|missing in feed"
        b"PID|1||MRN-1^^^MRN^MR||DOE^JANE||19700101|F"
    )
    out = normalize_segment_terminators(raw)
    # The 'OBX|' embedded inside EVN-6 must NOT have a \r injected before
    # it. The character preceding 'OBX|' is a space — not [A-Za-z0-9.] —
    # so the anchored regex must not match.
    assert b" OBX|" in out, "embedded 'OBX|' substring must survive intact"
    assert b"\rOBX|" not in out, "must not insert \\r before non-segment 'OBX|'"
    # PID still gets terminated correctly
    assert b"\rPID|" in out
    assert b"\rEVN|" in out
