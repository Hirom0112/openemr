"""Offline matrix evaluator for OCR/preprocess/verify/classifier flag combos.

Why: comparing flag combinations by burning W2 Eval Suite CI runs is the
single largest source of Anthropic API spend on this project. This runner
invokes ``run_full_suite.py`` once per combo as a subprocess (so each invocation
sees fresh env vars + module imports), collects per-rubric pass-rates, and
emits a Pareto-style markdown summary so a human can pick which combo to
promote to the default.

Curated combos (--combos curated, default):
  baseline           — all flags at their committed defaults
  +paddle            — OCR_ENGINE=paddle
  +photo_preprocess  — PHOTO_PREPROCESS=auto
  +verify_sample     — VERIFY_CITATIONS=sample
  +classifier_claude — DOC_CLASSIFIER=claude
  full_stack         — paddle + photo_preprocess=auto + verify=sample

Each combo touches at least one binary flag. The full matrix (16 combos) is
opt-in via --combos full and is intended for manual one-off audits.

Run modes:
  --smoke      forwards --smoke to each child run (~$1/combo vs ~$15-30/combo)
  --dry-run    do not invoke the child runner; just print the planned env vars
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

REPO_AGENT_API = Path(__file__).resolve().parent.parent

# Flag axes — name → {label: env_var_value}. The first entry is the default.
FLAG_AXES: dict[str, dict[str, str]] = {
    "OCR_ENGINE": {"tesseract": "tesseract", "paddle": "paddle"},
    "PHOTO_PREPROCESS": {"off": "off", "auto": "auto"},
    "VERIFY_CITATIONS": {"off": "off", "sample": "sample"},
    "DOC_CLASSIFIER": {"regex": "regex", "claude": "claude"},
}


@dataclass(frozen=True)
class Combo:
    """One flag combination to evaluate."""
    name: str
    env: dict[str, str] = field(default_factory=dict)


# Each curated combo touches at least one flag away from the default.
CURATED: tuple[Combo, ...] = (
    Combo("baseline", {}),
    Combo("+paddle", {"OCR_ENGINE": "paddle"}),
    Combo("+photo_preprocess", {"PHOTO_PREPROCESS": "auto"}),
    Combo("+verify_sample", {"VERIFY_CITATIONS": "sample"}),
    Combo("+classifier_claude", {"DOC_CLASSIFIER": "claude"}),
    Combo("full_stack", {
        "OCR_ENGINE": "paddle",
        "PHOTO_PREPROCESS": "auto",
        "VERIFY_CITATIONS": "sample",
    }),
)


def _full_combos() -> list[Combo]:
    """Cartesian product of all axes — 16 combos, deterministic order."""
    axis_names = sorted(FLAG_AXES.keys())
    out: list[Combo] = []
    indices = [0] * len(axis_names)
    n_combos = 1
    for axis in axis_names:
        n_combos *= len(FLAG_AXES[axis])
    for _ in range(n_combos):
        env: dict[str, str] = {}
        label_parts: list[str] = []
        for i, axis in enumerate(axis_names):
            label = list(FLAG_AXES[axis].keys())[indices[i]]
            value = FLAG_AXES[axis][label]
            env[axis] = value
            label_parts.append(f"{axis}={label}")
        out.append(Combo(name="|".join(label_parts), env=env))
        # increment indices like an odometer
        for i in range(len(axis_names) - 1, -1, -1):
            indices[i] += 1
            if indices[i] < len(FLAG_AXES[axis_names[i]]):
                break
            indices[i] = 0
    return out


def _resolve_combos(spec: str) -> list[Combo]:
    if spec == "curated":
        return list(CURATED)
    if spec == "full":
        return _full_combos()
    raise SystemExit(f"unknown --combos value: {spec!r}")


# Rubrics surfaced in the markdown summary, in display order. citation_iou
# is intentionally excluded — it's info-only at the global level.
_REPORT_RUBRICS = (
    "schema_valid",
    "citation_resolvable",
    "citation_row_match",
    "citation_token_match",
    "correct_critic_decision",
    "factually_consistent",
    "no_phi_in_logs",
    "provenance_chain",
)


def _run_one(
    combo: Combo,
    output_root: Path,
    smoke: bool,
    cache_mode: Optional[str],
    runner: Callable[[list[str], dict[str, str]], subprocess.CompletedProcess],
) -> dict:
    """Invoke run_full_suite for one combo. Returns a dict with rubric rates +
    raw stdout/stderr for debugging. ``runner`` is injected so tests can mock
    the subprocess call.
    """
    env = dict(os.environ)
    env.update(combo.env)
    out_json = output_root / f"{_safe(combo.name)}.json"
    out_md = output_root / f"{_safe(combo.name)}.md"
    cmd = [
        sys.executable, "-m", "evals.run_full_suite",
        "--output", str(out_json),
        "--md", str(out_md),
    ]
    if smoke:
        cmd.append("--smoke")
    if cache_mode:
        cmd += ["--cache", cache_mode]
    proc = runner(cmd, env)
    rates: dict[str, Optional[float]] = {}
    try:
        data = json.loads(out_json.read_text()) if out_json.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    for rubric in _REPORT_RUBRICS:
        v = data.get(rubric)
        try:
            rates[rubric] = float(v) if v is not None else None
        except (TypeError, ValueError):
            rates[rubric] = None
    return {
        "name": combo.name,
        "env": dict(combo.env),
        "rates": rates,
        "returncode": proc.returncode,
    }


def _safe(name: str) -> str:
    return name.replace("|", "_").replace("=", "-").replace("+", "p_")


def _real_runner(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, cwd=REPO_AGENT_API, check=False)


def _markdown(results: list[dict]) -> str:
    """Render a markdown table ranked by mean rubric pass-rate (descending)."""
    def _score(row: dict) -> float:
        vals = [v for v in row["rates"].values() if v is not None]
        return sum(vals) / len(vals) if vals else 0.0
    ranked = sorted(results, key=_score, reverse=True)
    out = ["# Eval Matrix Results", ""]
    header = ["combo", "score"] + list(_REPORT_RUBRICS)
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for row in ranked:
        cells = [row["name"], f"{_score(row):.3f}"]
        for r in _REPORT_RUBRICS:
            v = row["rates"].get(r)
            cells.append(f"{v:.3f}" if v is not None else "—")
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combos", choices=["curated", "full"], default="curated")
    parser.add_argument(
        "--output", type=Path, required=True,
        help="Directory to write per-combo results JSONs + matrix_results.json + matrix_results.md",
    )
    parser.add_argument("--smoke", action="store_true", default=False,
                        help="Forward --smoke to each child run (~$1/combo).")
    parser.add_argument("--cache", type=str, default="readwrite",
                        choices=["off", "read", "write", "readwrite"],
                        help="Forward --cache to each child run (default: readwrite).")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Print planned env vars; do not invoke run_full_suite.")
    args = parser.parse_args(argv)

    combos = _resolve_combos(args.combos)
    args.output.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        for combo in combos:
            print(f"{combo.name}: {combo.env}")
        return 0

    results: list[dict] = []
    for combo in combos:
        print(f"\n=== Running combo: {combo.name} ===")
        result = _run_one(
            combo,
            output_root=args.output,
            smoke=args.smoke,
            cache_mode=args.cache,
            runner=_real_runner,
        )
        results.append(result)

    matrix_path = args.output / "matrix_results.json"
    matrix_path.write_text(json.dumps(results, indent=2) + "\n")
    md_path = args.output / "matrix_results.md"
    md = _markdown(results)
    md_path.write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
