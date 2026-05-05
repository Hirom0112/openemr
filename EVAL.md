# Clinical Co-Pilot — Eval Dataset & Test Results

## What's here

The eval suite is the test suite. All test files live in `agent-api/tests/` and run with pytest. **534 test cases across 44 test files**, covering every clinical-safety-critical path. Authoritative count: `python3 -m pytest agent-api/tests/ --collect-only -q | tail -1`.

---

## Headline test files

| File | Cases | What it covers |
|---|---|---|
| `test_triage_rules.py` | 23 | Rules-engine correctness: qSOFA scoring, critical lab detection, 10-level priority assignment, deterministic ordering |
| `test_criteria_extractor.py` | 59 | FHIR data extraction: lab values, vitals, condition codes parsed from raw bundles |
| `test_agent_routing.py` | 24 | Dispatcher routing: 24 natural-language prompts routed to the correct tool, misroute detection, out-of-census access block |
| `test_synthetic_patients.py` | 38 | Patient-level assertions against the 25-patient synthetic dataset: priorities, census ordering, pt-019/pt-020 boundary cases |
| `test_verification.py` | 20 | Source attribution and domain constraint enforcement: blank allergy section, stale critical values, code status flags |
| `test_citations.py` | 16 | Citation extraction and rendering: every clinical claim traces to a source type and timestamp |
| `test_tool_schemas.py` | 46 | Tool schema validation: every dispatcher tool definition matches its handler contract |
| `test_dispatcher_verification.py` | 13 | Verification layer applied to dispatcher output: blocked responses, source stripping |
| `test_checkpointer.py` | 9 | Conversation state persistence: Redis read/write, session replay |
| `test_query_router.py` | 19 | Keyword-level routing heuristics for follow-up query disambiguation |
| `test_medication_safety.py` | 13 | Medication safety surface: allergy cross-check, interaction detection |
| `test_fhir_auth.py` | 6 | FHIR auth layer: token cache hit/miss, expiry, re-fetch |
| `test_prompt_eval.py` | 46 | Golden-set prompt eval (45 fixture cases + sanity test); end-to-end through the dispatcher |

The other ~31 files cover dispatcher request shape, error classes, fast-path routing, history persistence, structured skip, briefing generation/cache/freshness, census cache + resilience, handoff streaming, prefetch backpressure & cost guardrail, observability, audit (emit / middleware / writer), scope check, and medication-safety endpoint shape. Run `--collect-only` to enumerate.

---

## Test fixtures (FHIR bundles)

Three representative FHIR R4 patient bundles in `agent-api/tests/fixtures/`:

| File | Patient | Signals |
|---|---|---|
| `marcus_webb_bundle.json` | Marcus Webb | qSOFA ≥ 2, critical lactate → triage P1 (URGENT/SEPSIS) |
| `delia_fontaine_bundle.json` | Delia Fontaine | Critical K⁺ (6.1), abnormal but non-critical labs → P3 |
| `stable_vitals_bundle.json` | Stable patient | Normal vitals, no critical labs → P8–P10 baseline |

These three bundles cover the three regions of the priority space (critical, abnormal, stable) and are the ground truth for all triage rule assertions.

---

## Running the tests

```bash
cd agent-api
pip install -r requirements.txt
python -m pytest tests/ -v
```

Tests marked `@pytest.mark.live` require a running agent-api + FHIR server. All other tests run offline with fixture data.

---

## Smoke test matrix

`docs/clinical-copilot/smoke-matrix.md` maps each of the 5 use cases to its expected endpoint, tool invocation, and pass/fail condition against the synthetic dataset. Run the full smoke suite against the deployed agent:

```bash
AGENT_API_URL=https://your-agent-url OPENEMR_BASE_URL=https://your-openemr-url bash scripts/03-smoke-test.sh
```

---

## Provenance chain rubric (seventh rubric)

The architecture's prior framing of "the system is gated by 50 cases scored against five boolean rubrics" is out of date — the gate is now scored against **seven** boolean rubrics (the original five from `W2_ARCHITECTURE.md §11.2` plus `factually_consistent` plus the new `provenance_chain`).

`provenance_chain` is a mechanical (non-LLM) boolean check, run per case, that asserts the full chain `LabValue → copilot_observations row → derivedFrom → DocumentReference/copilot-{doc_id} → documents.id` is traversable end-to-end on the deployed pilot. Concretely:

- For every extracted `LabValue` produced by the case, the rubric asserts a corresponding row exists in OpenEMR's module-private `copilot_observations` MySQL table at the deterministic id `copilot-{doc_id}-{loinc_code}`. Missing row = fail.
- For every such row, the rubric asserts the persisted FHIR resource's `derivedFrom[0].reference` parses to `DocumentReference/copilot-{doc_id}` and that `{doc_id}` resolves to a row in OpenEMR's `documents` table. Broken reference = fail.
- For every Observation, the rubric asserts each entry in the `_copilot_citations` extension carries a `bbox` and `quote_or_value` that resolve to a real bbox in the extraction's `ocr_layout` (the same layout JSON consumed by the citation-fidelity check). Unresolvable citation = fail.

This rubric is the regression-protected contract for the §4.2.4 provenance chain. A code change that drops the `derivedFrom` field, regresses the deterministic-id scheme, or breaks the bbox/citation linkage trips this rubric in CI before reaching the deployed pilot.

`scripts/verify_mvp.sh` Check 5 runs the same assertion against the deployed pilot at smoke-test time; the eval rubric runs it against the test fixtures.

---

## Eval design decisions

**Why pytest, not a separate eval framework?** The clinical-correctness assertions (triage ordering, source attribution, domain constraints) are deterministic — they check rule outputs against known fixture values, not LLM outputs. Deterministic tests belong in the test suite, not an LLM eval framework. The one exception is routing: `test_agent_routing.py` calls the live dispatcher and checks which tool was invoked. These require `ANTHROPIC_API_KEY` and are excluded from CI by default.

**Synthetic data only.** No real patient data enters the test suite. The 25-patient synthetic dataset is derived from publicly documented clinical vignettes with all identifiers replaced.

**Coverage gaps (v2 roadmap).** The eval suite does not yet include: end-to-end latency benchmarks against UC-1/2/3 SLA targets, adversarial prompt injection tests, or a comparison against a clinician gold standard for briefing quality. These are documented in `TODO.md` under "Queued."

---

## W2 88-case eval suite — Stage 4 category mapping

The W2 fixture set (`agent-api/tests/fixtures/w2_eval_cases.py`) is the gating eval. Each case is a frozen `W2EvalCase`; the runner (`agent-api/evals/runner.py`) executes the W2 graph and the rubric layer (`agent-api/evals/rubrics_*.py`) scores the outcome. The 11 buckets map onto the 5 Stage 4 categories below.

### a. Extraction

What we test: lab + intake extractors produce schema-valid output, name the right fields, and survive low-quality / multi-page / non-English input without inventing values.

Buckets covering this: `lab_nominal` (17), `intake_nominal` (17), `unknown_nominal` (8), `low_quality_scan` (5). Total: **47 cases**.

### b. Evidence retrieval

What we test: clinical questions are answered from one of the indexed sources (`kdigo-aki-2012`, `ada-inpatient-glycemic`, `ssc-2021`) with a keyword-grounded quote.

Buckets covering this: `evidence_retrieval` (10). Total: **10 cases**. Each case carries `evidence_query`, `expected_must_cite_source_id`, and `expected_keywords_in_quote`. The mechanical `keyword_match_in_citation` rubric (in `agent-api/evals/rubrics_mechanical.py`) checks two conditions per case: (a) at least one returned snippet's `source_id` matches the expected source, and (b) at least one snippet's `quote_or_value` contains every required keyword (case-insensitive). Vacuously True off the evidence bucket; cleanly skipped (not counted) when `AUDIT_DB_URL` or `VOYAGE_API_KEY` is missing.

### c. Citations

What we test: every clinical claim carries at least one citation that resolves into the OCR layout, AND every Observation that hits MySQL carries a `derivedFrom` chain back to the source DocumentReference. Runs over the full 88 via `citation_present`; `provenance_chain` runs over the labs that set `expected_provenance`. CI must run the MySQL service container so the provenance probe is live — without it, the rubric silently skips (tri-state `None`).

Beyond presence, three Wave 2C mechanical rubrics gate citation quality at the token level:

- `citation_resolvable` — every citation's `field_or_chunk_id` resolves to a real layout block (OCR bbox). Vacuously True when no layout is captured.
- `citation_row_match` — the cited block's text contains every value-token (case + punctuation normalized), in any order. `"glucose: 92"` and `"92 glucose"` both pass.
- `citation_token_match` — stricter subsequence match: value tokens must appear in the cited block in the same order. `"glucose 92"` passes; `"92 glucose"` fails.

All three default to vacuously True when the extraction lacks document-type citations (guidelines, observations) or when OCR layout is unavailable. `run_full_suite.py` reports pass-rates for each, and `baseline.json` gates them with a per-rubric `min_threshold`.

Buckets covering this: `lab_nominal` (17), `intake_nominal` (17). Total: **34 cases**.

### d. Refusals

What we test: when the agent should refuse, it refuses cleanly. Hard-block on identity / unreadable failures; soft-warn on conflicts and demographic drift.

Buckets covering this: `wrong_patient` (7), `wrong_type_hint` (6), `mixed_content` (6), `intra_doc_conflict` (3). Total: **22 cases**.

### e. Missing data

What we test: when fields are absent, the agent records absence rather than inventing values. When the document itself is empty / encrypted / all-noise, the agent refuses cleanly.

Buckets covering this: `missing_data` (4), `blank_noise` (5). Total: **9 cases**.

### Inventory

| Bucket               | Count |
| -------------------- | ----- |
| `lab_nominal`        | 17    |
| `intake_nominal`     | 17    |
| `unknown_nominal`    | 8     |
| `wrong_type_hint`    | 6     |
| `wrong_patient`      | 7     |
| `blank_noise`        | 5     |
| `mixed_content`      | 6     |
| `low_quality_scan`   | 5     |
| `intra_doc_conflict` | 3     |
| `evidence_retrieval` | 10    |
| `missing_data`       | 4     |
| **Total**            | **88**|

### Running the W2 suite

```bash
# Generate the deterministic fixture corpus.
python3 agent-api/tests/fixtures/eval/_generate_eval_corpus.py
# Run the full 88-case suite locally.
cd agent-api && python3 -m evals.run_full_suite --output ../eval_results.json
# Diff against the rolling baseline (gates the PR on >5pp drop).
python3 agent-api/evals/diff_baseline.py \
  --baseline agent-api/evals/baseline.json \
  --results eval_results.json
```

CI runs the same flow under `.github/workflows/copilot-eval.yml`. The `w2-eval` job mounts a MySQL 8 service so `provenance_chain` actually fires; without it the rubric is silently skipped via the tri-state path in `evals/scoring.py::_score_provenance_chain`.

### Adding a case

1. Add the `W2EvalCase` to `tests/fixtures/w2_eval_cases.py`.
2. Bump the matching entry in `BUCKET_COUNTS` and `TOTAL_CASES`.
3. If the case introduces a new synthetic identity, add it to the whitelist in `tests/test_w2_eval_no_real_phi.py`.
4. If the case references a fixture key that is not yet generated, add a builder to `tests/fixtures/eval/_generate_eval_corpus.py` and regenerate the corpus.
5. Run `python3 -m pytest agent-api/tests -k "eval" -q` to confirm bucket-count and PHI guards pass before opening a PR.

After a new rubric is added, refresh `agent-api/evals/baseline.json` so the diff gate has a non-zero floor for the new metric.
