"""In-memory fake asyncpg pool for the Slice 9.3 staging tests.

Implements just the SQL shapes ``staging/store.py`` and
``staging/watchdog.py`` fire — INSERT … ON CONFLICT, UPDATE … RETURNING,
SELECT-by-id, list-by-patient, watchdog reapers. Not a general SQL
engine; pattern-match the SQL strings the store actually emits and route
to a Python dict store. This mirrors the pattern used by
``tests/test_quarantine_endpoints.py`` (Slice 9.2).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any


class FakeRow(dict):
    def keys(self):  # type: ignore[override]
        return list(super().keys())


class _FakeStoreState:
    """Tiny in-memory replacement for ``copilot_pending_extractions``."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next_id: int = 1
        # Audit ring buffer (mirror of audit.writer.emit calls).
        self.audit_events: list[dict[str, Any]] = []
        # Override "now" for time-travel tests.
        self.now_override: _dt.datetime | None = None

    # ── Convenience ──────────────────────────────────────────────────────

    def now(self) -> _dt.datetime:
        return self.now_override or _dt.datetime.now(_dt.timezone.utc)

    def get_row(self, pending_id: int) -> dict[str, Any] | None:
        return self.rows.get(int(pending_id))

    # ── SQL routing ──────────────────────────────────────────────────────

    def execute_fetchrow(self, sql: str, args: tuple[Any, ...]) -> FakeRow | None:
        s = " ".join(sql.split())
        upper = s.upper()

        # ── INSERT … ON CONFLICT (stage_pending) ──────────────────────────
        if upper.startswith("INSERT INTO COPILOT_PENDING_EXTRACTIONS"):
            (
                document_reference_id,
                file_batch_id,
                patient_id,
                target_resource_type,
                target_resource_id,
                payload_text,
            ) = args
            for row in self.rows.values():
                if (
                    row["document_reference_id"] == document_reference_id
                    and row["target_resource_id"] == target_resource_id
                    and row["state"] == "pending"
                ):
                    row["payload"] = (
                        json.loads(payload_text) if isinstance(payload_text, str) else payload_text
                    )
                    row["file_batch_id"] = file_batch_id
                    row["patient_id"] = patient_id
                    row["target_resource_type"] = target_resource_type
                    return FakeRow(id=row["id"], state="pending")
            new_id = self._next_id
            self._next_id += 1
            now = self.now()
            self.rows[new_id] = {
                "id": new_id,
                "document_reference_id": document_reference_id,
                "file_batch_id": str(file_batch_id),
                "patient_id": str(patient_id),
                "target_resource_type": target_resource_type,
                "target_resource_id": target_resource_id,
                "state": "pending",
                "payload": (
                    json.loads(payload_text) if isinstance(payload_text, str) else payload_text
                ),
                "write_error": None,
                "retry_count": 0,
                "staged_at": now,
                "decided_at": None,
                "decided_by": None,
                "written_at": None,
            }
            return FakeRow(id=new_id, state="pending")

        # ── SELECT one by id ──────────────────────────────────────────────
        if upper.startswith("SELECT") and "WHERE ID = $1" in upper and "FROM COPILOT_PENDING_EXTRACTIONS" in upper:
            (pending_id,) = args
            row = self.rows.get(int(pending_id))
            if row is None:
                return None
            return FakeRow(**row)

        # ── UPDATE …  approve / reject / mark_written / mark_failed / retry / auto-reject ──
        if upper.startswith("UPDATE COPILOT_PENDING_EXTRACTIONS"):
            return self._route_update(s, upper, args)

        return None

    def _route_update(
        self, sql: str, upper: str, args: tuple[Any, ...]
    ) -> FakeRow | None:
        # approve
        if "STATE = 'APPROVED'" in upper and "STATE = 'PENDING'" in upper and "RETRY_COUNT" not in upper:
            pending_id, decided_by = args
            row = self.rows.get(int(pending_id))
            if row is None or row["state"] != "pending":
                return None
            now = self.now()
            row["state"] = "approved"
            row["decided_at"] = now
            row["decided_by"] = decided_by
            return FakeRow(
                id=row["id"], target_resource_id=row["target_resource_id"], state="approved"
            )

        # reject
        if "STATE = 'REJECTED'" in upper and "STATE = 'PENDING'" in upper and "DECIDED_BY = 'SYSTEM:WATCHDOG'" not in upper:
            pending_id, decided_by, write_error = args
            row = self.rows.get(int(pending_id))
            if row is None or row["state"] != "pending":
                return None
            now = self.now()
            row["state"] = "rejected"
            row["decided_at"] = now
            row["decided_by"] = decided_by
            row["write_error"] = write_error
            return FakeRow(id=row["id"], state="rejected")

        # mark_written
        if "STATE = 'WRITTEN'" in upper:
            (pending_id,) = args
            row = self.rows.get(int(pending_id))
            if row is None or row["state"] != "approved":
                return None
            row["state"] = "written"
            row["written_at"] = self.now()
            row["write_error"] = None
            return FakeRow(id=row["id"], state="written")

        # mark_failed
        if "STATE = 'FAILED'" in upper and "RETRY_COUNT" not in upper:
            pending_id, write_error = args
            row = self.rows.get(int(pending_id))
            if row is None or row["state"] != "approved":
                return None
            row["state"] = "failed"
            row["write_error"] = write_error
            return FakeRow(id=row["id"], state="failed")

        # retry: failed → approved AND retry_count < max
        if "STATE = 'APPROVED'" in upper and "RETRY_COUNT = RETRY_COUNT + 1" in upper:
            pending_id, max_retries = args
            row = self.rows.get(int(pending_id))
            if (
                row is None
                or row["state"] != "failed"
                or int(row["retry_count"]) >= int(max_retries)
            ):
                return None
            row["state"] = "approved"
            row["retry_count"] = int(row["retry_count"]) + 1
            row["write_error"] = None
            return FakeRow(id=row["id"], state="approved", retry_count=row["retry_count"])

        # auto-reject (watchdog)
        if "DECIDED_BY = 'SYSTEM:WATCHDOG'" in upper:
            pending_id, write_error = args
            row = self.rows.get(int(pending_id))
            if row is None or row["state"] != "pending":
                return None
            row["state"] = "rejected"
            row["decided_at"] = self.now()
            row["decided_by"] = "system:watchdog"
            row["write_error"] = write_error
            return FakeRow(id=row["id"])

        return None

    def execute_fetch(self, sql: str, args: tuple[Any, ...]) -> list[FakeRow]:
        s = " ".join(sql.split())
        upper = s.upper()
        if "FROM COPILOT_PENDING_EXTRACTIONS" not in upper:
            return []

        # list_pending: WHERE patient_id = $1 [AND state = ...] [AND file_batch_id = ...]
        if "WHERE PATIENT_ID = $1" in upper:
            patient_id = args[0]
            filters: dict[str, Any] = {}
            arg_idx = 1
            if "AND STATE = $" in upper:
                filters["state"] = args[arg_idx]
                arg_idx += 1
            if "AND FILE_BATCH_ID = $" in upper:
                filters["file_batch_id"] = args[arg_idx]
                arg_idx += 1
            limit = int(args[arg_idx])
            out: list[FakeRow] = []
            for row in self.rows.values():
                if row["patient_id"] != str(patient_id):
                    continue
                if "state" in filters and row["state"] != filters["state"]:
                    continue
                if (
                    "file_batch_id" in filters
                    and row["file_batch_id"] != str(filters["file_batch_id"])
                ):
                    continue
                out.append(FakeRow(**row))
            out.sort(key=lambda r: r["staged_at"], reverse=True)
            return out[:limit]

        # watchdog stuck-approved reaper
        if "STATE = 'APPROVED'" in upper and "DECIDED_AT < NOW()" in upper:
            (ttl_seconds,) = args
            cutoff = self.now() - _dt.timedelta(seconds=int(ttl_seconds))
            out = []
            for row in self.rows.values():
                if row["state"] != "approved":
                    continue
                if row["decided_at"] is None or row["decided_at"] >= cutoff:
                    continue
                out.append(FakeRow(**row))
            return out

        # watchdog stale-pending notifier
        if "STATE = 'PENDING'" in upper and "STAGED_AT < NOW()" in upper:
            (days,) = args
            cutoff = self.now() - _dt.timedelta(days=int(days))
            out = []
            now = self.now()
            for row in self.rows.values():
                if row["state"] != "pending":
                    continue
                if row["staged_at"] >= cutoff:
                    continue
                age_days = (now - row["staged_at"]).total_seconds() / 86400.0
                d = dict(row)
                d["age_days"] = age_days
                out.append(FakeRow(**d))
            return out

        return []


class _FakeConn:
    def __init__(self, store: _FakeStoreState) -> None:
        self.store = store

    async def fetchrow(self, sql: str, *args: Any) -> FakeRow | None:
        return self.store.execute_fetchrow(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[FakeRow]:
        return self.store.execute_fetch(sql, args)

    async def execute(self, sql: str, *args: Any) -> None:
        self.store.execute_fetchrow(sql, args)


class _AsyncCM:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class FakePool:
    def __init__(self, store: _FakeStoreState) -> None:
        self.store = store

    def acquire(self) -> _AsyncCM:
        return _AsyncCM(_FakeConn(self.store))


def make_fake_pool() -> tuple[FakePool, _FakeStoreState]:
    state = _FakeStoreState()
    return FakePool(state), state


__all__ = ["FakePool", "FakeRow", "make_fake_pool"]
