"""Endpoint tests for the Slice 9.2 quarantine queue.

Uses the FastAPI TestClient with the asyncpg pool monkeypatched to a
fully in-memory fake. We test the HTTP surface only — DB-level claim TTL
arithmetic is exercised via a synthesised "claim_expires_at < NOW()" row.

The tests share one fake pool per module and reset it in a fixture so
parallel tests don't poison each other.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


# ── In-memory fake asyncpg pool ──────────────────────────────────────────────


class _FakeRow(dict):
    """Acts like an asyncpg Record (has .keys() + __getitem__)."""

    def keys(self):  # type: ignore[override]
        return list(super().keys())


class _FakeConn:
    def __init__(self, store: "_FakeStore") -> None:
        self.store = store

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        return self.store.execute_fetchrow(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRow]:
        return self.store.execute_fetch(sql, args)

    async def execute(self, sql: str, *args: Any) -> None:
        self.store.execute_fetchrow(sql, args)


class _AsyncCM:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakePool:
    def __init__(self, store: "_FakeStore") -> None:
        self.store = store

    def acquire(self) -> _AsyncCM:
        return _AsyncCM(_FakeConn(self.store))


class _FakeStore:
    """Tiny in-memory replacement for the quarantine table.

    Implements just the SQL shapes the resolver / quarantine module fire.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone.utc)

    def execute_fetchrow(self, sql: str, args: tuple[Any, ...]) -> _FakeRow | None:
        s = sql.strip().upper()
        if s.startswith("INSERT INTO COPILOT_QUARANTINED_DOCUMENTS"):
            (
                document_reference_id,
                file_batch_id,
                panel_id,
                parsed_text,
                candidates_text,
                reason_code,
            ) = args
            qid = str(uuid.uuid4())
            now = self._now()
            row = {
                "quarantine_id": qid,
                "document_reference_id": document_reference_id,
                "file_batch_id": file_batch_id,
                "panel_id": panel_id,
                "parsed_identity": json.loads(parsed_text)
                if isinstance(parsed_text, str)
                else parsed_text,
                "candidate_matches": json.loads(candidates_text)
                if isinstance(candidates_text, str)
                else candidates_text,
                "reason_code": reason_code,
                "state": "unclaimed",
                "claimed_by": None,
                "claimed_at": None,
                "claim_expires_at": None,
                "resolved_patient_id": None,
                "resolved_at": None,
                "resolved_by": None,
                "rejected_reason": None,
                "quarantined_at": now,
                "expires_at": now + _dt.timedelta(hours=36),
            }
            self.rows[qid] = row
            return _FakeRow(quarantine_id=qid, expires_at=row["expires_at"])

        if "UPDATE COPILOT_QUARANTINED_DOCUMENTS" in s and "STATE" in s:
            # claim / match / reject — branch on the SET clause.
            set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
            if "CLAIMED_BY" in set_clause.upper() and "RESOLVED_PATIENT_ID" not in set_clause.upper():
                # claim
                qid, claimer, ttl = args
                row = self.rows.get(qid)
                if row is None:
                    return None
                now = self._now()
                claim_ok = (
                    row["state"] == "unclaimed"
                    or row["claim_expires_at"] is None
                    or row["claim_expires_at"] < now
                )
                if not claim_ok:
                    return None
                row["state"] = "claimed"
                row["claimed_by"] = claimer
                row["claimed_at"] = now
                row["claim_expires_at"] = now + _dt.timedelta(seconds=int(ttl))
                return _FakeRow(
                    quarantine_id=qid,
                    state="claimed",
                    claim_expires_at=row["claim_expires_at"],
                    prev_state_unused="unclaimed",
                )
            if "RESOLVED_PATIENT_ID" in set_clause.upper():
                # match
                qid, target, claimer = args
                row = self.rows.get(qid)
                if row is None:
                    return None
                now = self._now()
                if (
                    row["state"] != "claimed"
                    or row["claimed_by"] != claimer
                    or row["claim_expires_at"] is None
                    or row["claim_expires_at"] <= now
                ):
                    return None
                row["state"] = "matched"
                row["resolved_patient_id"] = target
                row["resolved_at"] = now
                row["resolved_by"] = claimer
                return _FakeRow(quarantine_id=qid, resolved_patient_id=target)
            if "REJECTED_REASON" in set_clause.upper():
                qid, reason, claimer = args
                row = self.rows.get(qid)
                if row is None:
                    return None
                # Determine if this is the clinician variant (requires claim).
                where = sql.split("WHERE", 1)[1].upper()
                requires_claim = "CLAIM_EXPIRES_AT" in where
                now = self._now()
                if requires_claim:
                    if (
                        row["state"] != "claimed"
                        or row["claimed_by"] != claimer
                        or row["claim_expires_at"] is None
                        or row["claim_expires_at"] <= now
                    ):
                        return None
                else:
                    if row["state"] not in {"unclaimed", "claimed"}:
                        return None
                row["state"] = "rejected"
                row["rejected_reason"] = reason
                row["resolved_at"] = now
                row["resolved_by"] = claimer
                return _FakeRow(quarantine_id=qid)
            return None

        if "SELECT STATE" in s and "FROM COPILOT_QUARANTINED_DOCUMENTS" in s:
            qid = args[0]
            row = self.rows.get(qid)
            if row is None:
                return None
            keys = ["state"]
            if "CLAIMED_BY" in s:
                keys.extend(["claimed_by", "claim_expires_at"])
            return _FakeRow(**{k: row.get(k) for k in keys})

        return None

    def execute_fetch(self, sql: str, args: tuple[Any, ...]) -> list[_FakeRow]:
        s = sql.strip().upper()
        if "FROM COPILOT_QUARANTINED_DOCUMENTS" not in s:
            return []
        panel_id = args[0]
        state_filter = args[1] if len(args) > 1 else None
        out: list[_FakeRow] = []
        for row in self.rows.values():
            if row.get("panel_id") != panel_id:
                continue
            if state_filter is not None and row.get("state") != state_filter:
                continue
            out.append(_FakeRow(**row))
        return out


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> TestClient:
    import main  # imports app + audit_writer
    from audit import writer as audit_writer

    fake_pool = _FakePool(store)

    async def _fake_get_pool() -> Any:
        return fake_pool

    monkeypatch.setattr(audit_writer, "get_pool", _fake_get_pool)

    async def _fake_emit(event: Any) -> None:
        return None

    monkeypatch.setattr(audit_writer, "emit", _fake_emit)
    return TestClient(main.app)


# ── Direct-quarantine-write helper ───────────────────────────────────────────


async def _seed(store: _FakeStore, *, panel_id: str, reason: str = "MRN_NOT_FOUND") -> str:
    from demographics import quarantine as _quar

    row = await _quar.quarantine_document(
        pool=_FakePool(store),
        document_reference_id="doc-ref-test",
        file_batch_id=None,
        panel_id=panel_id,
        parsed_identity={"mrn": "MRN-X", "format": "hl7_v2"},
        candidate_matches=[],
        reason_code=reason,
    )
    return row["quarantine_id"]


# ── Tests ────────────────────────────────────────────────────────────────────


def test_list_returns_panel_scoped_rows(client: TestClient, store: _FakeStore) -> None:
    import asyncio

    qid = asyncio.get_event_loop().run_until_complete(
        _seed(store, panel_id="unauthenticated")
    )

    resp = client.get("/document/quarantine")
    assert resp.status_code == 200
    body = resp.json()
    assert body["panel_id"] == "unauthenticated"
    assert any(r["quarantine_id"] == qid for r in body["rows"])


def test_list_filter_by_state(client: TestClient, store: _FakeStore) -> None:
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(store, panel_id="unauthenticated"))

    resp = client.get("/document/quarantine?state=matched")
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_list_invalid_state_400(client: TestClient) -> None:
    resp = client.get("/document/quarantine?state=bogus")
    assert resp.status_code == 400


def test_claim_then_match_succeeds(client: TestClient, store: _FakeStore) -> None:
    import asyncio

    qid = asyncio.get_event_loop().run_until_complete(
        _seed(store, panel_id="unauthenticated")
    )

    claim_resp = client.post(f"/document/quarantine/{qid}/claim")
    assert claim_resp.status_code == 200, claim_resp.text
    body = claim_resp.json()
    assert body["state"] == "claimed"
    assert body["claim_expires_at"] is not None

    match_resp = client.post(
        f"/document/quarantine/{qid}/match",
        json={"target_patient_id": "p-42"},
    )
    assert match_resp.status_code == 200, match_resp.text
    assert match_resp.json()["state"] == "matched"
    assert match_resp.json()["resolved_patient_id"] == "p-42"


def test_match_without_claim_returns_409(client: TestClient, store: _FakeStore) -> None:
    import asyncio

    qid = asyncio.get_event_loop().run_until_complete(
        _seed(store, panel_id="unauthenticated")
    )

    resp = client.post(
        f"/document/quarantine/{qid}/match",
        json={"target_patient_id": "p-42"},
    )
    assert resp.status_code == 409


def test_match_after_claim_expiry_returns_410(
    client: TestClient, store: _FakeStore
) -> None:
    import asyncio

    qid = asyncio.get_event_loop().run_until_complete(
        _seed(store, panel_id="unauthenticated")
    )
    # Claim, then back-date the claim_expires_at to force expiry.
    claim_resp = client.post(f"/document/quarantine/{qid}/claim")
    assert claim_resp.status_code == 200
    store.rows[qid]["claim_expires_at"] = _dt.datetime.now(
        _dt.timezone.utc
    ) - _dt.timedelta(seconds=1)

    resp = client.post(
        f"/document/quarantine/{qid}/match",
        json={"target_patient_id": "p-42"},
    )
    assert resp.status_code == 410


def test_reject_succeeds_after_claim(client: TestClient, store: _FakeStore) -> None:
    import asyncio

    qid = asyncio.get_event_loop().run_until_complete(
        _seed(store, panel_id="unauthenticated")
    )
    client.post(f"/document/quarantine/{qid}/claim")
    resp = client.post(
        f"/document/quarantine/{qid}/reject",
        json={"reason": "uploader confirmed wrong patient"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "rejected"


def test_reject_reason_too_long_returns_400(
    client: TestClient, store: _FakeStore
) -> None:
    import asyncio

    qid = asyncio.get_event_loop().run_until_complete(
        _seed(store, panel_id="unauthenticated")
    )
    client.post(f"/document/quarantine/{qid}/claim")
    resp = client.post(
        f"/document/quarantine/{qid}/reject",
        json={"reason": "x" * 1000},
    )
    assert resp.status_code == 400


def test_reject_empty_reason_returns_400(
    client: TestClient, store: _FakeStore
) -> None:
    import asyncio

    qid = asyncio.get_event_loop().run_until_complete(
        _seed(store, panel_id="unauthenticated")
    )
    client.post(f"/document/quarantine/{qid}/claim")
    resp = client.post(
        f"/document/quarantine/{qid}/reject",
        json={"reason": "   "},
    )
    assert resp.status_code == 400


def test_claim_unknown_id_returns_404(client: TestClient) -> None:
    bogus = str(uuid.uuid4())
    resp = client.post(f"/document/quarantine/{bogus}/claim")
    assert resp.status_code == 404


def test_ingest_quarantine_path_returns_202(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A POST to /document/ingest with no patient_id and an HL7 body whose
    MRN cannot be resolved must come back 202 with a quarantine_id."""

    # Stub the FHIR client search to return no entries (MRN_NOT_FOUND).
    from auth import fhir_client as _fhir_module

    async def _fake_search(resource: str, params: dict[str, str]) -> dict[str, Any]:
        return {"entry": []}

    monkeypatch.setattr(_fhir_module.fhir_client, "search", _fake_search)

    raw = (
        b"MSH|^~\\&|LAB|H|EMR|MAIN|20260201|||MSG999|P|2.5.1\r"
        b"PID|1||MRN-9999^^^MAIN^MR||DOE^JANE^M||19620314|F|||\r"
    )

    resp = client.post(
        "/document/ingest",
        files={"file": ("msg.hl7", raw, "application/octet-stream")},
        data={"format_hint": "hl7_v2"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "quarantined"
    assert body["reason_code"] == "MRN_NOT_FOUND"
    assert body["quarantine_id"]
    assert body["expires_at"]
