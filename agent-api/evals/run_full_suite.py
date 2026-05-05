"""Run the full W2 50-case eval suite and emit JSON + Markdown results.

Outputs:
  --output  : JSON file consumable by diff_baseline.py
  --md      : Markdown file with per-case status + per-rubric pass-rates
              (defaults next to --output, replacing .json with .md)

Wires the contracted public APIs:
  tests.fixtures.w2_eval_cases.CASES
  evals.runner.run_case
  evals.scoring.score_case
  evals.scoring.aggregate
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

# Allow running both via `python3 -m evals.run_full_suite` (cwd=agent-api)
# and directly. Tests patch the module-level symbols, so import lazily inside main.

REPO_AGENT_API = Path(__file__).resolve().parent.parent


def _serialize(obj):
    if is_dataclass(obj):
        return asdict(obj)
    return obj


def _markdown_report(case_rows: list[dict], aggregates: dict) -> str:
    lines: list[str] = []
    lines.append("# W2 Eval Suite Results")
    lines.append("")
    lines.append("## Per-rubric pass rates")
    lines.append("")
    lines.append("| rubric | rate |")
    lines.append("| --- | --- |")
    for k, v in aggregates.items():
        try:
            lines.append(f"| {k} | {float(v) * 100:.1f}% |")
        except (TypeError, ValueError):
            lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## Per-case results")
    lines.append("")
    lines.append("| case_id | bucket | status | notes |")
    lines.append("| --- | --- | --- | --- |")
    for row in case_rows:
        lines.append(
            f"| {row.get('case_id', '?')} | {row.get('bucket', '?')} | "
            f"{row.get('status', '?')} | {row.get('notes', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


async def _run_async(args: argparse.Namespace) -> tuple[list[dict], list[Any]]:
    # Lazy imports — let tests patch these.
    from tests.fixtures.w2_eval_cases import CASES  # type: ignore
    from evals.runner import run_case  # type: ignore
    from evals.scoring import aggregate, score_case  # type: ignore

    case_rows: list[dict] = []
    scores: list[Any] = []
    for case in CASES:
        try:
            outcome = await run_case(case, fixtures_root=args.fixtures_root)
            score = await score_case(case, outcome)
            scores.append(score)
            score_d = _serialize(score)
            case_d = _serialize(case)
            status = score_d.get("status") if isinstance(score_d, dict) else "?"
            case_rows.append({
                "case_id": (case_d.get("case_id") if isinstance(case_d, dict) else getattr(case, "case_id", "?")),
                "bucket": (case_d.get("bucket") if isinstance(case_d, dict) else getattr(case, "bucket", "?")),
                "status": status if status is not None else "scored",
                "notes": (score_d.get("notes", "") if isinstance(score_d, dict) else ""),
            })
        except Exception as e:  # never blow up the run; record the error
            case_rows.append({
                "case_id": getattr(case, "case_id", "?"),
                "bucket": getattr(case, "bucket", "?"),
                "status": "ERROR",
                "notes": str(e),
            })

    return case_rows, scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Path to JSON results")
    parser.add_argument("--md", type=Path, default=None, help="Path to Markdown report")
    parser.add_argument("--fixtures-root", type=Path, default=REPO_AGENT_API / "tests" / "fixtures" / "eval")
    args = parser.parse_args(argv)

    md_path = args.md or args.output.with_suffix(".md")

    import asyncio
    case_rows, scores = asyncio.run(_run_async(args))

    from evals.scoring import aggregate  # type: ignore
    agg = aggregate(scores)

    # Ensure the JSON contains all six rubric pass-rates + critic_false_positive_rate.
    expected_keys = (
        "schema_valid",
        "citation_present",
        "correct_critic_decision",
        "factually_consistent",
        "safe_refusal",
        "no_phi_in_logs",
        "provenance_chain",
        "critic_false_positive_rate",
        "keyword_match_in_citation",
    )
    results: dict[str, float] = {}
    for k in expected_keys:
        v = agg.get(k)
        results[k] = float(v) if v is not None else 0.0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_markdown_report(case_rows, results))

    print(f"Wrote {args.output} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
