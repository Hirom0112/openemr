"""Per-case log-capture isolation under ``asyncio.gather``.

Pre-Wave-2-parallelizer the eval runner attached a fresh
``_RecordCaptureHandler`` per ``run_case`` call and snapshotted
``handler.records`` into ``RunOutcome.captured_logs``. With
``asyncio.gather`` running N cases concurrently, multiple ``run_case``
invocations now overlap — each one's captured_logs would include records
emitted by sibling concurrent cases, false-positive-failing the
``no_phi_in_logs`` rubric whenever any sibling carried synthetic PHI.

The fix routes records through a ``contextvars.ContextVar`` whose value
is a per-task list. asyncio tasks inherit the parent context but each
task gets its own copy, so concurrent ``run_case`` invocations write to
disjoint lists.

This test reproduces the bug deterministically: two ``run_case`` calls
run concurrently via ``asyncio.gather``; each emits a logger record with
a UNIQUE marker token. The contract: case A's ``captured_logs`` must
contain ONLY case A's marker (and vice versa). Pre-fix, both lists
contain both markers.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from evals.runner import run_case

pytestmark = pytest.mark.hard_failure


@dataclass
class _StubCase:
    case_id: str
    bucket: str = "schema"
    fixture_key: str = "_unused"
    chart_patient: dict = None  # type: ignore[assignment]
    doc_type_hint: str | None = None

    def __post_init__(self) -> None:
        if self.chart_patient is None:
            self.chart_patient = {"id": "p-1"}


def _make_stub_factory(marker: str, ready: asyncio.Event, release: asyncio.Event):
    """Compile-graph factory that emits a marker log line and yields control.

    The factory returns a fake ``compiled`` object whose ``ainvoke`` coroutine:
      1) emits ``logger.info(marker)`` on a captured logger,
      2) signals ``ready``,
      3) awaits ``release`` so both concurrent tasks overlap their
         capture window before either completes,
      4) returns a minimal final-state dict.
    """

    def _factory(*, file_bytes_provider, fhir_patient_provider):  # noqa: ARG001
        class _Compiled:
            async def ainvoke(self, initial, config=None):  # noqa: ARG002
                # Emit on a logger covered by _GRAPH_LOGGERS so the global
                # capture handler routes it through the ContextVar.
                logging.getLogger("graph.nodes.critic").info(
                    "stub_case_marker",
                    extra={"marker": marker},
                )
                ready.set()
                # Hold the capture window open until the sibling task has
                # also emitted its record. Pre-fix, this is precisely when
                # cross-contamination occurs.
                await release.wait()
                return {
                    "extraction": {"kind": "unknown"},
                    "critic_decision": "pass",
                    "critic_violations": [],
                    "soft_warns": [],
                    "ocr_layout": [],
                }

        return _Compiled()

    return _factory


def _make_initial_state_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``graph.make_initial_state`` to a trivial stub.

    The runner imports it lazily inside ``run_case``. We use ``monkeypatch``
    so any insertions into ``sys.modules`` and module attribute changes are
    rolled back at fixture teardown — leaving the test session free of a
    half-stubbed ``graph`` module that would poison subsequent tests.
    """
    import sys
    import types

    created_here = "graph" not in sys.modules
    if created_here:
        mod = types.ModuleType("graph")
        monkeypatch.setitem(sys.modules, "graph", mod)
    else:
        mod = sys.modules["graph"]

    def _fake_make_initial_state(**kwargs):
        return dict(kwargs)

    monkeypatch.setattr(mod, "make_initial_state", _fake_make_initial_state, raising=False)
    # Also stub ``compile_graph`` to satisfy the runner's lazy import path
    # (we override via the ``compile_graph_factory`` arg, but the import
    # statement still resolves the name on the module).
    monkeypatch.setattr(mod, "compile_graph", lambda **_kw: None, raising=False)


@pytest.mark.asyncio
async def test_concurrent_run_case_log_capture_is_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_initial_state_patch(monkeypatch)

    # Patch ``resolve_fixture_path`` so we don't touch the real fixture
    # generator. The stub factory never reads bytes, so the path can be
    # anything that exists.
    fixture_file = tmp_path / "stub.pdf"
    fixture_file.write_bytes(b"%PDF-stub")
    monkeypatch.setattr(
        "evals.runner.resolve_fixture_path",
        lambda key, root: fixture_file,
    )

    ready_a = asyncio.Event()
    ready_b = asyncio.Event()
    release = asyncio.Event()

    factory_a = _make_stub_factory("MARKER_AAA_uniqueA", ready_a, release)
    factory_b = _make_stub_factory("MARKER_BBB_uniqueB", ready_b, release)

    case_a = _StubCase(case_id="case-A")
    case_b = _StubCase(case_id="case-B")

    # Drive both cases through asyncio.gather. Once both have emitted
    # their marker (ready_a + ready_b set), release them so they finalize.
    async def _gate() -> None:
        await ready_a.wait()
        await ready_b.wait()
        release.set()

    task_a = asyncio.create_task(
        run_case(case_a, fixtures_root=tmp_path, compile_graph_factory=factory_a)
    )
    task_b = asyncio.create_task(
        run_case(case_b, fixtures_root=tmp_path, compile_graph_factory=factory_b)
    )
    gate = asyncio.create_task(_gate())

    outcome_a, outcome_b, _ = await asyncio.gather(task_a, task_b, gate)

    a_messages = [
        (r["message"], r.get("extra", {}).get("marker"))
        for r in outcome_a.captured_logs
    ]
    b_messages = [
        (r["message"], r.get("extra", {}).get("marker"))
        for r in outcome_b.captured_logs
    ]

    a_markers = {m for _msg, m in a_messages if m}
    b_markers = {m for _msg, m in b_messages if m}

    # Each case must see its own marker.
    assert "MARKER_AAA_uniqueA" in a_markers, (
        f"case-A lost its own marker; captured_logs={a_messages}"
    )
    assert "MARKER_BBB_uniqueB" in b_markers, (
        f"case-B lost its own marker; captured_logs={b_messages}"
    )

    # And — the contract this test exists to enforce — must NOT see the
    # sibling's marker. Pre-fix this fails because both cases share the
    # same handler.records list.
    assert "MARKER_BBB_uniqueB" not in a_markers, (
        "cross-contamination: case-A's captured_logs contains case-B's marker. "
        f"a_markers={a_markers}"
    )
    assert "MARKER_AAA_uniqueA" not in b_markers, (
        "cross-contamination: case-B's captured_logs contains case-A's marker. "
        f"b_markers={b_markers}"
    )
