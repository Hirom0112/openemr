"""Tests for evals.diff_baseline gate logic."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hard_failure

REPO_AGENT_API = Path(__file__).resolve().parent.parent
DIFF_SCRIPT = REPO_AGENT_API / "evals" / "diff_baseline.py"
BASELINE = REPO_AGENT_API / "evals" / "baseline.json"


def _baseline_results() -> dict:
    """Synthetic results matching the committed baseline pass-rates exactly."""
    spec = json.loads(BASELINE.read_text())
    out: dict = {}
    for k, v in spec.items():
        if k == "critic_false_positive_rate":
            out[k] = 0.0
        elif k == "per_modality":
            # Wave 2C — nested per-modality block; not a pass_rate rubric.
            # Mirrors the skip in evals/diff_baseline.py.
            continue
        else:
            out[k] = v["pass_rate"]
    return out


def _run(results: dict | str, tmp_path: Path) -> subprocess.CompletedProcess:
    if isinstance(results, dict):
        path = tmp_path / "results.json"
        path.write_text(json.dumps(results))
    else:
        path = tmp_path / "results.json"
        path.write_text(results)
    return subprocess.run(
        [sys.executable, str(DIFF_SCRIPT), "--baseline", str(BASELINE), "--results", str(path)],
        capture_output=True,
        text=True,
    )


def test_baseline_equal_results_passes(tmp_path):
    results = _baseline_results()
    proc = _run(results, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "GATE: PASS" in proc.stdout


def test_drop_more_than_5pp_fails(tmp_path):
    results = _baseline_results()
    results["schema_valid"] = results["schema_valid"] - 0.06  # 6pp drop
    proc = _run(results, tmp_path)
    assert proc.returncode == 1
    assert "GATE: FAIL" in proc.stdout
    assert "schema_valid" in proc.stdout


def test_no_phi_in_logs_failure_is_absolute(tmp_path):
    results = _baseline_results()
    results["no_phi_in_logs"] = 0.99  # below the 1.00 floor
    proc = _run(results, tmp_path)
    assert proc.returncode == 1
    assert "no_phi_in_logs" in proc.stdout
    assert "GATE: FAIL" in proc.stdout


def test_critic_false_positive_above_max_fails(tmp_path):
    results = _baseline_results()
    # Pick a value strictly above whatever ``max`` the committed baseline
    # carries today — Phase 2 raised the cap from 0.02 to 0.1636, so a
    # hard-coded 0.03 no longer trips the gate. Compute it from the spec.
    spec = json.loads(BASELINE.read_text())
    cap = float(spec["critic_false_positive_rate"]["max"])
    results["critic_false_positive_rate"] = cap + 0.05
    proc = _run(results, tmp_path)
    assert proc.returncode == 1
    assert "critic_false_positive_rate" in proc.stdout
    assert "GATE: FAIL" in proc.stdout


def test_min_threshold_floor_fails(tmp_path):
    """Even when delta from baseline is small, observed below min_threshold fails."""
    results = _baseline_results()
    # factually_consistent baseline 0.94, min 0.85 — set 0.50 (huge drop, also below floor).
    # The test name says "even when baseline only dropped 4pp". The floor logic should
    # fail before delta logic, but to make this test specifically about the floor we
    # need a case where delta <=5pp but observed < min. baseline 0.94 → observed 0.90:
    # delta=4pp (within tol), but min is 0.85, so observed=0.90 actually passes. We need
    # a rubric whose min is close to baseline. correct_critic_decision: baseline 0.96,
    # min 0.90 — observed 0.92: delta 4pp (ok), 0.92 > 0.90 (ok). Still passes.
    # Solution: lower observed for safe_refusal: baseline 0.96, min 0.90 → set 0.89 with
    # baseline shifted... easier: monkey-tweak: just set correct_critic_decision = 0.89
    # which is delta=7pp AND below min. The point is min is checked.
    # To isolate "floor fails when delta within tolerance" — that combination is impossible
    # given the current baseline numbers (min always sits >5pp below baseline). Use a
    # custom baseline file for this test.
    custom = {
        "schema_valid": {"pass_rate": 1.0, "min_threshold": 0.98},
        "citation_present": {"pass_rate": 1.0, "min_threshold": 0.98},
        "correct_critic_decision": {"pass_rate": 0.96, "min_threshold": 0.90},
        # baseline only 4pp above min — drop of 4pp lands below min.
        "factually_consistent": {"pass_rate": 0.89, "min_threshold": 0.85},
        "safe_refusal": {"pass_rate": 0.96, "min_threshold": 0.90},
        "no_phi_in_logs": {"pass_rate": 1.0, "min_threshold": 1.0},
        "critic_false_positive_rate": {"max": 0.02},
    }
    custom_baseline = tmp_path / "baseline.json"
    custom_baseline.write_text(json.dumps(custom))
    results = {
        "schema_valid": 1.0,
        "citation_present": 1.0,
        "correct_critic_decision": 0.96,
        "factually_consistent": 0.84,  # delta 5pp — within tolerance, but below min 0.85
        "safe_refusal": 0.96,
        "no_phi_in_logs": 1.0,
        "critic_false_positive_rate": 0.0,
    }
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(results))
    proc = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT), "--baseline", str(custom_baseline), "--results", str(results_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout
    assert "factually_consistent" in proc.stdout
    assert "min_threshold" in proc.stdout or "min" in proc.stdout


def _per_modality_floor_baseline() -> dict:
    """Synthetic baseline carrying both global rubrics and per-modality pins."""
    return {
        "schema_valid": {"pass_rate": 0.95, "min_threshold": 0.85},
        "citation_present": {"pass_rate": 1.0, "min_threshold": 0.95},
        "correct_critic_decision": {"pass_rate": 0.96, "min_threshold": 0.90},
        "factually_consistent": {"pass_rate": 0.94, "min_threshold": 0.85},
        "safe_refusal": {"pass_rate": 0.96, "min_threshold": 0.90},
        "no_phi_in_logs": {"pass_rate": 1.0, "min_threshold": 1.0},
        "citation_row_match": {"pass_rate": 0.85, "min_threshold": 0.75},
        "critic_false_positive_rate": {"max": 0.10},
        "per_modality": {
            # Structurally-low pinned bucket: 0.333 schema_valid is well below
            # the global lenient floor (0.85 - 0.05 = 0.80). Pre-fix this
            # would FAIL even at delta=0; post-fix it must PASS.
            "typed_pdf": {
                "n_cases": 10,
                "schema_valid": 0.333,
                "citation_row_match": 0.727,
            },
            # Bucket present in observed but absent from per_modality_baseline
            # at this rubric — falls back to the global lenient floor.
            "scanned_pdf": {
                "n_cases": 8,
                "citation_row_match": 0.727,
            },
        },
    }


def _per_modality_floor_results_baseline_equal() -> dict:
    """Observed exactly matches the pinned per-bucket rates."""
    return {
        "schema_valid": 0.95,
        "citation_present": 1.0,
        "correct_critic_decision": 0.96,
        "factually_consistent": 0.94,
        "safe_refusal": 0.96,
        "no_phi_in_logs": 1.0,
        "citation_row_match": 0.85,
        "critic_false_positive_rate": 0.0,
        "per_modality": {
            "typed_pdf": {
                "n_cases": 10,
                "schema_valid": 0.333,
                "citation_row_match": 0.727,
            },
            "scanned_pdf": {
                "n_cases": 8,
                "citation_row_match": 0.727,
            },
        },
    }


def test_per_modality_pinned_below_global_floor_passes_at_delta_zero(tmp_path):
    """Bucket pinned below global lenient floor must PASS when delta=0.

    Regression test for the bug where the lenient_floor was computed solely
    from the GLOBAL rubric min_threshold (minus 5pp), causing structurally-
    low pinned buckets (e.g. typed_pdf.schema_valid=0.333) to FAIL even when
    observed exactly equals the pinned baseline rate.
    """
    baseline = _per_modality_floor_baseline()
    results = _per_modality_floor_results_baseline_equal()
    baseline_path = tmp_path / "baseline.json"
    results_path = tmp_path / "results.json"
    baseline_path.write_text(json.dumps(baseline))
    results_path.write_text(json.dumps(results))
    proc = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT), "--baseline", str(baseline_path), "--results", str(results_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "GATE: PASS" in proc.stdout
    # Sanity: ensure the bucket actually appeared in the table — i.e. the
    # gate logic did execute against it.
    assert "typed_pdf.schema_valid" in proc.stdout


def test_per_modality_drop_below_pinned_minus_5pp_fails(tmp_path):
    """Bucket observed < bucket pinned - 5pp must FAIL the per-bucket floor."""
    baseline = _per_modality_floor_baseline()
    results = _per_modality_floor_results_baseline_equal()
    # typed_pdf schema_valid pinned at 0.333; lenient floor = 0.283.
    # 0.27 is below the per-bucket lenient floor (and the bucket-delta is
    # 0.063 ≈ 6.3pp, also below the 8pp regression limit, so this asserts
    # the FLOOR fires, not the regression check). Also avoids the 8pp
    # bucket-delta regression check tripping (which would mask the floor).
    results["per_modality"]["typed_pdf"]["schema_valid"] = 0.27
    baseline_path = tmp_path / "baseline.json"
    results_path = tmp_path / "results.json"
    baseline_path.write_text(json.dumps(baseline))
    results_path.write_text(json.dumps(results))
    proc = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT), "--baseline", str(baseline_path), "--results", str(results_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout
    assert "GATE: FAIL" in proc.stdout
    assert "typed_pdf" in proc.stdout
    assert "schema_valid" in proc.stdout
    assert "lenient_floor" in proc.stdout


def test_per_modality_missing_bucket_falls_back_to_global_floor(tmp_path):
    """Bucket without per_modality entry falls back to the global lenient floor."""
    baseline = _per_modality_floor_baseline()
    results = _per_modality_floor_results_baseline_equal()
    # Add a NEW observed bucket not in per_modality_baseline at all. Its
    # schema_valid floor must come from the global rubric (0.85 - 0.05 = 0.80).
    # Set observed = 0.79 (below global lenient floor) — must FAIL.
    results["per_modality"]["unknown_modality"] = {
        "n_cases": 10,
        "schema_valid": 0.79,
    }
    baseline_path = tmp_path / "baseline.json"
    results_path = tmp_path / "results.json"
    baseline_path.write_text(json.dumps(baseline))
    results_path.write_text(json.dumps(results))
    proc = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT), "--baseline", str(baseline_path), "--results", str(results_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout
    assert "GATE: FAIL" in proc.stdout
    assert "unknown_modality" in proc.stdout
    # The reported floor must be the global lenient floor (0.80), not a
    # per-bucket value — the bucket has no baseline entry.
    assert "0.800" in proc.stdout


def test_malformed_results_json_fails(tmp_path):
    proc = _run("{not valid json", tmp_path)
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "GATE: FAIL" in combined or "ERROR" in combined


# ── failing_only mode ────────────────────────────────────────────────────


def test_failing_only_skipped_passes(tmp_path):
    """Stub artifact from short-circuit path — no rubrics to gate."""
    proc = _run(
        {"_mode": "failing_only_skipped", "_note": "no prior failures", "no_phi_in_logs": 1.0},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "GATE: PASS" in proc.stdout


def test_failing_only_drops_pass_rate_gate(tmp_path):
    """Rubric pass-rates well below floor should still PASS in failing_only
    mode — only ABSOLUTE_RUBRICS gate."""
    results = _baseline_results()
    results["_mode"] = "failing_only"
    # Drop several rubrics far below floor — would normally hard-fail.
    results["schema_valid"] = 0.05
    results["citation_resolvable"] = 0.10
    results["correct_critic_decision"] = 0.0
    proc = _run(results, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "GATE: PASS" in proc.stdout
    # Subset rubrics should be reported as INFO, not FAIL.
    assert "INFO" in proc.stdout


def test_failing_only_still_enforces_no_phi_in_logs(tmp_path):
    """ABSOLUTE_RUBRICS gate even in failing_only mode."""
    results = _baseline_results()
    results["_mode"] = "failing_only"
    results["no_phi_in_logs"] = 0.99
    proc = _run(results, tmp_path)
    assert proc.returncode == 1
    assert "no_phi_in_logs" in proc.stdout
    assert "GATE: FAIL" in proc.stdout
