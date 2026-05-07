"""W2State — typed dict for the Week-2 LangGraph pipeline.

See W2_ARCHITECTURE.md §5.10. Every node mutates this single state object;
LangGraph merges per-node returns into it via TypedDict semantics.

We import TypedDict / NotRequired from typing_extensions so this module
works on Python 3.9 (the agent-api runtime); the future-annotations import
defers evaluation of `str | None` / `list[...]` syntax.
"""
from typing import Any, Dict, List, Literal, Optional

from typing_extensions import NotRequired, TypedDict


class W2State(TypedDict):
    # --- Inputs ---------------------------------------------------------
    request_id: str
    patient_id: Optional[str]
    patient_ids: NotRequired[List[str]]
    session_id: str
    provider_id: str
    message: Optional[str]                # textual user query
    file_bytes_ref: Optional[str]         # opaque pointer; raw bytes pass via ctx
    doc_type_hint: Optional[str]

    # --- Pipeline outputs ----------------------------------------------
    ocr_layout: NotRequired[List[Dict[str, Any]]]
    classifier_verdict: NotRequired[Dict[str, Any]]
    extraction: NotRequired[Dict[str, Any]]
    demographic_check: NotRequired[Dict[str, Any]]
    retrieval: NotRequired[Dict[str, Any]]
    conflict_pass: NotRequired[Dict[str, Any]]
    # Phase 9 Slice 9.7 — cross-source conflict pass output (additive).
    # Populated by ``graph.nodes.cross_source_conflict``. See W2_ARCH §5.11.
    cross_source_conflicts: NotRequired[List[Dict[str, Any]]]
    # Inputs read by the cross-source conflict node — also additive.
    staged_lab_values: NotRequired[List[Dict[str, Any]]]
    persisted_observations: NotRequired[List[Dict[str, Any]]]
    critic_decision: NotRequired[Literal["pass", "soft_warn", "hard_block"]]
    critic_violations: NotRequired[List[str]]
    soft_warns: NotRequired[List[Dict[str, Any]]]
    structured_response: NotRequired[Dict[str, Any]]
    errors: NotRequired[List[str]]
    finalized: NotRequired[Dict[str, Any]]

    # --- Routing -------------------------------------------------------
    next_node: NotRequired[str]


def make_initial_state(
    *,
    request_id: str,
    session_id: str,
    provider_id: str,
    patient_id: Optional[str] = None,
    patient_ids: Optional[List[str]] = None,
    message: Optional[str] = None,
    file_bytes_ref: Optional[str] = None,
    doc_type_hint: Optional[str] = None,
) -> W2State:
    """Build a W2State with required fields populated and safe defaults."""
    state: W2State = {
        "request_id": request_id,
        "patient_id": patient_id,
        "patient_ids": list(patient_ids or []),
        "session_id": session_id,
        "provider_id": provider_id,
        "message": message,
        "file_bytes_ref": file_bytes_ref,
        "doc_type_hint": doc_type_hint,
        "errors": [],
    }
    return state


__all__ = ["W2State", "make_initial_state"]
