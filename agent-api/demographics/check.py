"""Wrong-patient demographic comparison (W2_ARCHITECTURE §5.6).

Pure functions. No I/O, no logging. The caller (graph.nodes.critic) maps the
returned :class:`DemographicCheckResult` into a critic verdict and emits the
audit row.

The §5.6 table is implemented verbatim — see the unit tests in
``tests/test_demographics.py`` for one parametrized case per row.
"""

from __future__ import annotations

import re
from typing import Any, Literal, NamedTuple

# ── Result type ──────────────────────────────────────────────────────────────


class DemographicCheckResult(NamedTuple):
    decision: Literal["pass", "soft_warn", "hard_block"]
    reason_code: str
    message: str


# ── Per-field matchers ───────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _normalize_name(value: str) -> str:
    return _WS_RE.sub(" ", value).strip().lower()


def _mrn_match(
    doc_mrn: str | None, chart_mrn: str
) -> Literal["match", "mismatch", "unreadable", "absent"]:
    """Compare document MRN to chart MRN.

    The doc-side accepts the sentinel string ``"UNREADABLE"`` to indicate the
    OCR layer saw an MRN region but could not transcribe it; ``None`` or
    empty means the document had no MRN at all.
    """
    if doc_mrn is None or doc_mrn == "":
        return "absent"
    if doc_mrn == "UNREADABLE":
        return "unreadable"
    if doc_mrn.strip() == chart_mrn.strip():
        return "match"
    return "mismatch"


def _name_match(
    doc_name: str | None, chart_name: str
) -> Literal["match", "mismatch", "absent"]:
    """Case-insensitive, whitespace-collapsed comparison."""
    if doc_name is None or doc_name.strip() == "":
        return "absent"
    if _normalize_name(doc_name) == _normalize_name(chart_name):
        return "match"
    return "mismatch"


def _dob_match(
    doc_dob: str | None, chart_dob: str
) -> Literal["match", "mismatch", "absent"]:
    """Strict ISO-date string equality (``YYYY-MM-DD``)."""
    if doc_dob is None or doc_dob == "":
        return "absent"
    if doc_dob == chart_dob:
        return "match"
    return "mismatch"


# ── Public entry point ───────────────────────────────────────────────────────


def check_demographics(
    *,
    document_demographics: dict[str, Any],
    chart_patient: dict[str, Any],
) -> DemographicCheckResult:
    """Compare document-extracted demographics to the chart patient.

    Implements the §5.6 wrong-patient table verbatim.

    Parameters
    ----------
    document_demographics:
        ``{"mrn": str | None | "UNREADABLE", "name": str | None, "dob": str | None}``
        — ``dob`` is an ISO date (``YYYY-MM-DD``) when present.
    chart_patient:
        ``{"mrn": str, "name": str, "dob": str}`` from the chart's FHIR
        Patient resource.

    Returns
    -------
    :class:`DemographicCheckResult` with categorical decision, machine
    reason code, and human-readable banner message.
    """
    mrn = _mrn_match(document_demographics.get("mrn"), chart_patient["mrn"])
    name = _name_match(document_demographics.get("name"), chart_patient["name"])
    dob = _dob_match(document_demographics.get("dob"), chart_patient["dob"])

    # ── MRN match ───────────────────────────────────────────────────────────
    if mrn == "match":
        if name == "match" and dob == "match":
            return DemographicCheckResult(
                decision="pass",
                reason_code="ALL_MATCH",
                message="",
            )
        if name == "match" and dob == "mismatch":
            return DemographicCheckResult(
                decision="soft_warn",
                reason_code="MRN_MATCH_DOB_MISMATCH",
                message="DOB on document differs from chart — verify",
            )
        if name == "mismatch" and dob == "match":
            return DemographicCheckResult(
                decision="soft_warn",
                reason_code="MRN_MATCH_NAME_MISMATCH",
                message="name on document differs from chart — verify",
            )
        # name mismatch and dob mismatch (or absent + mismatch combos under MRN match)
        return DemographicCheckResult(
            decision="soft_warn",
            reason_code="MRN_MATCH_OTHERS_MISMATCH",
            message=(
                "MRN matches but other identifiers don't — "
                "possible OCR collision, verify"
            ),
        )

    # ── MRN mismatch ────────────────────────────────────────────────────────
    if mrn == "mismatch":
        return DemographicCheckResult(
            decision="hard_block",
            reason_code="MRN_MISMATCH",
            message="MRN on document does not match chart",
        )

    # ── MRN unreadable ──────────────────────────────────────────────────────
    if mrn == "unreadable":
        if name == "match" and dob == "match":
            return DemographicCheckResult(
                decision="soft_warn",
                reason_code="MRN_UNREADABLE_NAME_DOB_MATCH",
                message="MRN visible on document but unreadable — verify",
            )
        return DemographicCheckResult(
            decision="hard_block",
            reason_code="MRN_UNREADABLE_OTHERS_DIVERGE",
            message="MRN unreadable and other identifiers do not both match — refusing",
        )

    # ── MRN absent ──────────────────────────────────────────────────────────
    # mrn == "absent"
    if name == "match" and dob == "match":
        return DemographicCheckResult(
            decision="pass",
            reason_code="WEAK_SIGNAL_PASS",
            message="No MRN on document — name+DOB match (weak-signal pass)",
        )
    if name == "match" and dob == "mismatch":
        return DemographicCheckResult(
            decision="hard_block",
            reason_code="NO_MRN_DOB_MISMATCH",
            message="DOB is the most reliable identifier absent MRN",
        )
    if name == "mismatch" and dob == "match":
        return DemographicCheckResult(
            decision="soft_warn",
            reason_code="NO_MRN_NAME_MISMATCH",
            message=(
                "name on document differs from chart — "
                "married names, transcription drift"
            ),
        )
    # absent + mismatch + mismatch (or absent name combos that aren't covered above)
    return DemographicCheckResult(
        decision="hard_block",
        reason_code="NO_MRN_NAME_DOB_DIVERGE",
        message="No MRN and other identifiers do not match — refusing",
    )


__all__ = ["DemographicCheckResult", "check_demographics"]
