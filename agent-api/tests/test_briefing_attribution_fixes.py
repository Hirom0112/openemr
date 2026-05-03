"""Tests for the two attribution fixes in the briefing pipeline.

Part 1: ``Citation.value`` for a ``MedicationRequest`` includes the medication
        name so the verifier's substring check (``source_value`` in
        ``citation.value``) can succeed when the model copies the med name into
        ``source_value``.

Part 2: The system prompt explicitly tells the model that meta-observations
        about a blank/absent section belong in ``alerts``, not in
        ``sections[*].claims``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from briefing.context_builder import build
from briefing.generator import _SYSTEM, generate_briefing
from briefing.schema import BriefingResponse


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.get_event_loop().run_until_complete(coro)


def _med_bundle() -> tuple[dict[str, Any], dict[str, Any]]:
    """Synthetic patient + bundle with a single MedicationRequest.

    Dose and route are intentionally empty to mirror the synthetic-data shape
    that exposed the bug: with the old builder ``Citation.value`` collapsed to
    ``""`` and every model claim citing the med got stripped.
    """
    patient = {
        "id": "pt-attr-001",
        "name": [{"given": ["Test"], "family": "Patient"}],
        "birthDate": "1970-01-01",
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-ATTR-001"},
        ],
    }
    bundle = {
        "resources": {
            "MedicationRequest": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "status": "active",
                        "authoredOn": "2026-04-30T08:00:00+00:00",
                        "medicationCodeableConcept": {
                            "coding": [
                                {
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "4603",
                                    "display": "Furosemide",
                                }
                            ]
                        },
                        "dosageInstruction": [],
                    }
                }
            ]
        }
    }
    return patient, bundle


def _llm_response(tool_input: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "produce_briefing"
    block.id = "toolu_test"
    block.input = tool_input

    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


def _populated_tool_input() -> dict[str, Any]:
    return {
        "patient_id": "pt-attr-001",
        "name": "Test Patient",
        "sections": [],
        "alerts": [],
        "generated_at": "2026-05-01T00:00:00+00:00",
    }


# ── Part 1 ────────────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
def test_medication_citation_value_contains_name() -> None:
    """Citation.value for a MedicationRequest must contain the medication name.

    Regression for the ``source_value_no_substring_match`` strip: when dose
    and route are empty, ``value`` must still hold the human-readable name
    so claims that cite the med name pass the substring check.
    """
    patient, bundle = _med_bundle()
    ctx = build(patient, bundle)

    assert len(ctx.active_medications) == 1
    citation = ctx.active_medications[0].citation
    assert citation.value != ""
    assert "furosemide" in citation.value.lower()

    # The verifier's substring check must succeed for a model claim that
    # copies the med name into source_value (case-insensitive).
    assert "Furosemide".lower() in citation.value.lower()


# ── Part 2 ────────────────────────────────────────────────────────────────────


@pytest.mark.hard_failure
def test_system_prompt_routes_blank_section_observations_to_alerts() -> None:
    """The system prompt must tell the model to put blank/absent-section
    observations in the alerts array, not inside sections[*].claims."""
    # Static check on the module-level constant — the prompt is embedded there.
    assert "alerts" in _SYSTEM
    assert "blank" in _SYSTEM.lower()
    # The exact directive must be present so the model has explicit guidance.
    assert "NOT inside sections" in _SYSTEM


@pytest.mark.hard_failure
def test_system_prompt_passed_to_anthropic_includes_blank_section_rule() -> None:
    """End-to-end: the system block sent to Anthropic must carry the rule."""
    patient, bundle = _med_bundle()
    ctx = build(patient, bundle)

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[_llm_response(_populated_tool_input())]
    )

    with patch("briefing.generator.anthropic.AsyncAnthropic", return_value=mock_client):
        result = _run(generate_briefing(ctx))

    assert isinstance(result, BriefingResponse)
    call = mock_client.messages.create.await_args
    system_blocks = call.kwargs["system"]
    assert isinstance(system_blocks, list) and system_blocks
    system_text = system_blocks[0]["text"]
    # Phrases drawn from the new rule — at least one must be present.
    assert "NOT inside sections" in system_text
    assert "alerts" in system_text.lower()


# Touch unused imports so linters do not complain on hypothetical future edits.
_ = (datetime, timezone)
