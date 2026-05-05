"""Regression test for the no_phi_in_logs cascade artifact.

When the runner's capture handler attaches at root → DEBUG, third-party
libraries (httpx, httpcore, anthropic._base_client, langgraph, ...) emit
DEBUG-level records that include raw HTTP request/response bodies. For a
clinical-document pipeline that means the synthetic PHI from the source
document leaks into the captured log set, and the no_phi_in_logs rubric
— correctly — flags those emissions.

The runner pins these third-party loggers to WARNING for the duration of
the capture so only first-party graph events reach the rubric. This test
guards that contract: DEBUG records emitted on the third-party loggers
during capture must not appear in the captured set, and the rubric must
return True for a representative cascade payload.
"""
from __future__ import annotations

import logging

import pytest

from evals.rubrics_mechanical import no_phi_in_logs
from evals.runner import (
    RunOutcome,
    _attach_capture,
    _detach_capture,
)


# The set of third-party loggers whose DEBUG emissions cascade synthetic
# PHI into captured_logs. Hardcoded here (rather than imported) so the
# test exercises the contract from the rubric's perspective: these names
# must never appear in captured_logs at DEBUG level after _attach_capture.
_THIRD_PARTY_LOGGER_NAMES = frozenset({
    "httpx",
    "httpcore",
    "httpcore.http11",
    "httpcore.connection",
    "anthropic",
    "anthropic._base_client",
    "openai",
    "urllib3",
    "langgraph",
    "langchain",
    "langchain_core",
})


pytestmark = pytest.mark.hard_failure


def _emit_synthetic_cascade() -> None:
    """Mimic the third-party DEBUG emissions seen during a real CI run."""
    logging.getLogger("anthropic._base_client").debug(
        "request_options: %r",
        {"messages": [{"role": "user", "content": "Patient: Marcus Webb MRN: 100847 DOB: 1962-03-14"}]},
    )
    logging.getLogger("httpx").debug(
        "HTTP Request: POST https://api.anthropic.com/v1/messages"
    )
    logging.getLogger("httpcore.http11").debug(
        "send_request_body. body: 'Patient: Jane Doe DOB: 1980-01-01 MRN-1'"
    )
    logging.getLogger("langgraph").debug("checkpoint write: 100847 Marcus Webb")


def test_third_party_debug_does_not_leak_into_captured_logs() -> None:
    handler = _attach_capture()
    try:
        _emit_synthetic_cascade()
        # First-party event must still flow through.
        logging.getLogger("graph.nodes.critic").info(
            "graph_critic_decision",
            extra={"request_id": "eval-x", "decision": "hard_block"},
        )
    finally:
        _detach_capture(handler)

    third_party_records = [
        r for r in handler.records if r["name"] in _THIRD_PARTY_LOGGER_NAMES
    ]
    assert third_party_records == [], (
        "Third-party DEBUG records leaked into captured_logs — "
        "the no_phi_in_logs rubric will trip on cascading PHI from request bodies. "
        f"Offenders: {[(r['name'], r['message'][:80]) for r in third_party_records]}"
    )

    first_party = [r for r in handler.records if r["name"].startswith("graph.")]
    assert first_party, "expected first-party graph events to still be captured"


def test_no_phi_in_logs_passes_after_third_party_quieting() -> None:
    handler = _attach_capture()
    try:
        _emit_synthetic_cascade()
    finally:
        _detach_capture(handler)

    outcome = RunOutcome(case_id="cascade-repro", captured_logs=list(handler.records))
    assert no_phi_in_logs(outcome) is True


def test_third_party_logger_level_is_restored_after_detach() -> None:
    target = logging.getLogger("httpx")
    target.setLevel(logging.INFO)
    try:
        handler = _attach_capture()
        try:
            assert target.level == logging.WARNING
        finally:
            _detach_capture(handler)
        assert target.level == logging.INFO
    finally:
        target.setLevel(logging.NOTSET)
