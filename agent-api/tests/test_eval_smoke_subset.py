"""Tests for the evals._smoke_subset deterministic case selection.

Contracts:
  1. SMOKE_CASE_IDS has exactly 10 entries.
  2. Every ID in SMOKE_CASE_IDS exists in the full CASES list.
  3. At least one ID per document_modality bucket present in the corpus.
  4. --smoke flag wires through run_full_suite.main and emits the startup
     log line "eval running in SMOKE mode, n=10 cases".
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.hard_failure


# ---------------------------------------------------------------------------
# Import the module under test (isolated — no real agent stack needed).
# ---------------------------------------------------------------------------


def _get_smoke_ids() -> tuple[str, ...]:
    from evals._smoke_subset import SMOKE_CASE_IDS
    return SMOKE_CASE_IDS


def _get_full_cases():
    from tests.fixtures.w2_eval_cases import CASES
    return CASES


# ---------------------------------------------------------------------------
# Contract 1: exactly 10 entries
# ---------------------------------------------------------------------------


def test_smoke_case_ids_length():
    ids = _get_smoke_ids()
    assert len(ids) == 10, f"Expected 10 smoke cases, got {len(ids)}: {ids}"


# ---------------------------------------------------------------------------
# Contract 2: every ID exists in CASES
# ---------------------------------------------------------------------------


def test_smoke_case_ids_all_exist_in_cases():
    ids = _get_smoke_ids()
    cases = _get_full_cases()
    case_ids_set = {c.case_id for c in cases}
    missing = [cid for cid in ids if cid not in case_ids_set]
    assert not missing, f"Smoke IDs not found in CASES: {missing}"


# ---------------------------------------------------------------------------
# Contract 3: at least one ID per document_modality bucket
# ---------------------------------------------------------------------------


def test_smoke_case_ids_cover_all_modalities():
    """Every modality bucket that exists in the full corpus must have at least
    one representative in SMOKE_CASE_IDS.

    ``unknown`` modality cases (blank / encrypted / empty_stream) are
    intentionally excluded from smoke — they require no LLM calls and are
    the cheapest cases to run; spending one of the 10 smoke slots on them
    would waste coverage budget. So ``unknown`` is exempt from this check.
    """
    ids = _get_smoke_ids()
    cases = _get_full_cases()

    # Build modality → case_id map for the smoke set.
    case_by_id = {c.case_id: c for c in cases}
    smoke_modalities = {
        case_by_id[cid].document_modality
        for cid in ids
        if cid in case_by_id
    }

    # Build full modality set (excluding ``unknown``).
    all_modalities = {
        c.document_modality
        for c in cases
        if c.document_modality != "unknown"
    }

    missing = all_modalities - smoke_modalities
    assert not missing, (
        f"Modalities present in corpus but missing from smoke subset: {missing}\n"
        f"Smoke modalities covered: {smoke_modalities}"
    )


# ---------------------------------------------------------------------------
# Contract 4: --smoke flag wires through run_full_suite.main
# ---------------------------------------------------------------------------


@dataclass
class _FakeCase:
    case_id: str
    bucket: str
    document_modality: str = "typed_pdf"


@dataclass
class _FakeOutcome:
    raw: str = ""


@dataclass
class _FakeScore:
    case_id: str
    status: str = "pass"
    notes: str = ""
    schema_valid: bool = True
    citation_present: bool = True
    citation_resolvable: bool = True
    citation_row_match: bool = True
    citation_token_match: bool = True
    correct_critic_decision: bool = True
    no_phi_in_logs: bool = True
    factually_consistent: bool = True
    safe_refusal: bool = True
    provenance_chain: bool = True
    is_critic_false_positive: bool = False


def _fake_aggregate(_scores):
    return {
        "schema_valid": 1.0,
        "citation_present": 1.0,
        "citation_resolvable": 1.0,
        "citation_row_match": 1.0,
        "citation_token_match": 1.0,
        "correct_critic_decision": 1.0,
        "factually_consistent": 1.0,
        "safe_refusal": 1.0,
        "no_phi_in_logs": 1.0,
        "provenance_chain": 1.0,
        "critic_false_positive_rate": 0.0,
        "keyword_match_in_citation": 1.0,
    }


@pytest.fixture
def _isolated_sys_modules():
    """Snapshot + restore sys.modules entries this test file fakes, so its
    fakes don't leak into test_nearest_label_grounded_wiring (which imports
    evals.rubrics_llm → from .runner import RunOutcome and falls over when
    runner is still our fake)."""
    keys = (
        "tests.fixtures",
        "tests.fixtures.w2_eval_cases",
        "evals.runner",
        "evals.scoring",
        "evals.run_full_suite",
    )
    saved: dict = {k: sys.modules.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _install_fake_modules_for_smoke():
    """Stub the full CASES list + runner + scoring so run_full_suite
    can execute with --smoke in isolation. Fakes accept ``**kwargs`` because
    the cache wrapper passes ``cache=`` and ``cache_mode=`` through."""
    from tests.fixtures.w2_eval_cases import CASES as real_cases

    # Use the real CASES so --smoke can look up SMOKE_CASE_IDS in them.
    fixtures_pkg = sys.modules.setdefault(
        "tests.fixtures", types.ModuleType("tests.fixtures")
    )
    w2_mod = types.ModuleType("tests.fixtures.w2_eval_cases")
    w2_mod.CASES = real_cases
    sys.modules["tests.fixtures.w2_eval_cases"] = w2_mod

    runner_mod = types.ModuleType("evals.runner")
    runner_mod.run_case = lambda case, fixtures_root=None, **_kw: _FakeOutcome()

    def _resolve_fixture_path(_key, root):
        return Path(root) / "missing.jpg"

    runner_mod.resolve_fixture_path = _resolve_fixture_path
    sys.modules["evals.runner"] = runner_mod

    scoring_mod = types.ModuleType("evals.scoring")
    scoring_mod.score_case = lambda case, outcome, **_kw: _FakeScore(case_id=case.case_id)
    scoring_mod.aggregate = _fake_aggregate
    sys.modules["evals.scoring"] = scoring_mod
    sys.modules.pop("evals.run_full_suite", None)


def _stub_bbox_rubrics():
    from evals import run_full_suite as _rfs

    def _empty(_cases, _outcomes, _root):
        return (
            {
                "citation_iou": {"pass_rate": None, "n_evaluated": 0},
                "citation_pixel_distance": {
                    "mean_px": None,
                    "n_evaluated": 0,
                    "info_only": True,
                },
            },
            {},
        )

    _rfs._score_bbox_rubrics = _empty  # type: ignore[attr-defined]


def test_smoke_flag_runs_only_smoke_cases_and_emits_log(tmp_path, capsys, _isolated_sys_modules):
    _install_fake_modules_for_smoke()

    from evals import run_full_suite
    _stub_bbox_rubrics()

    out_json = tmp_path / "results.json"
    out_md = tmp_path / "results.md"

    rc = run_full_suite.main([
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(tmp_path),
        "--smoke",
    ])
    assert rc == 0, "run_full_suite.main returned non-zero with --smoke"

    captured = capsys.readouterr()
    assert "SMOKE mode" in captured.out, (
        f"Expected startup log line with 'SMOKE mode' in stdout, got: {captured.out!r}"
    )
    assert "n=10" in captured.out, (
        f"Expected 'n=10' in startup log line, got: {captured.out!r}"
    )

    data = json.loads(out_json.read_text())
    assert "schema_valid" in data


def test_smoke_flag_overrides_max_cases(tmp_path, capsys, _isolated_sys_modules):
    """When both --smoke and --max-cases are passed, --smoke wins."""
    _install_fake_modules_for_smoke()

    from evals import run_full_suite
    _stub_bbox_rubrics()

    out_json = tmp_path / "results.json"
    out_md = tmp_path / "results.md"

    rc = run_full_suite.main([
        "--output", str(out_json),
        "--md", str(out_md),
        "--fixtures-root", str(tmp_path),
        "--smoke",
        "--max-cases", "3",
    ])
    assert rc == 0

    captured = capsys.readouterr()
    # Smoke mode was active — 10 cases, not 3.
    assert "SMOKE mode" in captured.out
    assert "n=10" in captured.out
