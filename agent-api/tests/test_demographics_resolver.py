"""Tests for the pre-extraction patient resolver (Slice 9.2).

One parametrized case per row of the resolver decision matrix in
``demographics/resolver.py``. The FHIR client is mocked at the
``fhir_search`` injection seam so the test stays a true unit test.
"""

from __future__ import annotations

from typing import Any

import pytest

from demographics.resolver import (
    Pass,
    Quarantine,
    probe_hl7_v2,
    resolve,
)


pytestmark = [pytest.mark.hard_failure, pytest.mark.clinical_accuracy]


# ── HL7 PID probe basics ─────────────────────────────────────────────────────


_HL7_LIPID_OK = (
    b"MSH|^~\\&|LAB|HOSPITAL|EMR|MAIN|20260201120000||ORU^R01|MSG001|P|2.5.1\r"
    b"PID|1||MRN-001^^^MAIN^MR||DOE^JANE^M||19620314|F|||\r"
    b"OBR|1||L1|24331-1^Lipid panel^LN|||20260201100000\r"
    b"OBX|1|NM|2093-3^Cholesterol total^LN||210|mg/dL|<200|H|||F\r"
)


def test_probe_hl7_extracts_pid3_pid5_pid7() -> None:
    pi = probe_hl7_v2(_HL7_LIPID_OK)
    assert pi is not None
    assert pi.mrn == "MRN-001"
    assert pi.name == "JANE DOE"  # given family
    assert pi.dob == "1962-03-14"
    assert pi.format == "hl7_v2"
    assert pi.same_region is True


def test_probe_hl7_returns_none_when_pid_missing() -> None:
    raw = b"MSH|^~\\&|LAB|HOSPITAL|EMR|MAIN|20260201|||MSG002|P|2.5.1\r"
    assert probe_hl7_v2(raw) is None


def test_probe_hl7_handles_missing_dob() -> None:
    raw = b"MSH|^~\\&|LAB|HOSPITAL|EMR|MAIN|20260201|||MSG003|P|2.5.1\rPID|1||MRN-002^^^MAIN^MR||SMITH^JOHN||\r"
    pi = probe_hl7_v2(raw)
    assert pi is not None
    assert pi.mrn == "MRN-002"
    assert pi.dob is None
    assert pi.same_region is False  # name without DOB → no co-location guarantee


# ── FHIR search mock ─────────────────────────────────────────────────────────


def _patient_entry(pid: str, name: str | None, dob: str | None) -> dict[str, Any]:
    given = ""
    family = ""
    if name:
        parts = name.split()
        given = parts[0] if parts else ""
        family = parts[-1] if len(parts) >= 2 else ""
    res: dict[str, Any] = {
        "id": pid,
        "name": [{"given": [given], "family": family}],
    }
    if dob:
        res["birthDate"] = dob
    return {"resource": res}


class _FakeFhir:
    """Records every call and returns a programmable bundle."""

    def __init__(self, bundles: list[dict[str, Any]]) -> None:
        self._bundles = list(bundles)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, resource: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append((resource, dict(params)))
        if not self._bundles:
            return {"entry": []}
        return self._bundles.pop(0)


# ── Decision matrix ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mrn_exact_one_passes() -> None:
    fhir = _FakeFhir([{"entry": [_patient_entry("p-1", "JANE DOE", "1962-03-14")]}])
    out = await resolve(raw=_HL7_LIPID_OK, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Pass)
    assert out.patient_id == "p-1"
    assert out.source == "mrn_exact"
    # First call is identifier=MRN.
    assert fhir.calls[0][1] == {"identifier": "MRN-001"}


@pytest.mark.asyncio
async def test_mrn_exact_multiple_quarantines_ambiguous() -> None:
    fhir = _FakeFhir(
        [
            {
                "entry": [
                    _patient_entry("p-1", "JANE DOE", "1962-03-14"),
                    _patient_entry("p-2", "JANE DOE", "1962-03-14"),
                ]
            }
        ]
    )
    out = await resolve(raw=_HL7_LIPID_OK, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Quarantine)
    assert out.reason_code == "MRN_AMBIGUOUS"
    # Hint payload includes the parsed identity + per-candidate rows.
    assert len(out.candidate_hints) >= 2


@pytest.mark.asyncio
async def test_mrn_not_found_quarantines() -> None:
    fhir = _FakeFhir([{"entry": []}])
    out = await resolve(raw=_HL7_LIPID_OK, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Quarantine)
    assert out.reason_code == "MRN_NOT_FOUND"


@pytest.mark.asyncio
async def test_mrn_unreadable_falls_back_then_quarantines_when_no_chart_match() -> None:
    raw = (
        b"MSH|^~\\&|LAB|HOSPITAL|EMR|MAIN|20260201|||MSG004|P|2.5.1\r"
        b"PID|1||UNREADABLE^^^MAIN^MR||DOE^JANE^M||19620314|F|||\r"
    )
    fhir = _FakeFhir([{"entry": []}])
    out = await resolve(raw=raw, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Quarantine)
    # Reason code surfaces as MRN_UNREADABLE — not NAME_DOB_NOT_FOUND —
    # so the operator banner says "MRN region was unreadable".
    assert out.reason_code == "MRN_UNREADABLE"


@pytest.mark.asyncio
async def test_no_pid_segment_quarantines_no_identity_hints() -> None:
    raw = b"MSH|^~\\&|LAB|HOSPITAL|EMR|MAIN|20260201|||MSG005|P|2.5.1\r"
    fhir = _FakeFhir([])
    out = await resolve(raw=raw, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Quarantine)
    assert out.reason_code == "NO_IDENTITY_HINTS"
    # FHIR should not be hit when the probe yielded nothing.
    assert fhir.calls == []


@pytest.mark.asyncio
async def test_missing_dob_in_pid_quarantines_when_mrn_absent() -> None:
    # Empty MRN field, name present, DOB blank.
    raw = (
        b"MSH|^~\\&|LAB|HOSPITAL|EMR|MAIN|20260201|||MSG006|P|2.5.1\r"
        b"PID|1||||DOE^JANE^M|||F|||\r"
    )
    fhir = _FakeFhir([])
    out = await resolve(raw=raw, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Quarantine)
    # No MRN, name without DOB → same_region False → REGION_MISMATCH path.
    assert out.reason_code in {"NAME_DOB_REGION_MISMATCH", "MISSING_DOB"}


# ── Soft-resolve gates ───────────────────────────────────────────────────────


_HL7_NO_MRN = (
    b"MSH|^~\\&|LAB|HOSPITAL|EMR|MAIN|20260201|||MSG007|P|2.5.1\r"
    b"PID|1||||DOE^JANE^M||19620314|F|||\r"
)


@pytest.mark.asyncio
async def test_soft_resolve_passes_when_all_four_gates_hold() -> None:
    # Empty identifier search returns no rows (we don't issue one); the
    # resolver routes straight to the name+DOB query with _count=2.
    fhir = _FakeFhir(
        [{"entry": [_patient_entry("p-soft", "JANE DOE", "1962-03-14")]}]
    )
    out = await resolve(raw=_HL7_NO_MRN, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Pass)
    assert out.source == "name_dob_soft"
    assert out.patient_id == "p-soft"
    # Confirm we asked FHIR with _count=2 — the gate (3) confirmation.
    assert fhir.calls[0][0] == "Patient"
    assert fhir.calls[0][1]["_count"] == "2"
    assert fhir.calls[0][1]["birthdate"] == "1962-03-14"


@pytest.mark.asyncio
async def test_soft_resolve_quarantines_when_count2_returns_two() -> None:
    fhir = _FakeFhir(
        [
            {
                "entry": [
                    _patient_entry("p-1", "JANE DOE", "1962-03-14"),
                    _patient_entry("p-2", "JANE DOE", "1962-03-14"),
                ]
            }
        ]
    )
    out = await resolve(raw=_HL7_NO_MRN, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Quarantine)
    assert out.reason_code == "NAME_DOB_AMBIGUOUS"


@pytest.mark.asyncio
async def test_soft_resolve_quarantines_when_chart_name_disagrees() -> None:
    # FHIR returns one candidate but the name doesn't normalize-equal —
    # gate (1) fails.
    fhir = _FakeFhir(
        [{"entry": [_patient_entry("p-1", "JANET ROE", "1962-03-14")]}]
    )
    out = await resolve(raw=_HL7_NO_MRN, format_hint="hl7_v2", fhir_search=fhir)
    assert isinstance(out, Quarantine)
    assert out.reason_code == "NAME_DOB_NOT_FOUND"
