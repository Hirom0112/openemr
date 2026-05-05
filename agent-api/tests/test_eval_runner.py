"""Tests for evals.runner.run_case — graph patched, no live API."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from evals import runner
from evals.runner import RunOutcome, run_case

pytestmark = pytest.mark.hard_failure


@dataclass
class _StubCase:
    case_id: str = "stub-1"
    bucket: str = "nominal_lab"
    fixture_key: str = "stub_fixture"
    doc_type_hint: str | None = None
    chart_patient: dict | None = None
    expected_kind: str = "lab_report"
    expected_critic_decision: str = "pass"
    expected_violation_codes: Tuple[str, ...] = ()
    expected_softwarn_codes: Tuple[str, ...] = ()
    expected_field_assertions: Tuple = ()
    notes: str = ""


@pytest.mark.asyncio
async def test_run_case_executes_graph(tmp_path: Path) -> None:
    """run_case wires providers into compile_graph and returns a RunOutcome."""
    # Stub fixture file so the file_bytes_provider has something to read.
    fixture = tmp_path / "stub_fixture.pdf"
    fixture.write_bytes(b"%PDF-fake")

    # Reset cached fixture index so flat fallback is used.
    runner._FIXTURE_INDEX_CACHE = None

    final_state = {
        "extraction": {"kind": "lab_report", "values": []},
        "critic_decision": "pass",
        "critic_violations": [],
        "soft_warns": [],
    }

    compiled = MagicMock()
    compiled.ainvoke = AsyncMock(return_value=final_state)

    captured_kwargs: dict = {}

    def _factory(**kwargs):
        captured_kwargs.update(kwargs)
        return compiled

    case = _StubCase(chart_patient={"id": "pt-99"})

    # Emit a log record during the "run" so we can verify capture works.
    async def _ainvoke(initial, config):
        logging.getLogger("graph").info("graph_test_event", extra={"duration_ms": 7})
        return final_state

    compiled.ainvoke = _ainvoke  # type: ignore[assignment]

    outcome = await run_case(
        case,
        fixtures_root=tmp_path,
        compile_graph_factory=_factory,
    )

    assert isinstance(outcome, RunOutcome)
    assert outcome.case_id == "stub-1"
    assert outcome.extraction == {"kind": "lab_report", "values": []}
    assert outcome.critic_decision == "pass"
    assert outcome.error is None

    # Providers were wired in.
    assert "file_bytes_provider" in captured_kwargs
    assert "fhir_patient_provider" in captured_kwargs
    fhir_provider = captured_kwargs["fhir_patient_provider"]
    fbp = captured_kwargs["file_bytes_provider"]
    # File-bytes provider reads the resolved fixture.
    assert (await fbp("any-ref")) == b"%PDF-fake"
    # FHIR provider returns the case's chart_patient dict.
    assert (await fhir_provider("any-pid")) == {"id": "pt-99"}

    # The capture handler should have grabbed at least the test event.
    messages = [r["message"] for r in outcome.captured_logs]
    assert any("graph_test_event" in m for m in messages)


@pytest.mark.asyncio
async def test_run_case_surfaces_error_on_graph_failure(tmp_path: Path) -> None:
    runner._FIXTURE_INDEX_CACHE = None
    fixture = tmp_path / "stub_fixture.pdf"
    fixture.write_bytes(b"%PDF-fake")

    def _factory(**kwargs):  # noqa: ARG001
        raise RuntimeError("boom")

    case = _StubCase(chart_patient={"id": "pt-99"})
    outcome = await run_case(
        case, fixtures_root=tmp_path, compile_graph_factory=_factory
    )
    assert outcome.error == "RuntimeError"
    assert outcome.critic_decision is None
