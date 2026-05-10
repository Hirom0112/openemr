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


@pytest.mark.asyncio
async def test_run_case_captures_audit_rows_emitted_during_graph(
    tmp_path: Path,
) -> None:
    """Phase 1.2 — every AuditEvent passed to ``audit_writer.emit`` during a
    run lands on ``RunOutcome.audit_rows`` in normalized dict shape so the
    multimodal mechanical rubrics can fire substantively.
    """
    from audit.models import AuditEvent
    from audit import writer as audit_writer

    runner._FIXTURE_INDEX_CACHE = None
    fixture = tmp_path / "stub_fixture.pdf"
    fixture.write_bytes(b"%PDF-fake")

    async def _ainvoke(initial, config):
        # Mimic the staging pipeline emitting a quarantine audit row mid-run.
        await audit_writer.emit(
            AuditEvent(
                event_type="document_quarantined",
                request_id="req-42",
                detail_json={"reason": "identity_mismatch", "doc_id": "abc"},
            )
        )
        return {
            "extraction": {"kind": "lab_report", "values": []},
            "critic_decision": "pass",
            "critic_violations": [],
            "soft_warns": [],
        }

    compiled = MagicMock()
    compiled.ainvoke = _ainvoke  # type: ignore[assignment]

    def _factory(**kwargs):  # noqa: ARG001
        return compiled

    case = _StubCase(chart_patient={"id": "pt-99"})
    outcome = await run_case(
        case,
        fixtures_root=tmp_path,
        compile_graph_factory=_factory,
    )

    assert outcome.audit_rows is not None
    assert len(outcome.audit_rows) >= 1
    quarantine = [r for r in outcome.audit_rows if r.get("event") == "document_quarantined"]
    assert len(quarantine) == 1
    assert quarantine[0]["detail_json"]["reason"] == "identity_mismatch"
    # Both ``event`` and ``event_type`` keys are exposed for rubric compat.
    assert quarantine[0]["event_type"] == "document_quarantined"


@pytest.mark.asyncio
async def test_run_case_audit_capture_restored_after_run(tmp_path: Path) -> None:
    """The patch must restore the original ``audit_writer.emit`` after each
    run — leaving a stale capture in place would cross-contaminate
    subsequent eval runs (and any other in-process audit emissions)."""
    from audit import writer as audit_writer

    runner._FIXTURE_INDEX_CACHE = None
    (tmp_path / "stub_fixture.pdf").write_bytes(b"%PDF-fake")

    original = audit_writer.emit

    async def _ainvoke(initial, config):
        return {
            "extraction": {"kind": "lab_report", "values": []},
            "critic_decision": "pass",
            "critic_violations": [],
            "soft_warns": [],
        }

    compiled = MagicMock()
    compiled.ainvoke = _ainvoke  # type: ignore[assignment]

    case = _StubCase(chart_patient={"id": "pt-99"})
    await run_case(
        case,
        fixtures_root=tmp_path,
        compile_graph_factory=lambda **_kw: compiled,
    )
    assert audit_writer.emit is original


@pytest.mark.asyncio
async def test_run_case_propagates_writer_state_when_present(tmp_path: Path) -> None:
    """When the graph state carries staged_observations / pending_extractions
    / written_observation_ids / written_condition_ids (forward-compat for an
    approval-flow runner), ``RunOutcome`` exposes them verbatim. The current
    extraction-only graph does not produce these keys, so they default to
    None — the rubrics' vacuous-True branch handles that.
    """
    runner._FIXTURE_INDEX_CACHE = None
    (tmp_path / "stub_fixture.pdf").write_bytes(b"%PDF-fake")

    async def _ainvoke(initial, config):
        return {
            "extraction": {"kind": "lab_report", "values": []},
            "critic_decision": "pass",
            "critic_violations": [],
            "soft_warns": [],
            "staged_observations": [{"valueString": "4.2"}],
            "pending_extractions": [{"observation_id": "obs-1", "state": "written"}],
            "written_observation_ids": ["obs-1"],
            "written_condition_ids": ["copilot-doc-1-I10"],
        }

    compiled = MagicMock()
    compiled.ainvoke = _ainvoke  # type: ignore[assignment]

    case = _StubCase(chart_patient={"id": "pt-99"})
    outcome = await run_case(
        case,
        fixtures_root=tmp_path,
        compile_graph_factory=lambda **_kw: compiled,
    )

    assert outcome.staged_observations == [{"valueString": "4.2"}]
    assert outcome.pending_extractions == [{"observation_id": "obs-1", "state": "written"}]
    assert outcome.written_observation_ids == ["obs-1"]
    assert outcome.written_condition_ids == ["copilot-doc-1-I10"]


def test_per_modality_breakdown_includes_bbox_gt_cases(monkeypatch, tmp_path) -> None:
    """Phase 4b regression — per-modality counters must include bbox_gt cases.

    Prior to the fix, the ``main()`` assembly path called
    ``_per_modality_breakdown`` with a ``legacy_*`` list that filtered out
    the ``bbox_gt`` bucket. That silently excluded 12 table_heavy bbox_gt
    cases and all 12 photo_capture bbox_gt cases from the per-modality
    ``n_cases`` / ``schema_valid`` / etc tallies — only their citation_iou
    rates appeared in the output via a separate overlay path
    (``_score_bbox_rubrics``), giving a misleading "n=11 / no entry" view.

    This test drives ``main()`` end-to-end with a synthetic mix of legacy
    table_heavy + bbox_gt table_heavy + bbox_gt photo_capture cases and
    asserts that the resulting JSON's per_modality block contains
    ``n_cases`` for both modalities including the bbox_gt cases.
    """
    import json
    from evals import run_full_suite
    from evals.scoring import CaseScore
    from evals.runner import RunOutcome

    def _mk_case(case_id: str, bucket: str, modality: str):
        return type(
            "C",
            (),
            {
                "case_id": case_id,
                "bucket": bucket,
                "document_modality": modality,
                "fixture_key": case_id,
                "expected_critic_decision": "pass",
            },
        )()

    cases = (
        [_mk_case(f"legacy_th_{i}", "lab_nominal", "table_heavy") for i in range(11)]
        + [_mk_case(f"bbox_th_{i}", "bbox_gt", "table_heavy") for i in range(12)]
        + [_mk_case(f"bbox_pc_{i}", "bbox_gt", "photo_capture") for i in range(12)]
    )

    def _mk_score(case_id: str) -> CaseScore:
        return CaseScore(
            case_id=case_id,
            schema_valid=True,
            citation_present=True,
            correct_critic_decision=True,
            factually_consistent=True,
            safe_refusal=True,
            no_phi_in_logs=True,
            is_critic_false_positive=False,
        )

    async def _fake_run_case(case, *, fixtures_root, **kw):
        return RunOutcome(
            case_id=case.case_id,
            extraction={"kind": "lab_report", "values": []},
            critic_decision="pass",
            critic_violations=[],
            soft_warns=[],
        )

    async def _fake_score_case(case, outcome):
        return _mk_score(case.case_id)

    # Stub fixtures module so _run_async picks up our case list.
    import tests.fixtures.w2_eval_cases as wcases

    monkeypatch.setattr(wcases, "CASES", cases)
    monkeypatch.setattr("evals.runner.run_case", _fake_run_case)
    monkeypatch.setattr("evals.scoring.score_case", _fake_score_case)

    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    fixtures_root = tmp_path / "fixtures"
    fixtures_root.mkdir()

    rc = run_full_suite.main([
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(fixtures_root),
        "--batch-size", "8",
        "--cache", "off",
    ])
    assert rc == 0

    payload = json.loads(out_json.read_text())
    pm = payload.get("per_modality", {})
    assert "table_heavy" in pm, f"table_heavy missing from per_modality: {list(pm)}"
    assert pm["table_heavy"].get("n_cases") == 23, (
        "Expected table_heavy n_cases=23 (11 legacy + 12 bbox_gt). "
        f"Got {pm['table_heavy'].get('n_cases')!r}. "
        "Indicates _per_modality_breakdown was called with the legacy_* filter."
    )
    assert "photo_capture" in pm, f"photo_capture missing entirely from per_modality: {list(pm)}"
    assert pm["photo_capture"].get("n_cases") == 12, (
        "Expected photo_capture n_cases=12 (all bbox_gt). "
        f"Got {pm['photo_capture'].get('n_cases')!r}."
    )
    # bbox_gt cases also contribute to schema_valid / etc — confirm they're
    # not silently zeroed out.
    assert pm["photo_capture"].get("schema_valid") == 1.0
    assert pm["table_heavy"].get("schema_valid") == 1.0


# --------------------------------------------------------------------------- #
# Phase 4.6 — _AuditCaptureScope concurrency regression
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_audit_capture_scope_isolates_concurrent_emits() -> None:
    """Concurrent ``_AuditCaptureScope`` instances must not bleed events.

    Phase 4.5 root cause: the original ``_AuditCapturePatch`` swapped
    ``audit.writer.emit`` at the module level, so two concurrent ``run_case``
    invocations under ``asyncio.gather`` overlapped on that attribute and
    saw each other's events. The Phase 4.6 reshape replaces the swap with a
    ContextVar sink + a single permanent dispatcher (mirrors
    ``_case_log_records``).

    This test simulates 4 concurrent eval-style scopes, each emitting its
    own labelled event, and asserts that every scope's ``rows`` contains
    ONLY events tagged with its own case_id. With the old patch this test
    fails (events bled across scopes).
    """
    import asyncio

    from audit import writer as audit_writer
    from audit.models import AuditEvent
    from evals.runner import _AuditCaptureScope

    case_ids = [f"concurrent-case-{i}" for i in range(4)]
    # Per-case event count is staggered so we can detect cross-talk by
    # both label AND count.
    events_per_case = {cid: (idx + 1) * 3 for idx, cid in enumerate(case_ids)}

    async def _run_one(case_id: str) -> list[dict]:
        with _AuditCaptureScope() as cap:
            # Yield to let other tasks interleave their __enter__ calls
            # before we start emitting — maximises chance of cross-talk
            # under the old module-attribute-swap implementation.
            await asyncio.sleep(0)
            for i in range(events_per_case[case_id]):
                await audit_writer.emit(
                    AuditEvent(
                        event_type="phase46_test_event",
                        request_id=f"{case_id}-req-{i}",
                        detail_json={"case_id": case_id, "seq": i},
                    )
                )
                # Re-yield between emits so other scopes get to run.
                await asyncio.sleep(0)
            return list(cap.rows)

    # All 4 scopes run concurrently — the failure mode under the old
    # implementation is exactly this concurrency pattern.
    results = await asyncio.gather(*[_run_one(cid) for cid in case_ids])

    # Each scope must see only its own events, in order, with the
    # expected count.
    for case_id, rows in zip(case_ids, results):
        expected_n = events_per_case[case_id]
        assert len(rows) == expected_n, (
            f"Case {case_id!r} captured {len(rows)} rows, expected {expected_n}. "
            f"Cross-talk likely. All rows: {rows!r}"
        )
        for i, row in enumerate(rows):
            seen_case_id = (row.get("detail_json") or {}).get("case_id")
            seen_seq = (row.get("detail_json") or {}).get("seq")
            assert seen_case_id == case_id, (
                f"Case {case_id!r} row {i} carries case_id={seen_case_id!r} "
                "— scope sink leaked across asyncio tasks."
            )
            assert seen_seq == i, (
                f"Case {case_id!r} row {i} has seq={seen_seq!r}; "
                "ordering / cross-talk regression."
            )


@pytest.mark.asyncio
async def test_audit_capture_scope_does_not_leak_after_exit() -> None:
    """After ``__exit__`` the ContextVar sink is reset and post-scope emits
    do not accumulate into the captured rows."""
    from audit import writer as audit_writer
    from audit.models import AuditEvent
    from evals.runner import _AuditCaptureScope, _case_audit_rows

    with _AuditCaptureScope() as cap:
        await audit_writer.emit(AuditEvent(event_type="inside_scope"))
        assert len(cap.rows) == 1
    # After exit, the ContextVar should be back to None and a follow-up
    # emit must not land in cap.rows.
    assert _case_audit_rows.get() is None
    await audit_writer.emit(AuditEvent(event_type="after_scope"))
    assert len(cap.rows) == 1, "Event leaked into closed scope"
