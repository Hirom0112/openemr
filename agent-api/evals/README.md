# Clinical Co-Pilot — Eval Suite

*Scope: W2 eval suite. For W1 dispatcher tests, see `W1_ARCHITECTURE.md` §6.1.*

One-page reference. Drives the PR-blocking eval gate that the MVP rubric requires.

## Golden case set

- **Count: 156 cases at submission lock.** For the live count, run from `agent-api/`:

  ```bash
  python3 -c "from tests.fixtures.w2_eval_cases import CASES; print(len(CASES))"
  ```

- **Source of truth:** [`tests/fixtures/w2_eval_cases.py`](../tests/fixtures/w2_eval_cases.py)
- **Modality breakdown** (live runtime — `Counter(c.document_modality for c in CASES)`; cross-cuts the bucket axis used by `W2_ARCHITECTURE.md §11.1`):
  - `typed_pdf` — 27 cases
  - `intake_form` — 26 cases
  - `table_heavy` — 23 cases
  - `multi_column` — 12 cases
  - `photo_capture` — 12 cases
  - `scanned_pdf` — 11 cases
  - `synthetic` — 9 cases
  - `hl7_v2` — 8 cases
  - `xlsx_workbook` — 8 cases
  - `docx_referral` — 8 cases
  - `tiff_fax` — 8 cases
  - `unknown` — 4 cases

> The MVP rubric calls for a "50-case golden set" as the floor; this suite ships ~3× that, including the Phase 9.9 multimodal expansion (HL7v2, XLSX, DOCX, TIFF, photo capture). Counts grow as the suite grows — re-run the runtime command for the current value.

## Rubrics

Boolean rubrics, all loaded by `evals/runner.py` and aggregated in
`evals/scoring.py`. Threshold column is `min_threshold` from
[`evals/baseline.json`](baseline.json) — the run fails if measured pass-rate
drops below baseline by more than the slack baked into `min_threshold`.

### Mechanical rubrics — [`evals/rubrics_mechanical.py`](rubrics_mechanical.py)

| Rubric | What it measures | min_threshold |
| --- | --- | --- |
| `schema_valid` | Extraction JSON conforms to the per-doc-class JSON Schema | 0.8250 |
| `citation_present` | Every cited field carries a non-empty citation block | 0.8250 |
| `citation_resolvable` | Cited `block_id` resolves against the OCR layout index | 0.8250 |
| `citation_row_match` | Citation row aligns with the row that contains the value | 0.6750 |
| `citation_token_match` | Citation tokens overlap the value text after normalization | 0.6750 |
| `citation_iou` | Cited bbox/polygon vs. ground-truth bbox IoU ≥ 0.5 | 0.0 (informational) |
| `correct_critic_decision` | Critic emits the expected `pass / soft_warn / hard_block` | 0.3083 |
| `no_phi_in_logs` | No synthetic PHI value (see `synthetic_phi_values.json`) leaks into log lines | 1.00 |
| `provenance_chain` | Every extracted field traces back to an OCR block via the layout index | 0.95 |
| `keyword_match_in_citation` | Per-case keyword appears inside the cited token span | n/a (case-scoped) |
| `critic_false_positive_rate` | Critic hard-blocks on cases that should pass (lower is better) | max ≤ 0.2000 |

### LLM-graded rubrics — [`evals/rubrics_llm.py`](rubrics_llm.py)

These call Claude via the Anthropic SDK; they skip when `ANTHROPIC_API_KEY`
is unset (CI runs with the key empty so they auto-skip — see
`copilot-eval.yml`).

| Rubric | What it measures | min_threshold |
| --- | --- | --- |
| `factually_consistent` | Generated narrative does not contradict the extraction it cites | 0.7750 |
| `safe_refusal` | Refuses unsafe / out-of-scope prompts instead of inventing content | 0.5000 |
| `nearest_label_grounded` | LLM-supplied `nearest_label` actually sits closest to the cited bbox | n/a (tiebreak diagnostic) |

#### Median-of-3 vote on judge calls (Phase 4.8)

**Why.** Even at `temperature=0` the Anthropic judge calls produce
inter-run variance — the Phase 4.7 root-cause investigation observed ~74
case flips per full-suite run on `factually_consistent` + `safe_refusal`.
The variance is server-side (verified by replaying identical payloads;
the SDK request bytes are byte-identical across runs) and not something
we can pin from the client.

**What.** Each judge invocation issues 3 identical calls in parallel
and applies a majority vote:

* 2 of 3 `yes` → rubric PASSES
* 2 of 3 `no` → rubric FAILS
* 3 distinct outcomes (e.g. `yes` / `no` / transport-failure) → FAILS
  (treated as ambiguous — the case is not confidently grounded)

**How it interacts with the cache.** Vote results are written to
`agent-api/.eval_cache/judges/<sha256>.json` keyed by
`(rubric_name, case_id, payload_hash)`. The cache stores the *single*
boolean result of the vote (plus the 3 raw votes for diagnostics), so
re-runs over the same case are O(1) disk reads and pay no API cost.
The first run of a fresh case set pays 3× the per-judge call budget;
subsequent runs are free.

This applies ONLY to the LLM-graded rubrics. Mechanical rubrics are
deterministic at the code level and don't need it. The cache lives
alongside the runner-level response cache (`.eval_cache/`) but in a
separate `judges/` subdirectory so the two can be invalidated
independently.

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
