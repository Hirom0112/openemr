"""Unit tests for the pre-tool-call patient-scope check.

Covers the five behaviours required by the dispatcher contract:

1. Patient inside census          → allowed
2. Patient outside census          → blocked with scope_violation
3. Tool without patient_id arg     → allowed (nothing to scope)
4. Empty/missing census            → allowed (fail-open) + warn
5. Tool not in patient-keyed list  → allowed (e.g. get_census_summary)
"""

from __future__ import annotations

import logging

import pytest

from auth.scope import PATIENT_KEYED_TOOLS, check_patient_scope

pytestmark = pytest.mark.hard_failure


def test_patient_in_census_is_allowed() -> None:
    allowed, reason = check_patient_scope(
        "get_patient_briefing",
        {"patient_id": "8"},
        {"patient_ids": ["8", "9", "10"], "session_id": "s1"},
    )
    assert allowed is True
    assert reason is None


def test_patient_not_in_census_is_blocked() -> None:
    allowed, reason = check_patient_scope(
        "get_patient_briefing",
        {"patient_id": "999"},
        {"patient_ids": ["8", "9", "10"], "session_id": "s1"},
    )
    assert allowed is False
    assert reason is not None
    assert "scope_violation" in reason
    assert "999" in reason


def test_tool_without_patient_id_is_allowed() -> None:
    # generate_handoff is patient-keyed-by-list (not single patient_id),
    # but more importantly query_patient_records called with no patient_id
    # at all should not be blocked here.
    allowed, reason = check_patient_scope(
        "get_patient_briefing",
        {},  # no patient_id key
        {"patient_ids": ["8"], "session_id": "s1"},
    )
    assert allowed is True
    assert reason is None


def test_empty_census_fails_closed_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # VUL-0001 mitigation (2026-05-15): the prior fail-open branch was
    # exploitable by multi-turn social-engineering; deny when there is no
    # authoritative panel to validate against.
    caplog.set_level(logging.WARNING, logger="auth.scope")
    allowed, reason = check_patient_scope(
        "get_patient_briefing",
        {"patient_id": "999"},
        {"patient_ids": [], "session_id": "s1"},
    )
    assert allowed is False
    assert reason is not None and "no active census" in reason
    assert any(
        "tool_scope_check_deny_empty_census" in r.message for r in caplog.records
    )


def test_missing_census_key_fails_closed() -> None:
    # Same VUL-0001 mitigation when the key is absent entirely.
    allowed, reason = check_patient_scope(
        "get_patient_briefing",
        {"patient_id": "999"},
        {"session_id": "s1"},  # no patient_ids key at all
    )
    assert allowed is False
    assert reason is not None and "no active census" in reason


def test_non_patient_keyed_tool_is_allowed() -> None:
    # get_census_summary IS the scope source — never blocked here even
    # if it carried a patient_id (it doesn't, but defensively).
    allowed, reason = check_patient_scope(
        "get_census_summary",
        {"provider_id": "1", "patient_ids": ["8", "9"]},
        {"patient_ids": ["8"], "session_id": "s1"},
    )
    assert allowed is True
    assert reason is None


def test_patient_id_int_vs_str_is_normalized() -> None:
    # session_context arrives from JSON so census_ids may be strings,
    # but a stray int patient_id from the planner should still match.
    allowed, _ = check_patient_scope(
        "get_medication_safety",
        {"patient_id": 8},
        {"patient_ids": ["8"], "session_id": "s1"},
    )
    assert allowed is True


def test_all_documented_patient_keyed_tools_are_enforced() -> None:
    expected = {
        "get_patient_briefing",
        "get_medication_safety",
        "query_patient_records",
        "get_triage_rationale",
    }
    assert expected <= PATIENT_KEYED_TOOLS
