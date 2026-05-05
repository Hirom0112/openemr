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

# ---------------------------------------------------------------------------
# Wave 2C — citation_present demotion
#
# ``citation_present`` is now informational-only. The historical rubric is a
# pure non-empty check: it passes whenever every cited item carries at least
# one citation, regardless of whether the citation actually points at a real
# layout block or matches the value text. The new mechanical rubrics
# (``citation_resolvable`` / ``citation_row_match`` / ``citation_token_match``)
# replace it as the gate. We keep ``citation_present`` reported in the
# results JSON so consumers don't break, but its baseline entry — if any —
# is skipped during gating.
# ---------------------------------------------------------------------------
INFORMATIONAL_ONLY_RUBRICS = ("citation_present",)

# Wave 2C — per-modality gating tunables.
# Bucket floor = global rubric floor MINUS this many percentage points.
# Buckets get more variance than the global mean, so the floor is slightly
# more lenient.
PER_MODALITY_FLOOR_LENIENCY_PP = 0.05
# Buckets with fewer cases are reported but do NOT gate (under-powered).
PER_MODALITY_MIN_CASES = 5
# No single (modality, rubric) pair may regress more than this vs. baseline.
# When the baseline does not yet carry per-modality entries, this is no-op.
PER_MODALITY_REGRESSION_LIMIT_PP = 0.08


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
        if rubric == "per_modality":
            # Handled below (separate bucket loop).
            continue
        if rubric in INFORMATIONAL_ONLY_RUBRICS:
            # Wave 2C: report only, do not gate.
            observed_info = results.get(rubric)
            try:
                observed_info_v = float(observed_info) if observed_info is not None else None
            except (TypeError, ValueError):
                observed_info_v = None
            rows.append((
                rubric,
                _fmt_pct(spec.get("pass_rate")),
                _fmt_pct(observed_info_v),
                "info",
                "—",
                "INFO",
            ))
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

    # ── Wave 2C — per-modality bucket gating ────────────────────────────
    # CI fails if any modality bucket regresses below floor, even when the
    # global mean improves. Under-powered buckets (<5 cases) are reported
    # but do not gate.
    per_modality_observed = results.get("per_modality") or {}
    per_modality_baseline = baseline.get("per_modality") or {}
    if isinstance(per_modality_observed, dict) and per_modality_observed:
        rows.append(("--- per-modality ---", "", "", "", "", ""))
        # Pre-compute the per-rubric global floors (minus leniency).
        global_floors = {
            r: max(0.0, float(spec.get("min_threshold", 0.0)) - PER_MODALITY_FLOOR_LENIENCY_PP)
            for r, spec in baseline.items()
            if isinstance(spec, dict) and "min_threshold" in spec
            and r not in INFORMATIONAL_ONLY_RUBRICS
        }
        for modality in sorted(per_modality_observed.keys()):
            entry = per_modality_observed[modality] or {}
            if not isinstance(entry, dict):
                continue
            n_cases = int(entry.get("n_cases", 0) or 0)
            base_entry = per_modality_baseline.get(modality) if isinstance(per_modality_baseline, dict) else None
            for rubric, lenient_floor in global_floors.items():
                obs = entry.get(rubric)
                if obs is None:
                    continue
                try:
                    obs_v = float(obs)
                except (TypeError, ValueError):
                    continue
                gates = n_cases >= PER_MODALITY_MIN_CASES
                status = "PASS" if gates else "INFO"
                # Floor check
                if gates and obs_v < lenient_floor:
                    status = "FAIL"
                    failures.append(
                        f"per_modality[{modality}].{rubric}: {obs_v:.3f} "
                        f"< lenient_floor {lenient_floor:.3f} (n={n_cases})"
                    )
                # Bucket-delta gate vs baseline (when baseline carries it)
                base_rate = None
                if isinstance(base_entry, dict):
                    base_rate = base_entry.get(rubric)
                    try:
                        base_rate = float(base_rate) if base_rate is not None else None
                    except (TypeError, ValueError):
                        base_rate = None
                delta_str = "—"
                if base_rate is not None:
                    delta = obs_v - base_rate
                    delta_str = f"{delta:+.3f}"
                    if gates and (base_rate - obs_v) > PER_MODALITY_REGRESSION_LIMIT_PP:
                        status = "FAIL"
                        failures.append(
                            f"per_modality[{modality}].{rubric}: regression > "
                            f"{PER_MODALITY_REGRESSION_LIMIT_PP * 100:.0f}pp — "
                            f"baseline {base_rate:.3f} → observed {obs_v:.3f}"
                        )
                rows.append((
                    f"{modality}.{rubric} (n={n_cases})",
                    _fmt_pct(base_rate),
                    _fmt_pct(obs_v),
                    _fmt_pct(lenient_floor),
                    delta_str,
                    status,
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
