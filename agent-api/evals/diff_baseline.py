"""Compare an eval-results.json against the committed baseline.

Exit codes:
  0 — within tolerance
  1 — at least one rubric dropped >5pp from baseline OR below min_threshold,
      OR no_phi_in_logs failed (absolute), OR critic_false_positive_rate exceeded max,
      OR results JSON malformed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REGRESSION_TOLERANCE_PP = 0.05  # 5 percentage points

DEFAULT_BASELINE = Path(__file__).parent / "baseline.json"

# Rubrics with absolute floors (any failure is a hard fail regardless of delta).
ABSOLUTE_RUBRICS = ("no_phi_in_logs",)


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def diff(baseline: dict, results: dict) -> tuple[bool, list[str]]:
    """Returns (gate_passes, list_of_failure_messages) and prints a markdown table."""
    failures: list[str] = []
    rows: list[tuple[str, str, str, str, str, str]] = []
    rows.append(("rubric", "baseline", "observed", "min", "delta", "status"))

    # Pass-rate rubrics.
    for rubric, spec in baseline.items():
        if rubric == "critic_false_positive_rate":
            continue
        baseline_rate = spec["pass_rate"]
        min_floor = spec["min_threshold"]
        observed = results.get(rubric)
        if observed is None:
            failures.append(f"{rubric}: missing from results JSON")
            rows.append((rubric, _fmt_pct(baseline_rate), "MISSING", _fmt_pct(min_floor), "—", "FAIL"))
            continue
        try:
            observed = float(observed)
        except (TypeError, ValueError):
            failures.append(f"{rubric}: non-numeric value {observed!r}")
            rows.append((rubric, _fmt_pct(baseline_rate), str(observed), _fmt_pct(min_floor), "—", "FAIL"))
            continue

        delta = observed - baseline_rate
        status = "PASS"
        if rubric in ABSOLUTE_RUBRICS:
            if observed < min_floor:
                status = "FAIL"
                failures.append(
                    f"{rubric}: absolute floor — observed {observed:.3f} < min {min_floor:.3f}"
                )
        else:
            if observed < min_floor:
                status = "FAIL"
                failures.append(
                    f"{rubric}: below min_threshold — observed {observed:.3f} < min {min_floor:.3f}"
                )
            elif (baseline_rate - observed) > REGRESSION_TOLERANCE_PP:
                status = "FAIL"
                failures.append(
                    f"{rubric}: regression > {REGRESSION_TOLERANCE_PP * 100:.0f}pp — "
                    f"baseline {baseline_rate:.3f} → observed {observed:.3f} (Δ={delta:+.3f})"
                )

        rows.append((
            rubric,
            _fmt_pct(baseline_rate),
            _fmt_pct(observed),
            _fmt_pct(min_floor),
            f"{delta:+.3f}",
            status,
        ))

    # critic_false_positive_rate (max-bounded).
    cfpr_spec = baseline.get("critic_false_positive_rate", {})
    cfpr_max = cfpr_spec.get("max")
    cfpr_observed = results.get("critic_false_positive_rate")
    cfpr_status = "PASS"
    if cfpr_observed is None:
        cfpr_status = "FAIL"
        failures.append("critic_false_positive_rate: missing from results JSON")
        rows.append(("critic_false_positive_rate", "—", "MISSING", _fmt_pct(cfpr_max), "—", "FAIL"))
    else:
        try:
            cfpr_observed_f = float(cfpr_observed)
        except (TypeError, ValueError):
            failures.append(f"critic_false_positive_rate: non-numeric value {cfpr_observed!r}")
            cfpr_status = "FAIL"
            cfpr_observed_f = float("nan")
        else:
            if cfpr_max is not None and cfpr_observed_f > cfpr_max:
                cfpr_status = "FAIL"
                failures.append(
                    f"critic_false_positive_rate: {cfpr_observed_f:.3f} > max {cfpr_max:.3f}"
                )
        rows.append((
            "critic_false_positive_rate",
            "—",
            _fmt_pct(cfpr_observed_f) if cfpr_observed_f == cfpr_observed_f else str(cfpr_observed),
            _fmt_pct(cfpr_max),
            "—",
            cfpr_status,
        ))

    # Print markdown table.
    header = rows[0]
    print("| " + " | ".join(header) + " |")
    print("| " + " | ".join("---" for _ in header) + " |")
    for row in rows[1:]:
        print("| " + " | ".join(row) + " |")

    return (len(failures) == 0, failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()

    try:
        baseline = json.loads(args.baseline.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: failed to read baseline {args.baseline}: {e}", file=sys.stderr)
        print("GATE: FAIL")
        return 1

    try:
        if str(args.results) == "/dev/stdin" or str(args.results) == "-":
            results = json.loads(sys.stdin.read())
        else:
            results = json.loads(args.results.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: failed to read results {args.results}: {e}", file=sys.stderr)
        print("GATE: FAIL")
        return 1

    if not isinstance(results, dict):
        print("ERROR: results JSON must be an object", file=sys.stderr)
        print("GATE: FAIL")
        return 1

    ok, failures = diff(baseline, results)
    print()
    if not ok:
        print("Failures:")
        for f in failures:
            print(f"  - {f}")
        print("GATE: FAIL")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
