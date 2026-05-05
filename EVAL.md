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
