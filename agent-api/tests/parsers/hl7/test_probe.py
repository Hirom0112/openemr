"""Tests for parsers.hl7.probe (Phase 9 Slice 9.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from parsers.hl7.probe import probe_identity

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "w2" / "multimodal" / "hl7v2"


def test_probe_identity_extracts_pid_fields_from_p01_oru():
    raw = (FIXTURE_DIR / "p01-chen-oru-r01.hl7").read_bytes()
    hints = probe_identity(raw)
    assert hints is not None
    assert hints.mrn == "BHS-2847163"
    assert hints.name_family == "CHEN"
    assert hints.name_given == "MARGARET"
    assert hints.dob == "19680312"


def test_probe_identity_returns_none_when_pid_missing():
    raw = (
        b"MSH|^~\\&|A|B|C|D|20260101000000||ORU^R01^ORU_R01|MSG-1|P|2.5.1\r"
        b"OBR|1|ORD|FIL|57698-3^Lipid panel^LN\r"
    )
    assert probe_identity(raw) is None


def test_probe_identity_round_trip_matches_hl7apy_for_all_fixtures():
    """probe_identity (regex) and hl7apy parse must agree on PID-3.1, PID-5,
    PID-7 across every fixture. This guards against drift between the
    pre-dispatch fast path and the structural parse."""
    pytest.importorskip("hl7apy")
    from hl7apy.parser import parse_message

    fixtures = sorted(FIXTURE_DIR.glob("*.hl7"))
    assert fixtures, "expected HL7 fixtures to be present"

    for path in fixtures:
        raw = path.read_bytes()
        hints = probe_identity(raw)
        assert hints is not None, f"no PID found in {path.name}"

        message = parse_message(raw.decode("utf-8"), find_groups=False, validation_level=2)
        pid = next(child for child in message.children if child.name == "PID")

        assert hints.mrn == pid.pid_3.pid_3_1.value, f"MRN mismatch in {path.name}"
        assert hints.name_family == pid.pid_5.xpn_1.value, (
            f"family-name mismatch in {path.name}"
        )
        assert hints.name_given == pid.pid_5.xpn_2.value, (
            f"given-name mismatch in {path.name}"
        )
        assert hints.dob == pid.pid_7.value, f"DOB mismatch in {path.name}"
