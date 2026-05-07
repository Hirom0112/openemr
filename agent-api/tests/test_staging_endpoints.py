"""Endpoint tests for the Slice 9.3 pending-extractions router."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests._staging_fakes import make_fake_pool

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def state(monkeypatch: pytest.MonkeyPatch):
    pool, st = make_fake_pool()

    async def _fake_get_pool() -> Any:
        return pool

    async def _fake_emit(event: Any) -> None:
        st.audit_events.append(
            {
                "event_type": event.event_type,
                "outcome": event.outcome,
                "detail_json": dict(event.detail_json or {}),
            }
        )

    from audit import writer as audit_writer

    monkeypatch.setattr(audit_writer, "get_pool", _fake_get_pool)
    monkeypatch.setattr(audit_writer, "emit", _fake_emit)

    # Stub the writer's HTTP call so approve→write transitions are deterministic.
    from observations import writer as obs_writer

    async def _fake_perform_write(row: dict[str, Any]):
        return ("written", None)

    monkeypatch.setattr(obs_writer, "_perform_write", _fake_perform_write)

    return st


@pytest.fixture()
def client(state) -> TestClient:
    import main

    return TestClient(main.app)


def _stage(*, target_id: str = "copilot-1-2089-1", patient_id: str = "p-1") -> int:
    from staging import store

    return asyncio.get_event_loop().run_until_complete(
        store.stage_pending(
            document_reference_id="copilot:1",
            file_batch_id="00000000-0000-0000-0000-000000000001",
            patient_id=patient_id,
            source_format="hl7",
            target_resource_type="Observation",
            target_resource_id=target_id,
            payload={
                "id": target_id,
                "resourceType": "Observation",
                "subject": {"reference": f"Patient/{patient_id}"},
            },
        )
    )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_list_endpoint_returns_patient_scoped_rows(client: TestClient) -> None:
    pid_a = _stage(target_id="copilot-1-a", patient_id="p-1")
    _stage(target_id="copilot-1-b", patient_id="p-2")

    resp = client.get("/pending-extractions", params={"patient_id": "p-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["patient_id"] == "p-1"
    ids = {r["id"] for r in body["rows"]}
    assert pid_a in ids
    assert all(r["patient_id"] == "p-1" for r in body["rows"])


def test_list_requires_patient_id(client: TestClient) -> None:
    resp = client.get("/pending-extractions")
    assert resp.status_code == 422  # FastAPI validation


def test_get_one_returns_full_row(client: TestClient) -> None:
    pid = _stage(target_id="copilot-1-c")
    resp = client.get(f"/pending-extractions/{pid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == pid
    assert body["payload"]["resourceType"] == "Observation"


def test_get_one_404(client: TestClient) -> None:
    resp = client.get("/pending-extractions/999999")
    assert resp.status_code == 404


def test_approve_happy_path_returns_written(client: TestClient) -> None:
    pid = _stage(target_id="copilot-1-d")
    resp = client.post(f"/pending-extractions/{pid}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "written"
    assert body["write_error"] is None


def test_approve_wrong_state_409(client: TestClient) -> None:
    pid = _stage(target_id="copilot-1-e")
    # First approve drives it to written; second approve is illegal.
    client.post(f"/pending-extractions/{pid}/approve")
    resp = client.post(f"/pending-extractions/{pid}/approve")
    assert resp.status_code == 409


def test_approve_writer_failure_returns_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _stage(target_id="copilot-1-f")
    from observations import writer as obs_writer

    async def _fail(row: dict[str, Any]):
        return ("failed", "php_5xx")

    monkeypatch.setattr(obs_writer, "_perform_write", _fail)

    resp = client.post(f"/pending-extractions/{pid}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "failed"
    assert body["write_error"] == "php_5xx"


def test_batch_approve_best_effort(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_a = _stage(target_id="copilot-1-g")
    pid_b = _stage(target_id="copilot-1-h")
    pid_c = 99999  # nonexistent

    from observations import writer as obs_writer

    async def _per_row(row: dict[str, Any]):
        if row["target_resource_id"].endswith("-h"):
            return ("failed", "network_timeout")
        return ("written", None)

    monkeypatch.setattr(obs_writer, "_perform_write", _per_row)

    resp = client.post(
        "/pending-extractions/batch-approve",
        json={"ids": [pid_a, pid_b, pid_c]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["n_approved"] == 1
    assert body["n_failed"] == 2
    by_id = {r["pending_id"]: r for r in body["results"]}
    assert by_id[pid_a]["state"] == "written"
    assert by_id[pid_b]["state"] == "failed"
    assert by_id[pid_c]["error"]


def test_reject_endpoint_succeeds(client: TestClient) -> None:
    pid = _stage(target_id="copilot-1-i")
    resp = client.post(
        f"/pending-extractions/{pid}/reject",
        json={"reason": "wrong patient"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "rejected"


def test_reject_reason_too_long_400(client: TestClient) -> None:
    pid = _stage(target_id="copilot-1-j")
    resp = client.post(
        f"/pending-extractions/{pid}/reject",
        json={"reason": "x" * 300},
    )
    # Pydantic will catch >256-char reasons at model validation → 422.
    assert resp.status_code == 422


def test_reject_already_decided_409(client: TestClient) -> None:
    pid = _stage(target_id="copilot-1-k")
    client.post(f"/pending-extractions/{pid}/approve")
    resp = client.post(
        f"/pending-extractions/{pid}/reject",
        json={"reason": "too late"},
    )
    assert resp.status_code == 409


def test_retry_endpoint_from_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _stage(target_id="copilot-1-l")

    from observations import writer as obs_writer

    calls = {"n": 0}

    async def _toggle(row: dict[str, Any]):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("failed", "network_timeout")
        return ("written", None)

    monkeypatch.setattr(obs_writer, "_perform_write", _toggle)

    # First approve fails.
    r1 = client.post(f"/pending-extractions/{pid}/approve")
    assert r1.json()["state"] == "failed"

    # Retry succeeds.
    r2 = client.post(f"/pending-extractions/{pid}/retry")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["state"] == "written"
    assert body["retry_count"] == 1


def test_retry_from_pending_409(client: TestClient) -> None:
    pid = _stage(target_id="copilot-1-m")
    resp = client.post(f"/pending-extractions/{pid}/retry")
    assert resp.status_code == 409


def test_batch_approve_input_cap(client: TestClient) -> None:
    """201 ids should be rejected at body validation."""
    resp = client.post(
        "/pending-extractions/batch-approve",
        json={"ids": list(range(1, 202))},
    )
    assert resp.status_code == 422
