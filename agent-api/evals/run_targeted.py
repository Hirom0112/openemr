"""Targeted eval driver — run a hand-picked subset of cases.

Iterating on critic policy doesn't need a 156-case full-suite spend.
Pass case IDs (or modalities) on the CLI; we run only those, score
``correct_critic_decision`` against expected, and print a
case-by-case table plus per-modality summary.

Usage:
    python3.12 evals/run_targeted.py --cases bbox_gt_photo_001 lab_nominal_011_lipid_repeat
    python3.12 evals/run_targeted.py --modalities photo_capture scanned_pdf tiff_fax

Outputs to stdout. No JSON / Markdown writes. No baseline diff.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Eval defaults expected by the runner
os.environ.setdefault("EVAL_VERIFY_CITATIONS", "off")

from evals.runner import run_case  # noqa: E402
from tests.fixtures.w2_eval_cases import CASES  # noqa: E402

FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"


def select_cases(case_ids: List[str], modalities: List[str]) -> List[Any]:
    by_id = {c.case_id: c for c in CASES}
    out: List[Any] = []
    seen: set[str] = set()
    for cid in case_ids:
        if cid in by_id and cid not in seen:
            out.append(by_id[cid])
            seen.add(cid)
    if modalities:
        modality_set = set(modalities)
        for c in CASES:
            mod = getattr(c, "document_modality", None)
            if mod in modality_set and c.case_id not in seen:
                out.append(c)
                seen.add(c.case_id)
    return out


async def run_one(case: Any) -> dict:
    outcome = await run_case(case, fixtures_root=FIXTURES_ROOT)
    expected = case.expected_critic_decision
    actual = outcome.critic_decision
    return {
        "case_id": case.case_id,
        "modality": getattr(case, "document_modality", None),
        "expected": expected,
        "actual": actual,
        "correct": expected == actual,
        "soft_warns": [s.get("code") for s in (outcome.soft_warns or [])],
        "extraction_kind": (
            outcome.extraction.get("kind") if isinstance(outcome.extraction, dict) else None
        ),
    }


async def main_async(args: argparse.Namespace) -> int:
    cases = select_cases(args.cases or [], args.modalities or [])
    if not cases:
        print("No cases selected. Use --cases or --modalities.", file=sys.stderr)
        return 2
    print(f"Running {len(cases)} cases...")
    results = await asyncio.gather(*(run_one(c) for c in cases))

    # Per-case table
    print()
    print(f"{'case_id':<55} {'modality':<16} {'exp':<10} {'act':<10} {'ok':<3} {'kind':<14} sw")
    print("-" * 120)
    for r in results:
        ok = "✓" if r["correct"] else "✗"
        sw_str = ",".join(r["soft_warns"]) if r["soft_warns"] else "-"
        print(
            f"{r['case_id']:<55} {(r['modality'] or '-'):<16} "
            f"{(r['expected'] or '-'):<10} {(r['actual'] or '-'):<10} "
            f"{ok:<3} {(r['extraction_kind'] or '-'):<14} {sw_str}"
        )

    # Per-modality summary
    print()
    by_mod: dict[str, dict[str, int]] = {}
    for r in results:
        m = r["modality"] or "?"
        b = by_mod.setdefault(m, {"n": 0, "correct": 0})
        b["n"] += 1
        if r["correct"]:
            b["correct"] += 1
    print(f"{'modality':<16} {'correct':>10}/{'n':<5}")
    for m, b in sorted(by_mod.items()):
        rate = (b["correct"] / b["n"] * 100) if b["n"] else 0.0
        print(f"{m:<16} {b['correct']:>10}/{b['n']:<5}  ({rate:.1f}%)")

    total_correct = sum(1 for r in results if r["correct"])
    print(f"\nTOTAL correct: {total_correct}/{len(results)} ({total_correct/len(results)*100:.1f}%)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cases", nargs="*", help="Case IDs to run")
    p.add_argument("--modalities", nargs="*", help="Modalities to run all cases of")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
