"""Tests for demographics.check (W2_ARCHITECTURE §5.6).

One parametrized case per row of the wrong-patient detection table, plus
two normalization-edge tests.
"""

from __future__ import annotations

import pytest

from demographics.check import (
    DemographicCheckResult,
    _dob_match,
    _mrn_match,
    _name_match,
    check_demographics,
)

pytestmark = pytest.mark.hard_failure

CHART = {"mrn": "12345", "name": "Jane Doe", "dob": "1962-03-14"}


# ── §5.6 row coverage ────────────────────────────────────────────────────────

#  fmt: off
_TABLE = [
    # (label, doc, expected_decision)
    ("mrn_match__name_match__dob_match",
        {"mrn": "12345", "name": "Jane Doe", "dob": "1962-03-14"}, "pass"),
    ("mrn_match__name_match__dob_mismatch",
        {"mrn": "12345", "name": "Jane Doe", "dob": "1962-03-15"}, "soft_warn"),
    ("mrn_match__name_mismatch__dob_match",
        {"mrn": "12345", "name": "Janet Roe", "dob": "1962-03-14"}, "soft_warn"),
    ("mrn_match__name_mismatch__dob_mismatch",
        {"mrn": "12345", "name": "Janet Roe", "dob": "1965-01-01"}, "soft_warn"),
    ("mrn_mismatch__any__any",
        {"mrn": "99999", "name": "Jane Doe", "dob": "1962-03-14"}, "hard_block"),
    ("mrn_unreadable__name_match__dob_match",
        {"mrn": "UNREADABLE", "name": "Jane Doe", "dob": "1962-03-14"}, "soft_warn"),
    ("mrn_unreadable__others_diverge",
        {"mrn": "UNREADABLE", "name": "Jane Doe", "dob": "1900-01-01"}, "hard_block"),
    ("mrn_absent__name_match__dob_match",
        {"mrn": None, "name": "Jane Doe", "dob": "1962-03-14"}, "pass"),
    ("mrn_absent__name_match__dob_mismatch",
        {"mrn": None, "name": "Jane Doe", "dob": "1965-01-01"}, "hard_block"),
    ("mrn_absent__name_mismatch__dob_match",
        {"mrn": None, "name": "Janet Roe", "dob": "1962-03-14"}, "soft_warn"),
    ("mrn_absent__name_mismatch__dob_mismatch",
        {"mrn": None, "name": "Janet Roe", "dob": "1965-01-01"}, "hard_block"),
    # 12th: weak-signal pass returns reason_code WEAK_SIGNAL_PASS
    ("mrn_absent_weak_signal_reason_code",
        {"mrn": None, "name": "Jane Doe", "dob": "1962-03-14"}, "pass"),
]
#  fmt: on


@pytest.mark.parametrize(
    "label,doc,expected",
    _TABLE,
    ids=[row[0] for row in _TABLE],
)
def test_demographic_table(label: str, doc: dict, expected: str) -> None:
    result: DemographicCheckResult = check_demographics(
        document_demographics=doc, chart_patient=CHART
    )
    assert result.decision == expected, (label, result)


def test_weak_signal_pass_reason_code() -> None:
    result = check_demographics(
        document_demographics={"mrn": None, "name": "Jane Doe", "dob": "1962-03-14"},
        chart_patient=CHART,
    )
    assert result.decision == "pass"
    assert result.reason_code == "WEAK_SIGNAL_PASS"


# ── Normalization edges ──────────────────────────────────────────────────────


def test_name_match_case_insensitive_whitespace_collapsed() -> None:
    assert _name_match("JOHN  DOE", "John Doe") == "match"
    assert _name_match("  john\tdoe ", "John Doe") == "match"


def test_dob_match_strict_iso() -> None:
    # Strict equality — "1962-3-14" is not the same string as "1962-03-14".
    assert _dob_match("1962-3-14", "1962-03-14") == "mismatch"
    assert _dob_match("1962-03-14", "1962-03-14") == "match"


def test_mrn_helper_unreadable_and_absent() -> None:
    assert _mrn_match(None, "12345") == "absent"
    assert _mrn_match("", "12345") == "absent"
    assert _mrn_match("UNREADABLE", "12345") == "unreadable"
    assert _mrn_match("12345", "12345") == "match"
    assert _mrn_match("99999", "12345") == "mismatch"
