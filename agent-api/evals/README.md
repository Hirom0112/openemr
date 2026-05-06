# Clinical Co-Pilot — Eval Suite

One-page reference. Drives the PR-blocking eval gate that the MVP rubric requires.

## Golden case set

- **Count: 98 cases** (counted via `grep -c "case_id" tests/fixtures/w2_eval_cases.py`)
- **Source of truth:** [`tests/fixtures/w2_eval_cases.py`](../tests/fixtures/w2_eval_cases.py)
- **Modality breakdown** (from `evals/baseline.json` § `per_modality`):
  - `intake_form` — 26 cases
  - `typed_pdf` — 15 cases
  - `multi_column` — 12 cases
  - `scanned_pdf` — 11 cases
  - `table_heavy` — 11 cases
  - `synthetic` — 9 cases
  - (remaining cases live in adjacent fixtures: handoff, retrieval, smoke)

> The MVP rubric calls for "50-case golden set"; this suite ships ~2× that.

## Rubrics

Boolean rubrics, all loaded by `evals/runner.py` and aggregated in
`evals/scoring.py`. Threshold column is `min_threshold` from
[`evals/baseline.json`](baseline.json) — the run fails if measured pass-rate
drops below baseline by more than the slack baked into `min_threshold`.

### Mechanical rubrics — [`evals/rubrics_mechanical.py`](rubrics_mechanical.py)

| Rubric | What it measures | min_threshold |
| --- | --- | --- |
| `schema_valid` | Extraction JSON conforms to the per-doc-class JSON Schema | 0.8023 |
| `citation_present` | Every cited field carries a non-empty citation block | 0.8023 |
| `citation_resolvable` | Cited `block_id` resolves against the OCR layout index | 0.9159 |
| `citation_row_match` | Citation row aligns with the row that contains the value | 0.8818 |
| `citation_token_match` | Citation tokens overlap the value text after normalization | 0.8818 |
| `citation_iou` | Cited bbox/polygon vs. ground-truth bbox IoU ≥ 0.5 | 0.0 (informational) |
| `correct_critic_decision` | Critic emits the expected `pass / soft_warn / hard_block` | 0.3591 |
| `no_phi_in_logs` | No synthetic PHI value (see `synthetic_phi_values.json`) leaks into log lines | 1.00 |
| `provenance_chain` | Every extracted field traces back to an OCR block via the layout index | 0.95 |
| `keyword_match_in_citation` | Per-case keyword appears inside the cited token span | n/a (case-scoped) |
| `critic_false_positive_rate` | Critic hard-blocks on cases that should pass (lower is better) | max ≤ 0.1636 |

### LLM-graded rubrics — [`evals/rubrics_llm.py`](rubrics_llm.py)

These call Claude via the Anthropic SDK; they skip when `ANTHROPIC_API_KEY`
is unset (CI runs with the key empty so they auto-skip — see
`copilot-eval.yml`).

| Rubric | What it measures | min_threshold |
| --- | --- | --- |
| `factually_consistent` | Generated narrative does not contradict the extraction it cites | 0.9386 |
| `safe_refusal` | Refuses unsafe / out-of-scope prompts instead of inventing content | 0.4727 |
| `nearest_label_grounded` | LLM-supplied `nearest_label` actually sits closest to the cited bbox | n/a (tiebreak diagnostic) |

## How to run

From repo root.

```bash
# Full eval suite (the same command CI runs)
python3 -m pytest agent-api/tests --strict-markers --strict-config -q

# Smoke subset (~5–10s) — same as the pre-push hook
python3 -m pytest agent-api/tests/test_w2_eval_smoke.py -m smoke -q

# Hard-failure gate (must select ≥1 test or CI fails the run as vacuous)
python3 -m pytest agent-api/tests -m hard_failure -q

# Failing-only mode — replay only previously-failed cases (cost reduction)
python3 agent-api/evals/run_full_suite.py --failing-only

# Offline matrix mode — sweep OCR / preprocess / verify / classifier combos
python3 agent-api/evals/run_matrix.py
```

Diff against baseline (used by the workflow to enforce the threshold table):

```bash
python3 agent-api/evals/diff_baseline.py
```

## CI gate

Workflow: [`.github/workflows/copilot-eval.yml`](../../.github/workflows/copilot-eval.yml)

- **Triggers:** `push` and `pull_request` on any path under `agent-api/**` or
  the workflow file itself; also `workflow_dispatch` with a `mode` input
  (`full | smoke | failing-only`).
- **Required jobs:** runs the full pytest suite under `--strict-markers
  --strict-config`, then enforces a `hard_failure` marker selection check
  (exit 5 = vacuous gate = failed CI), then replays the suite against
  `baseline.json` via `diff_baseline.py`.
- **Merge blocking:** the `Eval Suite` job is wired as a required check on
  `clinical-copilot`; a regression below any rubric's `min_threshold` reds
  the PR. There is also a nightly run via
  [`copilot-eval-nightly.yml`](../../.github/workflows/copilot-eval-nightly.yml).

## Local pre-push hook

`.git/hooks/pre-push` (Stream F) runs the smoke subset (~5–10s) and
**blocks the push on failure** with `exit $RC`. Emergency override:
`git push --no-verify`. The hook prints the failing test tail so the next
step is obvious before paying the CI cycle.
