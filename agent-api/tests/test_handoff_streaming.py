"""Tests for the per-patient streaming handoff endpoint.

Covers:
  * ``handoff_chunk`` events arrive in completion order, not census order.
  * A per-patient failure emits an ``error`` event but does NOT abort the
    stream — sibling patients still emit ``handoff_chunk`` and a final
    ``done`` event arrives.
  * The existing ``generate_handoffs`` call site (no callback) is
    behaviourally unchanged.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from handoff.generator import HandoffSummary, generate_handoffs


def _summary(pid: str, *, error: str | None = None) -> HandoffSummary:
    return HandoffSummary(
        patient_id=pid,
        name=f"Patient {pid}",
        mrn=f"MRN-{pid}",
        triage_level=int(pid) if pid.isdigit() else 5,
        illness_severity="Stable",
        patient_summary="ok",
        action_list=[],
        situation_awareness="",
        contingency_plan="",
        generated_at=datetime.now(timezone.utc).isoformat(),
        error=error,
    )


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.hard_failure
def test_callback_fires_in_completion_order_not_census_order() -> None:
    """``on_patient_complete`` should be awaited as each task settles."""

    completion_order: list[str] = []
    fire_order: list[str] = []

    # patient-A finishes last, patient-C finishes first.
    delays = {"A": 0.30, "B": 0.15, "C": 0.05}

    async def _fake_generate_one(pid: str, *_a: Any, **_kw: Any) -> HandoffSummary:
        await asyncio.sleep(delays[pid])
        completion_order.append(pid)
        return _summary(pid)

    async def _on_complete(summary: HandoffSummary) -> None:
        fire_order.append(summary.patient_id)

    async def _exercise() -> list[HandoffSummary]:
        with patch("handoff.generator._generate_one", side_effect=_fake_generate_one), \
             patch("handoff.generator.anthropic.AsyncAnthropic", MagicMock()):
            return await generate_handoffs(
                patient_ids=["A", "B", "C"],
                on_patient_complete=_on_complete,
            )

    result = _run(_exercise())

    assert completion_order == ["C", "B", "A"]
    assert fire_order == ["C", "B", "A"], "callback must fire in completion order"
    # Aggregate result is still triage-sorted (A=int fails -> default 5; all 5).
    assert {s.patient_id for s in result} == {"A", "B", "C"}


@pytest.mark.hard_failure
def test_per_patient_failure_does_not_abort_stream() -> None:
    """A failing _generate_one should not prevent siblings from completing."""

    fire_log: list[tuple[str, str | None]] = []

    async def _fake_generate_one(pid: str, *_a: Any, **_kw: Any) -> HandoffSummary:
        await asyncio.sleep(0.01)
        if pid == "B":
            # _generate_one already swallows internal errors and returns an
            # error stub; mirror that contract here.
            return _summary(pid, error="LLM blew up")
        return _summary(pid)

    async def _on_complete(summary: HandoffSummary) -> None:
        fire_log.append((summary.patient_id, summary.error))

    async def _exercise() -> list[HandoffSummary]:
        with patch("handoff.generator._generate_one", side_effect=_fake_generate_one), \
             patch("handoff.generator.anthropic.AsyncAnthropic", MagicMock()):
            return await generate_handoffs(
                patient_ids=["A", "B", "C"],
                on_patient_complete=_on_complete,
            )

    result = _run(_exercise())

    pids_seen = {pid for pid, _ in fire_log}
    assert pids_seen == {"A", "B", "C"}, "all patients must emit a callback"
    failed = [pid for pid, err in fire_log if err is not None]
    assert failed == ["B"]
    assert len(result) == 3


@pytest.mark.hard_failure
def test_no_callback_path_is_unchanged() -> None:
    """Calling ``generate_handoffs`` without a callback must work as before."""

    async def _fake_generate_one(pid: str, *_a: Any, **_kw: Any) -> HandoffSummary:
        return _summary(pid)

    async def _exercise() -> list[HandoffSummary]:
        with patch("handoff.generator._generate_one", side_effect=_fake_generate_one), \
             patch("handoff.generator.anthropic.AsyncAnthropic", MagicMock()):
            return await generate_handoffs(patient_ids=["1", "2", "3"])

    result = _run(_exercise())
    assert {s.patient_id for s in result} == {"1", "2", "3"}
    assert all(s.error is None for s in result)


@pytest.mark.hard_failure
def test_sse_endpoint_emits_chunks_and_done(tmp_path: Path) -> None:
    """End-to-end: POST /handoff/generate/stream emits SSE chunks + done."""

    from fastapi.testclient import TestClient

    async def _fake_generate_one(pid: str, *_a: Any, **_kw: Any) -> HandoffSummary:
        # Stagger so completion order is C, B, A.
        delays = {"A": 0.20, "B": 0.10, "C": 0.02}
        await asyncio.sleep(delays.get(pid, 0.01))
        if pid == "B":
            return _summary(pid, error="boom")
        return _summary(pid)

    async def _noop_init(self: Any) -> None:
        return None

    with patch("handoff.generator._generate_one", side_effect=_fake_generate_one), \
         patch("handoff.generator.anthropic.AsyncAnthropic", MagicMock()), \
         patch("checkpointer.sqlite_saver.SqliteSaver.init", _noop_init):
        # Import after patching so the route uses the patched module-level
        # symbol via the local import inside the handler.
        from main import app

        with TestClient(app) as client:
            resp = client.post(
                "/handoff/generate/stream",
                json={"patient_ids": ["A", "B", "C"]},
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            text = resp.text

    # Parse SSE frames.
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in text.strip().split("\n\n"):
        lines = frame.splitlines()
        ev = next((line[len("event: "):] for line in lines if line.startswith("event: ")), "")
        data_line = next((line[len("data: "):] for line in lines if line.startswith("data: ")), "{}")
        events.append((ev, json.loads(data_line)))

    names = [e[0] for e in events]
    assert names[-1] == "done"
    assert "handoff_chunk" in names
    assert "error" in names

    chunk_pids = [data["patient_id"] for ev, data in events if ev == "handoff_chunk"]
    error_pids = [data["patient_id"] for ev, data in events if ev == "error"]
    assert "B" in error_pids
    assert set(chunk_pids) | set(error_pids) == {"A", "B", "C"}

    done_payload = events[-1][1]
    assert done_payload["total"] == 3
    assert done_payload["succeeded"] == 2
    assert done_payload["failed"] == 1
    assert "duration_ms" in done_payload


@pytest.mark.hard_failure
def test_sse_frame_format_is_well_formed() -> None:
    """Every emitted frame must end with the SSE-required blank line (``\\n\\n``).

    Regression guard for the most common SSE bug: ``data: {...}\\n`` (single
    newline) does not trigger the EventSource / fetch-stream parser on the
    client. The frame separator MUST be ``\\n\\n``. We assert at the byte
    level rather than parsing back into events, so a future change to
    ``_sse_format`` that drops a newline fails loudly.
    """

    from fastapi.testclient import TestClient

    async def _fake_generate_one(pid: str, *_a: Any, **_kw: Any) -> HandoffSummary:
        await asyncio.sleep(0.005)
        return _summary(pid)

    async def _noop_init(self: Any) -> None:
        return None

    with patch("handoff.generator._generate_one", side_effect=_fake_generate_one), \
         patch("handoff.generator.anthropic.AsyncAnthropic", MagicMock()), \
         patch("checkpointer.sqlite_saver.SqliteSaver.init", _noop_init):
        from main import app

        with TestClient(app) as client:
            resp = client.post(
                "/handoff/generate/stream",
                json={"patient_ids": ["A", "B", "C"]},
            )
            assert resp.status_code == 200
            text = resp.text

    # Stream must end with a blank-line terminator on the final frame too.
    assert text.endswith("\n\n"), "final SSE frame missing terminating blank line"
    # Each non-trailing frame must contain exactly one event line and one data
    # line, with no orphan ``data:`` fragments outside a frame.
    frames = [f for f in text.split("\n\n") if f]
    assert len(frames) >= 4  # 3 chunks + 1 done
    for frame in frames:
        lines = frame.split("\n")
        assert any(line.startswith("event: ") for line in lines), \
            f"frame missing event line: {frame!r}"
        assert any(line.startswith("data: ") for line in lines), \
            f"frame missing data line: {frame!r}"
