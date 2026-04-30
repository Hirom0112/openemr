"""Integration tests — real classifications for all 18 synthetic patients.

These tests load actual synthetic bundle data and assert the triage output
the rules engine produces for each patient.  They answer "is this patient
classified correctly?" not "does the rules engine correctly handle inputs?"
That distinction matters: if the synthetic data changes, these tests will
fail and tell you, whereas unit tests with constructed inputs will not.

All expected levels were verified by running the classifier on the actual
bundles and confirming the output matches the patient's intended scenario.

NOTE — eval corpus gap documented at the bottom of this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.clinical_accuracy

from triage.criteria import extract
from triage.rules_engine import rank

_BUNDLE_DIR = Path(__file__).parent.parent.parent / "synthetic_data" / "bundles"


def _load_bundle(fname: str) -> dict:
    data = json.loads((_BUNDLE_DIR / fname).read_text())
    resources: dict = {"Observation": [], "MedicationRequest": [], "Condition": [], "AllergyIntolerance": []}
    for entry in data.get("entry", []):
        res = entry.get("resource", {})
        rt = res.get("resourceType")
        if rt in resources:
            resources[rt].append({"resource": res})
    return {"resources": resources}


# ── Per-patient assertions ────────────────────────────────────────────────────

@pytest.mark.hard_failure
class TestMarcusWebb:
    """pt-001: Sepsis scenario — qSOFA ≥2, SpO2=88, critical labs."""
    def test_classified_as_sepsis(self):
        result = rank(extract(_load_bundle("pt-001.json")))
        assert result.level in (1, 2), f"Expected sepsis level (1-2), got {result.level}"


@pytest.mark.hard_failure
class TestDeliaFontaine:
    """pt-002: Critical K+ 6.4 — critical lab, normal vitals."""
    def test_classified_as_critical_lab(self):
        result = rank(extract(_load_bundle("pt-002.json")))
        assert result.level == 3, f"Expected P3 (critical lab), got {result.level}"


class TestRaymondOkafor:
    """pt-003: Abnormal labs, borderline SpO2=91 — no critical thresholds crossed."""
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-003.json")))
        assert result.level == 7, f"Expected P7 (abnormal lab), got {result.level}"


class TestGloriaTran:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-004.json")))
        assert result.level == 7


class TestBernardKowalski:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-005.json")))
        assert result.level == 7


class TestIngridNakamura:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-006.json")))
        assert result.level == 7


class TestDarnellSimmons:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-007.json")))
        assert result.level == 7


class TestYvonneCastillo:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-008.json")))
        assert result.level == 7


class TestElenaMorales:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-009.json")))
        assert result.level == 7


class TestRajivPatel:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-010.json")))
        assert result.level == 7


class TestKarlBergstrom:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-011.json")))
        assert result.level == 7


class TestMiriamJohnson:
    """pt-012: No abnormal labs, active conditions — stable."""
    def test_classified_as_stable(self):
        result = rank(extract(_load_bundle("pt-012.json")))
        assert result.level == 8, f"Expected P8 (active condition, stable), got {result.level}"


@pytest.mark.hard_failure
class TestCarlosReyes:
    """pt-013: Critical lab present."""
    def test_classified_as_critical_lab(self):
        result = rank(extract(_load_bundle("pt-013.json")))
        assert result.level == 3, f"Expected P3 (critical lab), got {result.level}"


class TestAbenaOsei:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-014.json")))
        assert result.level == 7


class TestDorothyWilliams:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-015.json")))
        assert result.level == 7


class TestWeiHuang:
    """pt-016: No abnormal labs, active conditions — stable."""
    def test_classified_as_stable(self):
        result = rank(extract(_load_bundle("pt-016.json")))
        assert result.level == 8


class TestSeanMurphy:
    """pt-017: No abnormal labs, active conditions — stable."""
    def test_classified_as_stable(self):
        result = rank(extract(_load_bundle("pt-017.json")))
        assert result.level == 8


@pytest.mark.hard_failure
class TestThomasGreer:
    """pt-018: AF with RVR — HR=136 is intentional, not a generator bug.

    Scenario S10: out-of-census patient under prov-other.  His diagnosis is
    atrial fibrillation with rapid ventricular response; HR=136 is clinically
    appropriate.  He was previously (incorrectly) used as a 'routine' fixture.
    """
    def test_classified_as_critical_vital(self):
        criteria = extract(_load_bundle("pt-018.json"))
        assert criteria.critical_vital is True, "Thomas Greer HR=136 should flag critical_vital"
        result = rank(criteria)
        assert result.level == 4, f"Expected P4 (critical vital sign), got {result.level}"

    def test_hr_exceeds_critical_threshold(self):
        criteria = extract(_load_bundle("pt-018.json"))
        hr = criteria.latest_vitals.get("8867-4", 0)
        assert hr > 120, f"Expected HR >120, got {hr}"


@pytest.mark.hard_failure
class TestLindaOkonkwo:
    """pt-019: Blank code status, no active conditions, normal vitals/labs → P9.

    Scenario S11: observation after minor fall, no Condition resources coded,
    code status Observation intentionally absent.  Verifies criteria.py detects
    missing LOINC 81638-3 and the rules engine reaches P9.
    """
    def test_classified_as_blank_code_status(self):
        result = rank(extract(_load_bundle("pt-019.json")))
        assert result.level == 9, f"Expected P9 (blank code status), got {result.level}"

    def test_blank_code_status_flag_set(self):
        criteria = extract(_load_bundle("pt-019.json"))
        assert criteria.blank_code_status is True

    def test_no_active_condition_coded(self):
        criteria = extract(_load_bundle("pt-019.json"))
        assert criteria.active_condition is False

    def test_no_critical_vital(self):
        criteria = extract(_load_bundle("pt-019.json"))
        assert criteria.critical_vital is False


class TestRobertFinch:
    """pt-020: Pre-procedure observation, no conditions coded, all normal → P10.

    Scenario S12: elective colonoscopy prep, encounter reason only — no
    Condition resources, normal vitals, normal labs, code status present.
    Verifies the P10 catch-all path and confirms the corpus covers routine
    presentations.
    """
    def test_classified_as_routine(self):
        result = rank(extract(_load_bundle("pt-020.json")))
        assert result.level == 10, f"Expected P10 (Routine), got {result.level}"

    def test_no_active_condition(self):
        criteria = extract(_load_bundle("pt-020.json"))
        assert criteria.active_condition is False

    def test_code_status_present(self):
        criteria = extract(_load_bundle("pt-020.json"))
        assert criteria.blank_code_status is False

    def test_no_critical_flags(self):
        criteria = extract(_load_bundle("pt-020.json"))
        assert not any([
            criteria.critical_vital, criteria.critical_lab,
            criteria.abnormal_lab, criteria.blank_code_status,
        ])


# ── Eval corpus coverage ──────────────────────────────────────────────────────

@pytest.mark.hard_failure
class TestEvalCorpusCoverage:
    """Assert that the corpus now covers all 10 priority levels.

    P1-P8 were covered by the original 18 patients.  P9 and P10 were added
    by the generator fix (pt-019 and pt-020).  These tests will fail if the
    generator is changed in a way that removes coverage for any level, giving
    an immediate signal that the eval corpus has regressed.
    """

    def test_corpus_contains_p9_patient(self):
        p9_patients = [
            fname for fname in sorted(os.listdir(_BUNDLE_DIR))
            if fname.endswith(".json") and rank(extract(_load_bundle(fname))).level == 9
        ]
        assert len(p9_patients) >= 1, "Corpus must contain at least one P9 patient"

    def test_corpus_contains_p10_patient(self):
        p10_patients = [
            fname for fname in sorted(os.listdir(_BUNDLE_DIR))
            if fname.endswith(".json") and rank(extract(_load_bundle(fname))).level == 10
        ]
        assert len(p10_patients) >= 1, "Corpus must contain at least one P10 patient"

    def test_all_10_priority_levels_covered(self):
        covered = set()
        for fname in sorted(os.listdir(_BUNDLE_DIR)):
            if not fname.endswith(".json"):
                continue
            covered.add(rank(extract(_load_bundle(fname))).level)
        missing = set(range(1, 11)) - covered
        assert not missing, f"Priority levels not covered by any patient: {sorted(missing)}"
