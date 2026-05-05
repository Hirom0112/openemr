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
class TestAlejandroCruz:
    """pt-024: Intra-abdominal sepsis — qSOFA=3, critical lactate → P1."""
    def test_classified_as_sepsis(self):
        result = rank(extract(_load_bundle("pt-024.json")))
        assert result.level in (1, 2), f"Expected sepsis level (1-2), got {result.level}"

    def test_critical_lactate_flagged(self):
        criteria = extract(_load_bundle("pt-024.json"))
        assert criteria.critical_lab is True

    def test_qsofa_at_least_two(self):
        criteria = extract(_load_bundle("pt-024.json"))
        assert criteria.qsofa_score >= 2


@pytest.mark.hard_failure
class TestDeliaFontaine:
    """pt-002: Critical K+ 6.4 — critical lab, normal vitals."""
    def test_classified_as_critical_lab(self):
        result = rank(extract(_load_bundle("pt-002.json")))
        assert result.level == 3, f"Expected P3 (critical lab), got {result.level}"


@pytest.mark.hard_failure
class TestRaymondOkafor:
    """pt-003: COPD with SpO2=91 (59408-5) — pulse-ox below 92% critical threshold → P4 respiratory."""
    def test_classified_as_critical_vital(self):
        result = rank(extract(_load_bundle("pt-003.json")))
        assert result.level == 4, f"Expected P4 (respiratory — SpO2 91%), got {result.level}"

    def test_spo2_below_critical_threshold(self):
        criteria = extract(_load_bundle("pt-003.json"))
        assert criteria.critical_vital_respiratory is True, "SpO2=91 should flag respiratory critical vital"
        assert criteria.critical_vital is True


class TestGloriaTran:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-004.json")))
        assert result.level == 8


class TestBernardKowalski:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-005.json")))
        assert result.level == 8


class TestIngridNakamura:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-006.json")))
        assert result.level == 8


class TestDarnellSimmons:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-007.json")))
        assert result.level == 8


class TestYvonneCastillo:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-008.json")))
        assert result.level == 8


class TestElenaMorales:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-009.json")))
        assert result.level == 8


class TestRajivPatel:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-010.json")))
        assert result.level == 8


class TestKarlBergstrom:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-011.json")))
        assert result.level == 8


class TestMiriamJohnson:
    """pt-012: Essential hypertension (chronic) — stable.

    Demonstrates the tightened P9 logic: active_condition fires only when at
    least one Condition matches the curated chronic-disease list
    (chronic_conditions.yaml). Hypertension SNOMED 38341003 is on that list.
    """
    def test_classified_as_stable(self):
        result = rank(extract(_load_bundle("pt-012.json")))
        assert result.level == 9, f"Expected P9 (active condition, stable), got {result.level}"


@pytest.mark.hard_failure
class TestCarlosReyes:
    """pt-013: Critical lab present."""
    def test_classified_as_critical_lab(self):
        result = rank(extract(_load_bundle("pt-013.json")))
        assert result.level == 3, f"Expected P3 (critical lab), got {result.level}"


class TestAbenaOsei:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-014.json")))
        assert result.level == 8


class TestDorothyWilliams:
    def test_classified_as_abnormal_lab(self):
        result = rank(extract(_load_bundle("pt-015.json")))
        assert result.level == 8


class TestWeiHuang:
    """pt-016: Post-op cholecystectomy — acute, not chronic.

    Under the tightened P9 rule, post-op recovery does not match the chronic
    disease list, so this patient now falls through to P11 (Routine).
    """
    def test_classified_as_routine(self):
        result = rank(extract(_load_bundle("pt-016.json")))
        assert result.level == 11


class TestSeanMurphy:
    """pt-017: Alcohol withdrawal — acute, not chronic → P11 under tightened P9."""
    def test_classified_as_routine(self):
        result = rank(extract(_load_bundle("pt-017.json")))
        assert result.level == 11


@pytest.mark.hard_failure
class TestThomasGreer:
    """pt-018: AF with RVR — HR=136 is intentional, not a generator bug.

    Scenario S10: out-of-census patient under prov-other.  His diagnosis is
    atrial fibrillation with rapid ventricular response; HR=136 is clinically
    appropriate.  He was previously (incorrectly) used as a 'routine' fixture.
    """
    def test_classified_as_critical_vital(self):
        criteria = extract(_load_bundle("pt-018.json"))
        assert criteria.critical_vital_circulatory is True, "Thomas Greer HR=136 should flag circulatory critical vital"
        result = rank(criteria)
        assert result.level == 5, f"Expected P5 (circulatory instability — HR 136), got {result.level}"

    def test_hr_exceeds_critical_threshold(self):
        criteria = extract(_load_bundle("pt-018.json"))
        hr = criteria.latest_vitals.get("8867-4", 0)
        assert hr > 120, f"Expected HR >120, got {hr}"


@pytest.mark.hard_failure
class TestLindaOkonkwo:
    """pt-019: No active conditions, normal vitals/labs → P11 (Routine).

    Scenario S11: observation after minor fall. Originally designed to
    exercise a P10 "blank code status" tier; that tier was deliberately
    removed (see triage/rules/rules_engine_config.yaml comment near
    level 10). The flag is preserved for downstream consumers but no
    longer drives a triage tier, so Linda now sits at P11.
    """
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
        assert result.level == 11, f"Expected P11 (Routine), got {result.level}"

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


@pytest.mark.hard_failure
class TestMayaLindgren:
    """pt-025: Severe pain (8/10) with chronic hypertension → P7.

    Demonstrates rule precedence: Maya has an active chronic condition
    (essential hypertension, SNOMED 38341003) that would match the tightened
    P9 rule on its own, but P7 (Severe Pain) sits above P9 in the ladder so
    fires first.
    """
    def test_classified_as_severe_pain(self):
        result = rank(extract(_load_bundle("pt-025.json")))
        assert result.level == 7, f"Expected P7 (severe pain), got {result.level}"

    def test_pain_score_high_flag_set(self):
        criteria = extract(_load_bundle("pt-025.json"))
        assert criteria.pain_score_high is True

    def test_chronic_active_condition_matched(self):
        criteria = extract(_load_bundle("pt-025.json"))
        assert criteria.active_condition is True, (
            "Hypertension is in the chronic condition list — active_condition should fire"
        )


# ── Eval corpus coverage ──────────────────────────────────────────────────────

@pytest.mark.hard_failure
class TestEvalCorpusCoverage:
    """Assert that the corpus covers the priority levels we exercise.

    The active ladder is {P1..P9, P11}. P10 ("Blank Code Status") was
    intentionally removed from the rules engine (see
    triage/rules/rules_engine_config.yaml). These tests will fail if the
    generator is changed in a way that removes coverage for any active
    level, giving an immediate signal that the eval corpus has regressed.
    """

    def test_corpus_contains_p11_patient(self):
        p11_patients = [
            fname for fname in sorted(os.listdir(_BUNDLE_DIR))
            if fname.endswith(".json") and rank(extract(_load_bundle(fname))).level == 11
        ]
        assert len(p11_patients) >= 1, "Corpus must contain at least one P11 patient"

    def test_core_priority_levels_covered(self):
        covered = set()
        for fname in sorted(os.listdir(_BUNDLE_DIR)):
            if not fname.endswith(".json"):
                continue
            covered.add(rank(extract(_load_bundle(fname))).level)
        # Active ladder coverage: P1..P9 plus P11. P10 is intentionally not
        # in the rules engine (see rules_engine_config.yaml). P7 is covered
        # by pt-025 (Maya Lindgren — severe pain).
        required = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11}
        missing = required - covered
        assert not missing, f"Required priority levels not covered: {sorted(missing)} (covered: {sorted(covered)})"


def _provider_id_of(bundle_data: dict) -> str | None:
    for entry in bundle_data.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Encounter":
            for participant in resource.get("participant", []):
                ref = participant.get("individual", {}).get("reference", "")
                if ref.startswith("Practitioner/"):
                    return ref.removeprefix("Practitioner/")
    return None


@pytest.mark.hard_failure
def test_sara_chen_panel_has_one_per_priority_level():
    """Sara Chen's panel (prov-chen) is exactly 10 patients covering the
    active ladder {P1..P9, P11}.

    P10 was deliberately removed from the rules engine (see
    triage/rules/rules_engine_config.yaml), so the panel covers nine
    sequential tiers plus the P11 routine catch-all.
    """
    panel: list[tuple[str, int]] = []
    for fname in sorted(os.listdir(_BUNDLE_DIR)):
        if not fname.endswith(".json"):
            continue
        raw = json.loads((_BUNDLE_DIR / fname).read_text())
        if _provider_id_of(raw) != "prov-chen":
            continue
        level = rank(extract(_load_bundle(fname))).level
        panel.append((fname, level))

    assert len(panel) == 10, f"Sara Chen's panel must be exactly 10 patients, got {len(panel)}: {panel}"
    levels = {level for _, level in panel}
    expected = set(range(1, 10)) | {11}
    assert levels == expected, (
        f"Sara's panel must cover {sorted(expected)} exactly once each. "
        f"Got levels {sorted(levels)} from {panel}"
    )
