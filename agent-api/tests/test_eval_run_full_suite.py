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


def test_run_full_suite_emits_json_and_markdown(tmp_path):
    fake_cases = [
        _FakeCase(case_id="c1", bucket="schema"),
        _FakeCase(case_id="c2", bucket="critic"),
    ]

    # Provide stub modules so run_full_suite's imports succeed even if the
    # parallel agents haven't landed their files yet.
    import types

    fixtures_pkg = sys.modules.setdefault("tests.fixtures", types.ModuleType("tests.fixtures"))
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
