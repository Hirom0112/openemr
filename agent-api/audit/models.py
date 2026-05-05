"""Audit event dataclass shared by the writer and middleware.

Single dataclass — kept dependency-free so the ``audit`` package stays a
leaf in ``.importlinter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditEvent:
    """One row destined for ``copilot_audit_events``.

    All fields except ``event_type`` are optional; the writer fills in
    ``ts`` server-side via ``DEFAULT NOW()``.

    ``detail_json`` MUST NOT contain free clinical text, prompt content,
    or completion text — only structured codes (failure_class, status_code,
    tool_name, ambiguous_name flag, reference IDs). The writer enforces a
    2 KB string-encoded ceiling and a small phrase block-list as a
    defensive guard against accidental PHI leakage.
    """

    event_type: str  # request | tool_call | scope_violation | auth_failure | destruction
    request_id: str | None = None
    session_id: str | None = None
    provider_id: str | None = None
    patient_id: str | None = None
    tool_name: str | None = None
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    duration_ms: int | None = None
    outcome: str | None = None  # success | failure | denied
    detail_json: dict[str, Any] = field(default_factory=dict)
