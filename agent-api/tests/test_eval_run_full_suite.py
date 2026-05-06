"""Tests for evals.run_full_suite entrypoint."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.hard_failure


@dataclass
class _FakeCase:
    case_id: str
    bucket: str


@dataclass
class _FakeOutcome:
    raw: str = ""


@dataclass
class _FakeScore:
    case_id: str
    status: str = "pass"
    notes: str = ""


def _fake_aggregate(_scores):
    return {
        "schema_valid": 1.0,
        "citation_present": 1.0,
        "correct_critic_decision": 0.96,
        "factually_consistent": 0.94,
        "safe_refusal": 0.96,
        "no_phi_in_logs": 1.0,
        "provenance_chain": 1.0,
        "critic_false_positive_rate": 0.0,
    }


@pytest.fixture
def _isolated_sys_modules():
    """Snapshot sys.modules entries we plan to fake, restore on teardown.

    Without this, the fake stubs below leak across tests in the same pytest
    session and break test_eval_smoke_subset / test_eval_parallelization /
    test_nearest_label_grounded_wiring (they import the same names and get
    our fakes instead of the real modules).
    """
    keys = (
        "tests.fixtures",
        "tests.fixtures.w2_eval_cases",
        "evals.runner",
        "evals.scoring",
        "evals.rubrics_mechanical",
        "evals.rubrics_llm",
        "evals.run_full_suite",
    )
    saved: dict[str, object] = {k: sys.modules.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_run_full_suite_emits_json_and_markdown(tmp_path, _isolated_sys_modules):
    fake_cases = [
        _FakeCase(case_id="c1", bucket="schema"),
        _FakeCase(case_id="c2", bucket="critic"),
    ]

    # Provide stub modules so run_full_suite's imports succeed even if the
    # parallel agents haven't landed their files yet. The
    # _isolated_sys_modules fixture restores the real ones on teardown.
    import types

    sys.modules.setdefault("tests.fixtures", types.ModuleType("tests.fixtures"))
    w2_mod = types.ModuleType("tests.fixtures.w2_eval_cases")
    w2_mod.CASES = fake_cases
    sys.modules["tests.fixtures.w2_eval_cases"] = w2_mod

    runner_mod = types.ModuleType("evals.runner")
    runner_mod.run_case = lambda case, *args, **kwargs: _FakeOutcome()
    runner_mod.resolve_fixture_path = lambda key, root: Path(root) / f"{key}.bin"
    sys.modules["evals.runner"] = runner_mod

    scoring_mod = types.ModuleType("evals.scoring")
    scoring_mod.score_case = lambda case, outcome: _FakeScore(case_id=case.case_id)
    scoring_mod.aggregate = _fake_aggregate
    sys.modules["evals.scoring"] = scoring_mod

    rubrics_mech_mod = types.ModuleType("evals.rubrics_mechanical")
    rubrics_mech_mod.citation_iou = lambda *a, **kw: False
    rubrics_mech_mod.citation_pixel_distance = lambda *a, **kw: None
    sys.modules["evals.rubrics_mechanical"] = rubrics_mech_mod

    rubrics_llm_mod = types.ModuleType("evals.rubrics_llm")
    rubrics_llm_mod.nearest_label_grounded = lambda *a, **kw: None
    sys.modules["evals.rubrics_llm"] = rubrics_llm_mod

    # Force re-import of run_full_suite under the fakes.
    sys.modules.pop("evals.run_full_suite", None)

    out_json = tmp_path / "results.json"
    out_md = tmp_path / "results.md"

    from evals import run_full_suite

    rc = run_full_suite.main([
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(tmp_path),
    ])
    assert rc == 0
    assert out_json.exists()
    assert out_md.exists()

    data = json.loads(out_json.read_text())
    for k in (
        "schema_valid",
        "citation_present",
        "correct_critic_decision",
        "factually_consistent",
        "safe_refusal",
        "no_phi_in_logs",
        "provenance_chain",
        "critic_false_positive_rate",
    ):
        assert k in data, f"missing rubric {k}"

    md = out_md.read_text()
    assert "W2 Eval Suite" in md
    assert "c1" in md and "c2" in md


# ── --failing-only mode ─────────────────────────────────────────────────


def test_load_failing_case_ids_extracts_failures():
    """The helper should pick up cases with any False rubric, ERROR status,
    or explicit error string."""
    from evals.run_full_suite import _load_failing_case_ids

    import tempfile
    payload = {
        "_mode": "full",
        "_case_rows": [
            {"case_id": "c1", "status": "OK", "rubric_results": {"schema_valid": True}},
            {"case_id": "c2", "status": "OK", "rubric_results": {"schema_valid": False}},
            {"case_id": "c3", "status": "ERROR", "error": "boom", "rubric_results": {}},
            {"case_id": "c4", "rubric_results": {"a": True, "b": False}},
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = Path(f.name)
    try:
        assert _load_failing_case_ids(path) == {"c2", "c3", "c4"}
    finally:
        path.unlink(missing_ok=True)


def test_load_failing_case_ids_handles_missing_or_malformed():
    """Missing file / malformed JSON / unexpected shape return empty set."""
    from evals.run_full_suite import _load_failing_case_ids

    import tempfile
    # Missing file.
    assert _load_failing_case_ids(Path("/tmp/does_not_exist_xyz_abc.json")) == set()
    # Malformed JSON.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{not json")
        path = Path(f.name)
    try:
        assert _load_failing_case_ids(path) == set()
    finally:
        path.unlink(missing_ok=True)
    # Unexpected shape.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"unexpected": "shape"}, f)
        path = Path(f.name)
    try:
        assert _load_failing_case_ids(path) == set()
    finally:
        path.unlink(missing_ok=True)


def test_failing_only_short_circuits_when_no_failures(tmp_path):
    """When the prior artifact has zero failing cases, the runner skips the
    case loop entirely and writes a stub JSON tagged failing_only_skipped."""
    from evals import run_full_suite

    # Prior artifact with all rubrics passing.
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({
        "_mode": "full",
        "_case_rows": [
            {"case_id": "c1", "status": "OK", "rubric_results": {"schema_valid": True}},
            {"case_id": "c2", "status": "OK", "rubric_results": {"schema_valid": True}},
        ],
    }))

    out_json = tmp_path / "results.json"
    out_md = tmp_path / "results.md"
    rc = run_full_suite.main([
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(tmp_path),
        "--failing-only", str(prior),
    ])
    assert rc == 0
    data = json.loads(out_json.read_text())
    assert data["_mode"] == "failing_only_skipped"
    assert data["no_phi_in_logs"] == 1.0
    md = out_md.read_text()
    assert "failing-only mode (skipped)" in md.lower() or "skipped" in md.lower()
