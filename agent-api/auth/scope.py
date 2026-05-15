"""Pre-tool-call patient-scope check.

Prevents the planner from "smuggling" tool calls for patients outside the
authenticated provider's current census. The dispatcher invokes
``check_patient_scope`` immediately before executing any tool_use block; on a
deny it surfaces an ``is_error=True`` tool_result back to the LLM rather than
crashing the loop.

Authoritative scope source: ``session_context["patient_ids"]`` — populated by
the HTTP layer from the inbound request body (which itself comes from the
HS256 launch JWT minted by the OpenEMR iframe wrapper).

Posture: **fail-closed** (VUL-0001 mitigation, 2026-05-15). Previous rollout
phase allowed an empty census to fall through; VUL-0001 demonstrated that
this gap is exactly what multi-turn social-engineering attacks exploit
(planner names an arbitrary out-of-panel patient and the dispatcher
executes the FHIR fetch). An empty census now denies any patient-keyed
tool call — the only legitimate caller in that state is the bootstrap
census-discovery path itself, which is not a member of ``PATIENT_KEYED_TOOLS``.

ID normalization: clinicians' census carries OpenEMR numeric pids
(``"5"``, ``"13"``) sourced from ``form_encounter.provider_id`` lookups.
The planner sometimes emits synthetic aliases (``"pt-018"``) or already-
resolved FHIR UUIDs (``"6da9dadb-..."``). To prevent a trivial alias-
bypass, we canonicalize both sides into the numeric-pid form when
possible, and compare the original string as a fallback for UUIDs.

This module is part of the ``auth`` leaf package. Per the import-linter
contract it must not import from any sibling business package.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Tools whose ``patient_id`` argument identifies a single patient that must
# fall inside the current provider's census. Tools NOT in this set are
# treated as out-of-scope-of-the-check (e.g. ``get_census_summary`` IS the
# scope source itself; ``generate_handoff`` takes a list and is governed by
# the same upstream filter).
PATIENT_KEYED_TOOLS: frozenset[str] = frozenset(
    {
        "get_patient_briefing",
        "get_medication_safety",
        "query_patient_records",
        "get_triage_rationale",
    }
)

_PT_ALIAS_RE = re.compile(r"^pt-(\d+)$", re.IGNORECASE)


def _canonical_pid(raw: Any) -> str:
    """Normalize a patient id string for membership comparison.

    Maps ``"pt-018"`` → ``"18"``, ``"5"`` → ``"5"``. UUIDs and other
    free-form strings pass through unchanged (lower-cased) so that
    direct-UUID membership still works on both sides.
    """
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    m = _PT_ALIAS_RE.match(s)
    if m:
        return str(int(m.group(1)))
    if s.isdigit():
        return str(int(s))
    return s


def check_patient_scope(
    tool_name: str,
    tool_input: dict[str, Any],
    session_context: dict[str, Any],
) -> tuple[bool, str | None]:
    """Decide whether ``tool_name`` may be invoked for this ``patient_id``.

    Returns ``(allowed, reason_if_not)``.  ``reason_if_not`` is non-None
    only when ``allowed`` is False and is suitable for surfacing to the LLM
    as the body of an ``is_error=True`` tool_result.

    Decisions (fail-closed posture):
      * Tool not in ``PATIENT_KEYED_TOOLS`` → allow.
      * No ``patient_id`` in ``tool_input`` → allow (nothing to scope).
      * ``session_context["patient_ids"]`` missing/empty → **deny**
        (VUL-0001 mitigation).
      * Canonicalized ``patient_id`` IS in canonicalized census → allow.
      * Otherwise → deny with reason.
    """
    if tool_name not in PATIENT_KEYED_TOOLS:
        return True, None

    requested = tool_input.get("patient_id")
    if requested is None or requested == "":
        return True, None

    raw_ids = session_context.get("patient_ids")
    if not raw_ids:
        # Fail-closed: there is no authoritative panel to validate against.
        # In production this means the iframe launch did not deliver the
        # JWT-derived panel into the request body, or some earlier hop
        # stripped it. VUL-0001 (2026-05-15) showed that the fail-open
        # variant of this branch was directly exploitable by multi-turn
        # social-engineering; deny here and let the dispatcher surface the
        # refusal as a structured tool_result so the LLM cannot retry the
        # same call with a different alias.
        logger.warning(
            "tool_scope_check_deny_empty_census",
            extra={
                "tool_name": tool_name,
                "requested_patient_id": requested,
                "session_id": session_context.get("session_id"),
            },
        )
        return (
            False,
            "scope_violation: no active census on this session; refuse to fetch "
            "patient data without a clinician-scoped panel",
        )

    census_canonical = {_canonical_pid(p) for p in raw_ids if p is not None and p != ""}
    requested_canonical = _canonical_pid(requested)
    if requested_canonical and requested_canonical in census_canonical:
        return True, None

    return (
        False,
        f"scope_violation: patient_id {requested} is not on the current provider's census",
    )


def filter_patient_ids_to_panel(
    patient_ids: list[Any] | None,
    session_context: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Restrict a list of patient ids to the current session's panel.

    Returns ``(in_panel, dropped)`` — the canonicalized in-panel ids that
    callers may proceed with, and the dropped (out-of-panel) ids for the
    purpose of audit logging. Used by bulk-extraction tools (handoff,
    census) where the LLM planner is allowed to pass a *list* of patient
    ids; VUL-0002 (2026-05-15) showed the unfiltered list was the
    exfiltration channel.

    Decisions (fail-closed):
      * ``session_context["patient_ids"]`` missing/empty → all input
        treated as out-of-panel (drop everything, return ``([], [...all])``).
      * Each input id is canonicalized; only those whose canonical form
        appears in the canonicalized panel are returned.
      * Empty/None inputs become ``([], [])``.
    """
    raw_input = list(patient_ids or [])
    if not raw_input:
        return [], []

    raw_panel = session_context.get("patient_ids")
    if not raw_panel:
        logger.warning(
            "bulk_tool_panel_filter_empty_census",
            extra={
                "input_count": len(raw_input),
                "session_id": session_context.get("session_id"),
            },
        )
        return [], [str(p) for p in raw_input if p is not None]

    panel_canonical = {
        _canonical_pid(p) for p in raw_panel if p is not None and p != ""
    }

    kept: list[str] = []
    dropped: list[str] = []
    for raw in raw_input:
        if raw is None or raw == "":
            continue
        cid = _canonical_pid(raw)
        if cid and cid in panel_canonical:
            kept.append(str(raw))
        else:
            dropped.append(str(raw))
    return kept, dropped


__all__ = [
    "check_patient_scope",
    "filter_patient_ids_to_panel",
    "PATIENT_KEYED_TOOLS",
]
