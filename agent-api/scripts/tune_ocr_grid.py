"""Wave 2D — offline tesseract PSM/DPI grid runner.

Sweeps PSM ∈ {3, 4, 6} × DPI ∈ {200, 300, 400} = 9 cells. For each cell we
override ``settings.tesseract_psm`` / ``settings.tesseract_dpi`` in-process
and run the eval suite (or a fast subset). We then tabulate the per-modality
pass-rate for each cell and pick the cell that maximises the *worst*
modality bucket (NOT the global mean — a cell that helps typed_pdf but tanks
handwritten is a Pareto loss, even if average improves).

If NO grid cell improves the worst bucket vs the (3, 300) baseline by ≥3pp,
we print "NO PARETO WINNER — keep defaults" and exit 0. The script never
mutates ``config.py`` or ``.env``; it only reports.

Outputs:
  * Markdown table to stdout.
  * ``tune_results.json`` artifact next to this script (overridable).

Usage:
  python3 scripts/tune_ocr_grid.py                         # full eval suite
  python3 scripts/tune_ocr_grid.py --rubrics-only          # mechanical only,
                                                            # no LLM judge
  python3 scripts/tune_ocr_grid.py --case-limit 10         # fast subset
  python3 scripts/tune_ocr_grid.py --psm 3 4 6 --dpi 200 300 400
  python3 scripts/tune_ocr_grid.py --output /tmp/grid.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Allow running as ``python3 scripts/tune_ocr_grid.py`` from agent-api/.
_AGENT_API = Path(__file__).resolve().parents[1]
if str(_AGENT_API) not in sys.path:
    sys.path.insert(0, str(_AGENT_API))


# Mechanical rubrics that depend on OCR extraction quality. These are the
# only rubrics we score in --rubrics-only mode (no LLM judge).
_MECHANICAL_RUBRICS: tuple[str, ...] = (
    "schema_valid",
    "citation_present",
    "citation_resolvable",
    "citation_row_match",
    "citation_token_match",
    "no_phi_in_logs",
)
# Full rubric set when LLM judges are available.
_ALL_RUBRICS: tuple[str, ...] = _MECHANICAL_RUBRICS + (
    "correct_critic_decision",
    "factually_consistent",
    "safe_refusal",
)

# Grid axes. Tweakable via CLI flags but kept narrow on purpose — wider
# sweeps belong in a dedicated paramsearch run, not in the standing eval.
_DEFAULT_PSM_VALUES: tuple[int, ...] = (3, 4, 6)
_DEFAULT_DPI_VALUES: tuple[int, ...] = (200, 300, 400)

# Pareto threshold — a non-default cell must improve the worst-bucket
# pass-rate by at least this many percentage points to be recommended.
_PARETO_THRESHOLD_PP: float = 3.0


def _per_modality_breakdown(
    scores: list[Any], cases: list[Any], rubrics: tuple[str, ...]
) -> dict[str, dict[str, float | int]]:
    """Mirror evals.run_full_suite._per_modality_breakdown but for a custom
    rubric subset.

    Returns ``{modality: {n_cases, <rubric>: pass_rate, ...}}``.
    """
    grouped: dict[str, list[Any]] = defaultdict(list)
    for case, score in zip(cases, scores):
        modality = getattr(case, "document_modality", None) or "unknown"
        grouped[str(modality)].append(score)

    out: dict[str, dict[str, float | int]] = {}
    for modality, group in grouped.items():
        n = len(group)
        if n == 0:
            continue
        entry: dict[str, float | int] = {"n_cases": n}
        for name in rubrics:
            passed = sum(1 for s in group if getattr(s, name, False))
            entry[name] = passed / n
        out[modality] = entry
    return out


async def _run_one_cell(
    psm: int, dpi: int, *, case_limit: int | None
) -> tuple[list[Any], list[Any]]:
    """Run the eval suite once with the given PSM/DPI override.

    Returns ``(scores, cases)`` parallel lists. Errors per case are
    swallowed (case skipped) so a single OCR failure never tanks the whole
    cell's data.
    """
    # Late imports — keep the script's startup cheap when --help is invoked.
    from config import settings
    from tests.fixtures.w2_eval_cases import CASES  # type: ignore
    from evals.runner import run_case  # type: ignore
    from evals.scoring import score_case  # type: ignore

    settings.tesseract_psm = psm
    settings.tesseract_dpi = dpi
    # Clear the OCR engine cache so the next call picks up the new settings.
    try:
        from documents import ocr_engine
        ocr_engine._engine_cache.clear()
    except Exception:  # noqa: BLE001 — best-effort; nothing else depends on this
        pass

    fixtures_root = _AGENT_API / "tests" / "fixtures" / "eval"
    cases = list(CASES)
    if case_limit is not None:
        cases = cases[:case_limit]

    scores: list[Any] = []
    scored_cases: list[Any] = []
    for case in cases:
        try:
            outcome = await run_case(case, fixtures_root=fixtures_root)
            score = await score_case(case, outcome)
            scores.append(score)
            scored_cases.append(case)
        except Exception as exc:  # noqa: BLE001 — never crash the sweep
            print(
                f"  [skip {getattr(case, 'case_id', '?')}] {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    return scores, scored_cases


def _worst_bucket(per_mod: dict[str, dict[str, float | int]], rubrics: tuple[str, ...]) -> tuple[str, float]:
    """Worst (modality, mean-pass-rate-across-rubrics) tuple.

    The "worst bucket" is the modality whose *mean* across the listed
    rubrics is lowest. Ties broken by alphabetical modality name for
    determinism.
    """
    if not per_mod:
        return ("unknown", 0.0)
    worst_name = ""
    worst_score = 2.0  # > any pass rate
    for modality in sorted(per_mod):
        agg = per_mod[modality]
        rates = [float(agg.get(r, 0.0)) for r in rubrics]
        mean = sum(rates) / len(rates) if rates else 0.0
        if mean < worst_score:
            worst_score = mean
            worst_name = modality
    return worst_name, worst_score


def _format_md_table(
    cells: list[dict[str, Any]], rubrics: tuple[str, ...]
) -> str:
    lines: list[str] = []
    lines.append("# OCR PSM/DPI Grid Results")
    lines.append("")
    lines.append("Worst-bucket score = lowest mean pass-rate across rubrics, by modality.")
    lines.append("")
    lines.append("| psm | dpi | n_cases | worst_bucket | worst_score | global_mean |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for cell in cells:
        lines.append(
            f"| {cell['psm']} | {cell['dpi']} | {cell['n_cases']} | "
            f"{cell['worst_bucket']} | {cell['worst_score'] * 100:.1f}% | "
            f"{cell['global_mean'] * 100:.1f}% |"
        )
    lines.append("")
    lines.append("## Per-modality pass rates by cell")
    lines.append("")
    for cell in cells:
        lines.append(f"### psm={cell['psm']} dpi={cell['dpi']}")
        lines.append("")
        if not cell["per_modality"]:
            lines.append("_no cases scored_")
            lines.append("")
            continue
        lines.append("| modality | n | " + " | ".join(rubrics) + " |")
        lines.append("| --- | --- | " + " | ".join("---" for _ in rubrics) + " |")
        for modality in sorted(cell["per_modality"]):
            agg = cell["per_modality"][modality]
            row = [modality, str(agg.get("n_cases", 0))]
            for r in rubrics:
                row.append(f"{float(agg.get(r, 0.0)) * 100:.0f}%")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--psm",
        type=int,
        nargs="+",
        default=list(_DEFAULT_PSM_VALUES),
        help="PSM values to sweep (default: 3 4 6)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        nargs="+",
        default=list(_DEFAULT_DPI_VALUES),
        help="DPI values to sweep (default: 200 300 400)",
    )
    parser.add_argument(
        "--rubrics-only",
        action="store_true",
        help="Score only mechanical rubrics (no LLM judges). Useful when "
             "ANTHROPIC_API_KEY is unavailable locally.",
    )
    parser.add_argument(
        "--case-limit",
        type=int,
        default=None,
        help="Cap the number of cases per cell (fast subset).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "tune_results.json",
        help="JSON artifact path",
    )
    args = parser.parse_args(argv)

    rubrics = _MECHANICAL_RUBRICS if args.rubrics_only else _ALL_RUBRICS

    cells: list[dict[str, Any]] = []
    baseline_psm, baseline_dpi = 3, 300
    baseline_worst_score: float | None = None

    for psm in args.psm:
        for dpi in args.dpi:
            print(f"=== cell psm={psm} dpi={dpi} ===", file=sys.stderr)
            scores, cases = asyncio.run(
                _run_one_cell(psm, dpi, case_limit=args.case_limit)
            )
            per_mod = _per_modality_breakdown(scores, cases, rubrics)
            worst_name, worst_score = _worst_bucket(per_mod, rubrics)
            global_mean = (
                sum(
                    float(per_mod[m].get(r, 0.0))
                    for m in per_mod for r in rubrics
                )
                / max(1, len(per_mod) * len(rubrics))
            )
            cell = {
                "psm": psm,
                "dpi": dpi,
                "n_cases": len(cases),
                "per_modality": per_mod,
                "worst_bucket": worst_name,
                "worst_score": worst_score,
                "global_mean": global_mean,
            }
            cells.append(cell)
            if psm == baseline_psm and dpi == baseline_dpi:
                baseline_worst_score = worst_score

    # If we never ran the baseline cell, the user passed a custom --psm/--dpi
    # subset. We still report all cells; the verdict just falls back to "no
    # baseline → keep defaults" rather than a numeric comparison.
    table = _format_md_table(cells, rubrics)
    print(table)

    verdict: dict[str, Any]
    if baseline_worst_score is None:
        verdict = {
            "kind": "keep_defaults",
            "reason": "baseline cell (3, 300) not in sweep — cannot compare",
        }
        print("\nNO PARETO WINNER — keep defaults (baseline cell missing)")
    else:
        # Pick the cell that maximises worst_score, breaking ties by global_mean.
        best = max(cells, key=lambda c: (c["worst_score"], c["global_mean"]))
        improvement_pp = (best["worst_score"] - baseline_worst_score) * 100.0
        is_baseline = best["psm"] == baseline_psm and best["dpi"] == baseline_dpi
        if is_baseline or improvement_pp < _PARETO_THRESHOLD_PP:
            verdict = {
                "kind": "keep_defaults",
                "best_cell": {"psm": best["psm"], "dpi": best["dpi"]},
                "improvement_pp": improvement_pp,
                "threshold_pp": _PARETO_THRESHOLD_PP,
            }
            print(
                f"\nNO PARETO WINNER — keep defaults "
                f"(best={best['psm']}/{best['dpi']} improves worst-bucket by "
                f"{improvement_pp:.1f}pp, threshold={_PARETO_THRESHOLD_PP}pp)"
            )
        else:
            # Hard escalation: any modality regresses by >threshold? If so,
            # downgrade to keep_defaults.
            baseline_cell = next(
                c for c in cells if c["psm"] == baseline_psm and c["dpi"] == baseline_dpi
            )
            regression = False
            for modality in best["per_modality"]:
                if modality not in baseline_cell["per_modality"]:
                    continue
                # Compare per-rubric mean within modality
                base_rates = [
                    float(baseline_cell["per_modality"][modality].get(r, 0.0))
                    for r in rubrics
                ]
                cand_rates = [
                    float(best["per_modality"][modality].get(r, 0.0))
                    for r in rubrics
                ]
                base_mean = sum(base_rates) / len(base_rates)
                cand_mean = sum(cand_rates) / len(cand_rates)
                if (base_mean - cand_mean) * 100.0 > _PARETO_THRESHOLD_PP:
                    regression = True
                    break
            if regression:
                verdict = {
                    "kind": "keep_defaults",
                    "reason": "best cell regresses another modality by > threshold",
                    "best_cell": {"psm": best["psm"], "dpi": best["dpi"]},
                    "improvement_pp": improvement_pp,
                }
                print(
                    "\nNO PARETO WINNER — keep defaults "
                    "(candidate cell regresses another modality bucket)"
                )
            else:
                verdict = {
                    "kind": "new_pair",
                    "psm": best["psm"],
                    "dpi": best["dpi"],
                    "improvement_pp": improvement_pp,
                }
                print(
                    f"\nPARETO WINNER: psm={best['psm']} dpi={best['dpi']} "
                    f"(worst-bucket +{improvement_pp:.1f}pp)"
                )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "rubrics": list(rubrics),
                "cells": cells,
                "verdict": verdict,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
