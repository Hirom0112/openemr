"""Phase 4.6 — guard: every Anthropic ``messages.create(...)`` in production
code must pin ``temperature=0``.

Rationale
---------
Without an explicit ``temperature`` kwarg, the Anthropic SDK defaults to
``1.0``. For W1 dispatcher / W2 graph / extractor / rubric judge call sites
that surface clinical content, sampling variance is a non-determinism source
that masquerades as eval-pipeline regression and (worse) makes safety-critical
output non-reproducible. Phase 4.5 root-caused unexplained 4b→4c eval drift
to exactly this. See W1_ARCHITECTURE §4.4 for the broader "no free-text prose"
contract — the same integrity argument applies to sampling.

This test walks every production ``.py`` under ``agent-api/`` (excluding
``tests/``, ``.venv*``, and tooling/scripts that don't ship in production)
and AST-parses each ``messages.create(...)`` call. If any call site is
missing the ``temperature`` kwarg, the test fails listing the offending
``path:line`` so the next contributor can fix it before merge.

The check uses AST (not regex) so kwarg detection is robust to formatting
churn. We intentionally do not assert the *value* (``0``) — pinning anywhere
between 0.0 and 0.3 is "deterministic enough" for our use case, but the
operative requirement is that no site falls back to the SDK default of 1.0.
The test fails fast if the kwarg is omitted entirely.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

import pytest


_AGENT_API_ROOT = Path(__file__).resolve().parent.parent

# Skip these subtrees: not production code paths.
_EXCLUDED_PARTS = {
    "tests",
    "scripts",
    "evals",  # eval harness scaffolding — but we DO scan rubrics_llm.py below
}
# Specific eval-side production files that DO ship judge calls and must be
# pinned (the harness itself uses LLMs to score eval outputs).
_INCLUDED_FROM_EVALS: Tuple[str, ...] = ("rubrics_llm.py",)


def _iter_production_py_files() -> List[Path]:
    files: List[Path] = []
    for path in _AGENT_API_ROOT.rglob("*.py"):
        # Skip virtualenvs, caches, tooling.
        parts = set(path.relative_to(_AGENT_API_ROOT).parts)
        if any(p.startswith(".venv") or p == "__pycache__" for p in parts):
            continue
        if "tests" in parts:
            continue
        if "scripts" in parts:
            continue
        if "evals" in parts:
            # Re-include the rubric LLM judge file by name.
            if path.name in _INCLUDED_FROM_EVALS:
                files.append(path)
            continue
        files.append(path)
    return files


def _is_messages_create_call(node: ast.Call) -> bool:
    """True iff this ast.Call is ``something.messages.create(...)``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "create":
        return False
    obj = func.value
    if not isinstance(obj, ast.Attribute):
        return False
    return obj.attr == "messages"


def _find_unpinned_call_sites() -> List[Tuple[Path, int]]:
    """Return list of ``(path, lineno)`` for every messages.create call
    missing the ``temperature`` kwarg in production code."""
    offenders: List[Tuple[Path, int]] = []
    for path in _iter_production_py_files():
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            # Skip files we can't parse — they'll fail other checks.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_messages_create_call(node):
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "temperature" not in kw_names:
                offenders.append((path, node.lineno))
    return offenders


@pytest.mark.hard_failure
def test_every_messages_create_pins_temperature() -> None:
    """Phase 4.6 contract: no production messages.create may drift back to
    the SDK default temperature.

    If this test fails, add ``temperature=0`` to the listed call sites. Do
    not silence — a drifting temperature makes every downstream eval and
    every clinical surface non-deterministic in a way that's invisible at
    the call site.
    """
    offenders = _find_unpinned_call_sites()
    if offenders:
        bullet_list = "\n".join(
            f"  - {path.relative_to(_AGENT_API_ROOT)}:{lineno}"
            for path, lineno in offenders
        )
        pytest.fail(
            "Found production messages.create(...) call(s) missing "
            "`temperature=0`:\n"
            f"{bullet_list}\n"
            "Add `temperature=0` to each. See "
            "agent-api/tests/test_eval_temperature_pinned.py docstring "
            "for rationale."
        )


@pytest.mark.hard_failure
def test_scanner_detects_missing_kwarg_synthetic() -> None:
    """Self-test: feed the AST checker a synthetic snippet and assert it
    flags the missing-temperature call but accepts the pinned one. Catches
    accidental regressions in the detection logic itself.
    """
    src = (
        "async def f(client):\n"
        "    await client.messages.create(model='m', max_tokens=1)\n"
        "    await client.messages.create(model='m', max_tokens=1, temperature=0)\n"
    )
    tree = ast.parse(src)
    creates = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_messages_create_call(n)
    ]
    assert len(creates) == 2
    pinned = [
        ("temperature" in {kw.arg for kw in c.keywords if kw.arg is not None})
        for c in creates
    ]
    assert pinned == [False, True], (
        "AST scanner mis-classified pinned vs unpinned messages.create calls"
    )
