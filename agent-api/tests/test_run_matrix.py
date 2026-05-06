"""Tests for evals.run_matrix — combo generator + dispatch logic.

Subprocess invocations are mocked; we never actually call run_full_suite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.hard_failure


def test_curated_combos_are_deterministic():
    from evals.run_matrix import CURATED, _resolve_combos
    a = _resolve_combos("curated")
    b = _resolve_combos("curated")
    assert [c.name for c in a] == [c.name for c in b]
    assert a == list(CURATED)


def test_curated_combos_exercise_each_axis_at_least_once():
    """Every flag axis should be flipped away from default in at least one combo."""
    from evals.run_matrix import CURATED, FLAG_AXES
    flipped: dict[str, set[str]] = {axis: set() for axis in FLAG_AXES}
    for combo in CURATED:
        for axis, value in combo.env.items():
            flipped[axis].add(value)
    for axis in FLAG_AXES:
        # baseline value is the default; we want at least one non-default value.
        non_default = set(FLAG_AXES[axis].values()) - {next(iter(FLAG_AXES[axis].values()))}
        assert flipped[axis] >= non_default, (
            f"Axis {axis} not flipped to non-default in any curated combo"
        )


def test_full_combos_cover_cartesian_product():
    from evals.run_matrix import FLAG_AXES, _full_combos
    expected = 1
    for axis in FLAG_AXES:
        expected *= len(FLAG_AXES[axis])
    combos = _full_combos()
    assert len(combos) == expected
    assert len({c.name for c in combos}) == expected  # distinct names


def test_dry_run_does_not_invoke_subprocess(tmp_path, capsys):
    from evals import run_matrix
    with patch("evals.run_matrix._real_runner") as mocked:
        rc = run_matrix.main([
            "--combos", "curated",
            "--output", str(tmp_path),
            "--dry-run",
        ])
    assert rc == 0
    mocked.assert_not_called()
    captured = capsys.readouterr().out
    assert "baseline" in captured
    assert "+paddle" in captured


def test_run_one_collects_rates_from_child_output(tmp_path):
    from evals.run_matrix import Combo, _run_one

    fake_results = {
        "schema_valid": 0.85,
        "citation_resolvable": 0.96,
        "citation_row_match": 0.93,
        "citation_token_match": 0.93,
        "correct_critic_decision": 0.40,
        "factually_consistent": 0.99,
        "no_phi_in_logs": 1.0,
        "provenance_chain": 1.0,
    }

    def _fake_runner(cmd, env):
        # Find the --output arg and write fake_results there.
        out_idx = cmd.index("--output") + 1
        Path(cmd[out_idx]).write_text(json.dumps(fake_results))
        return subprocess.CompletedProcess(cmd, 0)

    result = _run_one(
        Combo("baseline", {}),
        output_root=tmp_path,
        smoke=True,
        cache_mode="readwrite",
        runner=_fake_runner,
    )
    assert result["name"] == "baseline"
    assert result["returncode"] == 0
    for k, v in fake_results.items():
        assert result["rates"][k] == pytest.approx(v)


def test_markdown_ranks_by_mean_pass_rate():
    from evals.run_matrix import _markdown

    rows = [
        {"name": "low", "env": {}, "rates": {"schema_valid": 0.1, "no_phi_in_logs": 0.1}, "returncode": 0},
        {"name": "high", "env": {}, "rates": {"schema_valid": 0.9, "no_phi_in_logs": 0.9}, "returncode": 0},
        {"name": "mid", "env": {}, "rates": {"schema_valid": 0.5, "no_phi_in_logs": 0.5}, "returncode": 0},
    ]
    md = _markdown(rows)
    high_pos = md.index("| high |")
    mid_pos = md.index("| mid |")
    low_pos = md.index("| low |")
    assert high_pos < mid_pos < low_pos
