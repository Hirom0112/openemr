"""PHI-safety guard for the W2 eval set.

All names, MRNs, and DOBs in ``CASES`` are synthetic. This test maintains a
whitelist of every synthetic identity used; any drift triggers a failure so
real PHI cannot be sneaked into the eval-set fixtures via a future PR.
"""

from __future__ import annotations

import re

import pytest

from tests.fixtures.w2_eval_cases import CASES

# Synthetic identities used across CASES. Format: (given, family, mrn, dob).
# When you add a new chart_patient, add its identity here.
SYNTHETIC_IDENTITIES: frozenset[tuple[str, str, str, str]] = frozenset({
    ("Marcus",   "Webb",       "100847", "1962-03-14"),
    ("Marcus",   "Webb",       "100847", "1961-03-14"),  # wrong-patient DOB-mismatch variant
    ("Marcus",   "Webb",       "100847", "1972-11-30"),  # wrong-patient DOB-mismatch variant
    ("Markus",   "Webber",     "100847", "1962-03-14"),  # wrong-patient name-mismatch variant
    ("Roberta",  "Klein",      "100847", "1979-09-01"),  # wrong-patient MRN-only-collision variant
    ("Marcus",   "Webb",       "200999", "1962-03-14"),  # wrong-patient MRN-mismatch variant
    ("Jane",     "Doe",        "100231", "1978-07-22"),
    ("Carlos",   "Reyes",      "100412", "1955-11-02"),
    ("Priya",    "Natarajan",  "100558", "1990-01-19"),
    ("Liang",    "Park",       "100719", "1971-09-30"),
    ("Eleanor",  "Whitfield",  "100923", "1942-08-11"),
    ("Tomas",    "Albright",   "100377", "1985-02-26"),
    ("Hannah",   "Goldberg",   "100644", "2001-12-04"),
    ("Sven",     "Halvorsen",  "100805", "1968-05-17"),
    ("Yara",     "Olsson",     "100488", "1980-06-09"),
    # Real-shaped document patients (synthetic identities, see
    # tests/fixtures/eval/real-examples/). Chart-side MRNs use the standard
    # 100xxx synthetic range; document-side MRNs are formatted as
    # "MRN-2026-XXXXX" and intentionally do not match — the demographic
    # check degrades gracefully when MRN formats differ (W2_ARCH §5.6).
    ("Margaret", "Chen",       "100481", "1967-08-14"),
    ("James",    "Whitaker",   "100492", "1958-11-03"),
    ("Luis",     "Reyes",      "100503", "1972-05-18"),
    ("Andrzej",  "Kowalski",   "100518", "1954-09-22"),
    # 88-case expansion identities (geriatric, pediatric, OB, non-English,
    # off-by-one MRN refusal, stale-chart DOB drift, evidence-retrieval).
    ("Bertha",   "Nieminen",   "100744", "1944-04-04"),
    ("Darius",   "Okonkwo",    "100051", "2009-09-09"),
    ("Elena",    "Vargas",     "100862", "1992-06-15"),
    ("Felix",    "Dubois",     "100278", "1976-10-01"),
    ("Casey",    "Stone",      "100999", "1980-01-01"),
    ("Marcus",   "Webb",       "100848", "1962-03-14"),  # off-by-one MRN refusal variant
    ("Marcus",   "Webb",       "100847", "1965-03-14"),  # stale-chart DOB-drift variant
    # Wave 2C — bbox_gt bucket (synthetic_v2 fixtures with sidecar GT).
    # All 36 bbox_gt cases reuse this single benign chart patient; the
    # rubric operates on document-side citations, not demographics.
    ("GT",       "Synthetic",  "200000", "1980-01-01"),
})

# Spot-check ranges: every synthetic MRN starts with "100" or "200" (W2 reserves
# 100xxx and 200xxx for synthetic eval data). Every synthetic DOB falls in
# 1940-2010. Names are restricted to the whitelist above.
_MRN_PREFIX_RE = re.compile(r"^(100|200)\d{3}$")
_DOB_RE = re.compile(r"^(19[4-9]\d|200\d|2010)-\d{2}-\d{2}$")


def _identity(patient: dict) -> tuple[str, str, str, str]:
    name = patient["name"][0]
    given = name["given"][0]
    family = name["family"]
    mrn = ""
    for ident in patient.get("identifier", []):
        coding = ident.get("type", {}).get("coding", [])
        if coding and coding[0].get("code") == "MR":
            mrn = ident["value"]
            break
    dob = patient.get("birthDate", "")
    return given, family, mrn, dob


@pytest.mark.hard_failure
def test_every_chart_patient_is_synthetic() -> None:
    seen: set[tuple[str, str, str, str]] = set()
    offenders: list[tuple[str, tuple[str, str, str, str]]] = []
    for case in CASES:
        ident = _identity(case.chart_patient)
        seen.add(ident)
        if ident not in SYNTHETIC_IDENTITIES:
            offenders.append((case.case_id, ident))
    assert not offenders, (
        "Non-whitelisted patient identities found in CASES — add to "
        "SYNTHETIC_IDENTITIES if intentional, otherwise this is real PHI: "
        f"{offenders}"
    )


@pytest.mark.hard_failure
def test_synthetic_mrn_and_dob_format() -> None:
    bad: list[str] = []
    for case in CASES:
        _, _, mrn, dob = _identity(case.chart_patient)
        if not _MRN_PREFIX_RE.match(mrn):
            bad.append(f"{case.case_id}: MRN {mrn!r} not in synthetic 100xxx/200xxx range")
        if not _DOB_RE.match(dob):
            bad.append(f"{case.case_id}: DOB {dob!r} not in 1940-2010 range")
    assert not bad, "\n".join(bad)


@pytest.mark.hard_failure
def test_whitelist_does_not_drift() -> None:
    """Every entry in the whitelist must be referenced by at least one case OR
    be an explicit wrong-patient variant of a referenced identity. This stops
    the whitelist from becoming a junk drawer."""
    referenced = {_identity(c.chart_patient) for c in CASES}
    referenced_mrns = {ident[2] for ident in referenced}
    unused = []
    for ident in SYNTHETIC_IDENTITIES:
        if ident in referenced:
            continue
        # Permit MRN-sharing variants kept around for documentation purposes.
        if ident[2] in referenced_mrns:
            continue
        unused.append(ident)
    assert not unused, f"unused whitelist entries: {unused}"
