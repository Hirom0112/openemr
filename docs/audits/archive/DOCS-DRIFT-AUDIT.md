# Co-Pilot Documentation Drift Audit

**Scope:** Co-Pilot docs only — `agent-api/`, `ARCHITECTURE.md`, `W2_ARCHITECTURE.md`, `USERS.md`, `AUDIT.md`, `README.md`, `TODO.md`, `EVAL.md`, `SUBMISSION.md`. Read-only. Code is ground truth.
**Date:** 2026-05-07
**Cwd:** `/Users/hirom/Desktop/repos-gauntlet/openemr`

---

## 1. Grounding-read confirmation (one line each)

1. `agent-api/CLAUDE.md` — Co-Pilot project rules; *itself* flags ARCH §6.1 "47 test cases" as stale and lists drift items (lines 50-84) — meta-evidence that drift is known but not fixed.
2. `W2_ARCHITECTURE.md` (1,481 lines) — single-source-of-truth for W2; §11 still anchors the eval gate at "50 cases" / "5 boolean rubrics" throughout (W2_ARCHITECTURE.md:13, 1173, 1187-1196).
3. `ARCHITECTURE.md` (846 lines) — W1 design doc; §6.1 hard-codes "47 test cases organized into five categories" (ARCHITECTURE.md:517) with a category table that has no marker counterpart in code.
4. `README.md` (root, 203 lines) — submission landing page; mixes "98-case golden set, 14 boolean rubrics" (README.md:15), "50-case eval gate" (README.md:5, 39), "W1 prompt-eval (47 cases)" (README.md:39).
5. `TODO.md` (760 lines) — Phase 5 marks "50 cases · **7 rubrics**" as shipped (TODO.md:62); Phase 9.9 entry says "Total: 156 cases" (TODO.md:636, 709).
6. `agent-api/tests/` — 100 test files, **1,250 tests collected** by pytest (`pytest --collect-only -q | tail`, ~2.2s); 1,192 tagged `hard_failure`, 295 tagged `clinical_accuracy`, 12 tagged `smoke`.
7. `agent-api/pytest.ini` (13 lines) — declares 4 markers: `hard_failure` (100% gate), `clinical_accuracy` (95% gate), `live_api`, `smoke` (pytest.ini:8-12). No `w2_eval` marker.
8. CI config: `.github/workflows/copilot-eval.yml` is the live W2 gate; `.github/workflows/copilot-eval-nightly.yml` mirrors nightly. `.git/hooks/pre-push` is installed locally. Workflow strict-markers + strict-config; runs full pytest then asserts `hard_failure` collection non-empty.
9. `agent-api/.importlinter` — defines 19 architectural contracts across 21 root packages; no eval-count references.

---

## 2. Phase 1 — Eval-count ground truth

### 2.1 Canonical numbers (from code, not docs)

| Metric | Count | Source | Verification command |
|---|---|---|---|
| pytest tests collected (full repo, all of `agent-api/tests/`) | **1,250** | `pytest --collect-only -q` | `cd agent-api && python3 -m pytest --collect-only -q | tail -1` |
| pytest tests tagged `hard_failure` | **1,192** | `tests/conftest.py:21` enforces marker | `pytest --collect-only -q -m hard_failure | tail -1` |
| pytest tests tagged `clinical_accuracy` | **295** | conftest enforced marker | `pytest --collect-only -q -m clinical_accuracy | tail -1` |
| pytest tests tagged `smoke` | **12** | `pytest.ini:12` advisory | `pytest --collect-only -q -m smoke | tail -1` |
| pytest test files | **100** (`agent-api/tests/test_*.py`) | `find agent-api/tests -name 'test_*.py' \| wc -l` | — |
| W2 golden-set fixture cases (runtime `len(CASES)`) | **156** | `tests/fixtures/w2_eval_cases.py` (after Phase 9.9 multimodal append + annotated bolt-on) | `python3 -c "from tests.fixtures.w2_eval_cases import CASES; print(len(CASES))"` |
| W2 golden-set static `TOTAL_CASES` literal | **124** at module top, **mutated to 156** by `_load_annotated_cases()` and Phase 9.9 multimodal block | `tests/fixtures/w2_eval_cases.py:1401, 1552-1553` | `grep -n TOTAL_CASES tests/fixtures/w2_eval_cases.py` |
| `BUCKET_COUNTS` declared sum | **124** (12 buckets, max 36 = `bbox_gt`) | `tests/fixtures/w2_eval_cases.py:1383-1399` | — |
| `BUCKET_COUNTS` runtime sum (asserted by `test_bucket_counts_match`) | **156** at module-load (mutated by `_load_annotated_cases` and Wave 9.9) | `tests/test_w2_eval_bucket_counts.py:11-17` | — |
| `case_id` literal occurrences in `w2_eval_cases.py` | **133** (greps the file; not the runtime case count) | `evals/README.md:7` | `grep -c "case_id" tests/fixtures/w2_eval_cases.py` |
| W2 modalities (runtime) | 12 distinct: `typed_pdf=27, intake_form=26, table_heavy=23, multi_column=12, photo_capture=12, scanned_pdf=11, synthetic=9, hl7_v2=8, xlsx_workbook=8, docx_referral=8, tiff_fax=8, unknown=4` | python introspection of `CASES` | — |
| W2 buckets (runtime) | 12 distinct: `bbox_gt=36, lab_nominal=22, intake_nominal=22, mixed_content=12, wrong_patient=11, low_quality_scan=10, evidence_retrieval=10, unknown_nominal=8, blank_noise=7, missing_data=7, wrong_type_hint=6, intra_doc_conflict=5` | python introspection | — |
| Rubrics defined in `evals/baseline.json` | **18 distinct keys** at top level (counted): `schema_valid`, `citation_present`, `citation_resolvable`, `citation_row_match`, `citation_token_match`, `correct_critic_decision`, `factually_consistent`, `safe_refusal`, `no_phi_in_logs`, `provenance_chain`, `critic_false_positive_rate`, `citation_iou`, `quarantine_audit_emitted`, `no_unconfirmed_writes`, `stage_failure_audit_emitted`, `tiff_all_pages_ocrd`, `synthetic_marker_not_extracted`, plus `per_modality` map | `evals/baseline.json:1-18` | — |
| Rubrics implemented in `evals/rubrics_mechanical.py` + `evals/rubrics_llm.py` | mechanical + LLM rubrics; `evals/README.md` enumerates **11 mechanical + 3 LLM-graded = 14**, of which 11 carry `min_threshold` / advisory floor (`evals/README.md:29-53`) | `evals/rubrics_mechanical.py`, `evals/rubrics_llm.py` | — |
| Tools in `TOOL_REGISTRY` (dispatcher) | **5** (`get_census_summary`, `get_patient_briefing`, `query_patient_records`, `get_medication_safety`, `generate_handoff`) | `agent/tool_registry.py:24-30` | — |
| Tools total (incl. direct-call) | **6** (registry + `get_triage_rationale` in `DIRECT_TOOL_REGISTRY`) | `agent/tool_registry.py:34` | — |
| FastAPI routes registered in `main.py` | **27 distinct `@app.post/get` decorators** | `grep -nE '@app\.(post\|get\|put\|delete\|patch)' main.py` | — |
| Required-marker enforcement set | `{"hard_failure", "clinical_accuracy"}` | `tests/conftest.py:21` | — |

### 2.2 What "the 5 rubric categories" actually means

`agent-api/CLAUDE.md` and the user spec for this audit reference five rubric categories: `schema_valid`, `citation_present`, `factually_consistent`, `safe_refusal`, `no_phi_in_logs`. Code reality:

- All five exist in `evals/baseline.json:2-10` (and three more — `correct_critic_decision`, `citation_resolvable`, `provenance_chain` — sit alongside as graded rubrics).
- `schema_valid`, `citation_present`, `no_phi_in_logs` live in `evals/rubrics_mechanical.py`.
- `factually_consistent` and `safe_refusal` live in `evals/rubrics_llm.py` and skip when `ANTHROPIC_API_KEY` is unset (CI runs them as auto-skipped — `.github/workflows/copilot-eval.yml:30`).
- The W2_ARCH §11.2 table (W2_ARCHITECTURE.md:1187-1196) lists six rubrics: those five plus `correct_critic_decision`. So the doc itself never said "five" cleanly — yet `W2_ARCHITECTURE.md:13` says "five boolean rubrics."

There is no marker named `w2_eval` and no marker named for any of the rubric categories — none was ever written. The categories are case-level boolean scores, not pytest markers.

---

## 3. Phase 1 — Eval-count contradiction matrix

| File:line | Claim (verbatim) | Ground truth | Verdict |
|---|---|---|---|
| `ARCHITECTURE.md:517` | "The evaluation suite contains 47 test cases organized into five categories." | 1,250 pytest tests; W2 golden set has 156 cases; the "five categories" don't exist as markers anywhere in code. | ❌ wrong number; ❌ stale category model |
| `ARCHITECTURE.md:519` | "the full 47-test suite automatically" (CI gate trigger) | CI runs the full pytest suite (1,250); no "47-test suite" exists. | ❌ wrong number |
| `ARCHITECTURE.md:530-539` | Category table: 8+6+5+5+6+8+5+4 = 47 | Sum totals 47 cases but no marker / fixture file actually maps to these counts; the closest analog is `tests/conftest.py:21 REQUIRED_MARKERS` of just `{"hard_failure","clinical_accuracy"}`. The "Latency — per use case" sub-category has no enforcement at all. | ❌ category model never existed in code |
| `ARCHITECTURE.md:564, 769, 827` | "re-run the full 47-test eval suite" (×3) | Same — no 47-test suite. | ❌ wrong number (×3) |
| `W2_ARCHITECTURE.md:13` | "fifty cases scored against five boolean rubrics" | 156 runtime cases; six (or seven, including provenance_chain) boolean rubrics gated. | ❌ wrong number; ❌ wrong rubric count |
| `W2_ARCHITECTURE.md:1126` | "Sampled from a fixed query workload (the 50 eval cases ...)" | 156 cases; 12 buckets including 36 `bbox_gt` and 32 multimodal added in Phase 9.9. | ❌ wrong number |
| `W2_ARCHITECTURE.md:1173` | "### 11.1 Case mix (50 total)" + bucket table summing to 50 (12+10+6+4+5+4+4+3+2) | Runtime buckets total 156; declared bucket table in code (`BUCKET_COUNTS`, file:1383-1399) sums to 124 statically and is mutated to 156 at module load. | ❌ wrong number; ❌ stale bucket table |
| `W2_ARCHITECTURE.md:1187-1196` | Rubric table lists 6 rubrics. | Code: 18 keys in `baseline.json`; `evals/README.md` enumerates 14 boolean rubrics + 3 LLM-graded. | ❌ stale (rubric set has grown 3×) |
| `W2_ARCHITECTURE.md:1213-1219` | Baseline JSON example with `pass_rate: 1.00`, `0.96`, `0.94`, `0.96`, `1.00` | Actual `baseline.json:2-10` numbers: `0.8523`, `0.8523`, `0.4091`, `0.9886`, `0.5227`, `1.00`. | ❌ wrong numbers (illustrative-but-wrong example values now drift from real baseline) |
| `W2_ARCHITECTURE.md:1247-1248` | "Full 50-case run" (×2 — PR job + nightly) | Full 156-case run. | ❌ wrong number |
| `W2_ARCHITECTURE.md:1261` | "All 50 cases sourced from synthetic 25-patient panel ..." | 156 cases drawn from the panel + Wave 2C synthetic generator + Wave 2E annotated + Phase 9.9 multimodal corpus. | ❌ wrong number |
| `W2_ARCHITECTURE.md:1339, 1414, 1434, 1453` | "50-case eval suite", "50 golden cases", "50-case W2 suite", "50-case CI suite" (×4) | Same — 156. | ❌ wrong number (×4 in same doc) |
| `README.md:5` | "regression PR #1 hard-failing the 50-case suite" | The regression PR is real but the suite is 156 cases. | ❌ wrong number |
| `README.md:15` | "98-case golden set, 14 boolean rubrics" | 156 cases; 14 rubrics is approximately right by `evals/README.md:29-53` count. | ❌ wrong case count; ✅ rubric count roughly matches |
| `README.md:39` | "50-case eval gate \| W1 prompt-eval (47 cases)" | Same — 156 cases. The "W1 prompt-eval (47 cases)" is also wrong: there is no `test_prompt_eval.py` file in `agent-api/tests/` (verified by `find`). | ❌ wrong number; ❌ refers to a file that does not exist |
| `SUBMISSION.md:3, 23, 29, 51, 76` | "50-case CI suite" (×5), "50 cases × 7 boolean rubrics" | 156 cases; 14 rubrics in `evals/README.md`, 18 keys in baseline. | ❌ wrong number (×5); ❌ wrong rubric count |
| `SUBMISSION.md:35` | "204 W2 backend tests, 0 failures" | pytest collects 1,250 tests in `agent-api/tests/`; "W2 backend" is not a defined subset in code. | 🟦 unverifiable (no marker / file pattern matching this count) |
| `EVAL.md:5` | "534 test cases across 44 test files" | 1,250 tests across 100 files. (The `EVAL.md` author's "Authoritative count" command is correct; the cited result is stale by ~2.3×.) | ❌ wrong number (off by 716 tests, 56 files) |
| `EVAL.md:13-25` | Table claiming `test_triage_rules.py` 23, `test_criteria_extractor.py` 59, `test_synthetic_patients.py` 38, `test_prompt_eval.py` 46, `test_verification.py` 20, `test_medication_safety.py` 13 | Several of those files don't exist (`test_prompt_eval.py`, `test_verification.py`, `test_medication_safety.py` not in `agent-api/tests/` — verified). The repo has `test_medication_safety_cache.py`, `test_medication_safety_endpoint_shape.py`, `test_medication_safety_unification.py`. | ❌ table references non-existent files; ❌ counts unverifiable |
| `EVAL.md:69` | "the gate is now scored against **seven** boolean rubrics" | Closer to truth than other docs but still wrong: `evals/README.md:29-53` lists 11 mechanical + 3 LLM = 14; `baseline.json` has 18 keys. | ⚠ ambiguous — seven was true at one point; the gate has grown |
| `EVAL.md:88-150` | "W2 88-case eval suite" / "Total: **88**" / bucket table summing to 88 | 156 cases. The 88 figure traces to a pre-Wave-2C / pre-Phase-9.9 snapshot. | ❌ wrong number |
| `EVAL.md:101, 121, 127, 133` | Bucket sub-totals (47 / 34 / 22 / 9) summing to 88 | Runtime bucket totals: 22 + 22 + 8 + 7 + 12 + 11 + 10 + 6 + 5 + 10 + 36 + 7 = 156. The doc's `lab_nominal: 17` / `intake_nominal: 17` is also out of date — runtime is 22 each. | ❌ wrong numbers throughout |
| `EVAL.md:158` | "the full 88-case suite locally" | 156. | ❌ wrong number |
| `EVAL.md:170-175` | "Bump the matching entry in `BUCKET_COUNTS` and `TOTAL_CASES`" | True — but the procedure is followed inconsistently because the multimodal block mutates `TOTAL_CASES` at runtime (`tests/fixtures/w2_eval_cases.py:1552-1553`). | ⚠ procedure exists, code partially circumvents it. |
| `TODO.md:62` | "50 cases · **7 rubrics** (added provenance_chain)" | 156 cases; 14+ rubrics. Was true at Phase 5 close but never updated when Wave 2C/2E/9.9 grew the suite. | ❌ stale |
| `TODO.md:347` | "50 cases in `tests/fixtures/w2_eval_cases.py`, 6 boolean rubrics" | Same — stale Phase-5 wording. | ❌ stale |
| `TODO.md:373, 388` | "full 50-case suite", "50-case suite" | Same. | ❌ stale (×2) |
| `TODO.md:636` | "Total: 156 cases" | ✅ correct — TODO.md is the only doc that has been updated. | ✅ matches |
| `TODO.md:709` | "156 eval cases (124 prior + 32 multimodal)" | ✅ correct. | ✅ matches |
| `agent-api/evals/README.md:7` | "Count: 98 cases (counted via `grep -c "case_id"`)" | `grep -c "case_id"` returns **133**; runtime `len(CASES)` is **156**. The grep technique under-counts because multimodal cases use indirect constructors. | ❌ wrong number; ❌ counting methodology produces a third wrong answer |
| `agent-api/evals/README.md:18` | "MVP rubric calls for 50-case golden set; this suite ships ~2× that" | True directionally — 156 is ~3× the 50-case spec, not 2×. | ⚠ ambiguous |
| `agent-api/evals/README.md:11-15` | per-modality breakdown: `intake_form 26, typed_pdf 15, multi_column 12, scanned_pdf 11, table_heavy 11, synthetic 9` | Runtime: `typed_pdf=27, intake_form=26, table_heavy=23, multi_column=12, photo_capture=12, scanned_pdf=11, synthetic=9, hl7_v2=8, xlsx_workbook=8, docx_referral=8, tiff_fax=8, unknown=4`. The README is missing 5 modalities entirely (photo_capture, hl7_v2, xlsx_workbook, docx_referral, tiff_fax, unknown) and has stale typed_pdf/table_heavy counts. | ❌ stale + incomplete |
| `agent-api/CLAUDE.md:59-64` | Self-aware drift entry: "ARCH §6.1 says '47 test cases organized into five categories' ... the latency category in §6.1 has no corresponding marker in code." | ✅ correct meta-claim — flags the issue but the issue still hasn't been fixed in `ARCHITECTURE.md`. | ✅ matches reality (still needs action elsewhere) |

**Phase-1 contradiction count: 33 outdated/wrong claims across 9 files.** Two files (`TODO.md`, `agent-api/CLAUDE.md`) tell the truth; everything else is stale.

---

## 4. Phase 2 — Tool inventory contradictions

**Ground truth:** `agent-api/agent/tool_registry.py:24-35`. 5 conversational tools in `TOOL_REGISTRY`, 1 direct-call tool (`get_triage_rationale`) in `DIRECT_TOOL_REGISTRY`. Total: **6 tools defined in `agent/tools/__init__.py`**.

| File:line | Claim | Ground truth | Verdict |
|---|---|---|---|
| `ARCHITECTURE.md:17` | "the five tools" | 6 tools (5 conversational + 1 direct). | ⚠ ambiguous — "5 conversational" is the dispatcher count, but the doc never disambiguates |
| `ARCHITECTURE.md:319-327` ("§4.5 Tool Definitions" table) | Lists exactly 5 tools: `get_census_summary`, `get_patient_briefing`, `query_patient_records`, `get_medication_safety`, `generate_handoff` | Misses `get_triage_rationale` (defined at `agent/tools/__init__.py:1091`, registered as direct-call only). | ❌ outdated — same drift `agent-api/CLAUDE.md:55-58` already flags |
| `ARCHITECTURE.md:654` | "single-agent, five-tool problem" | 6 tools; W2 ships a multi-agent supervisor + workers + critic on LangGraph. | ❌ stale — both the count and the "single-agent" framing are wrong post-W2 |
| `ARCHITECTURE.md:637, 640` | references `query_patient_records`, "raw Anthropic SDK + custom checkpointer" | ✅ those names are correct in code, but §8 frames LangGraph as v2; W2 ships LangGraph today (`requirements.txt:62`, `agent/graph/`). | ⚠ ambiguous — names match, framing stale |
| `W2_ARCHITECTURE.md` | No tool enumeration in W2 doc; W2 talks about graph nodes (supervisor, workers, critic) | n/a | n/a |

---

## 5. Phase 2 — Route contradictions

**Ground truth (`grep -nE '@app\.(post\|get\|put\|delete\|patch)' agent-api/main.py`):** 27 routes, including:

```
POST /audit/destruction-record (main.py:301)
GET  /health (387)            GET  /fhir/patient/{patient_id} (401)
GET  /diag/fhir (412)         POST /triage/census (494)
POST /briefing/{patient_id} (603)   POST /session/{session_id}/query (635)
GET  /medication/safety/{patient_id} (652)
POST /handoff/generate (698)   POST /handoff/generate/stream (724)
POST /agent/triage_rationale/{patient_id} (816)
POST /agent/query (841)
GET  /agent/prefetch/status (928)   POST /agent/prefetch (951)
POST /agent/client-timing (1235)
POST /session/{session_id}/message (1261)   GET /session/{session_id}/history (1275)
POST /document/ingest (2035)
POST /evidence/search (2665)
POST /agent/w2/dispatch (2708)
POST /document/post-ingest-context (3054)
POST /document/{document_reference_id}/chat (3183)
GET  /document/quarantine (3379)
POST /document/quarantine/{quarantine_id}/claim (3413)
POST /document/quarantine/{quarantine_id}/match (3483)
POST /document/quarantine/{quarantine_id}/reject (3566)
```

| File:line | Claim | Ground truth | Verdict |
|---|---|---|---|
| `agent-api/CLAUDE.md:65-67` | Says ARCH §4 has no §4.6 and that `/agent/triage_rationale/{patient_id}` (main.py:811) and `/agent/query` (main.py:836) are not enumerated in the doc | main.py line numbers shifted to **816** and **841**. Otherwise ✅. | ⚠ ambiguous — line numbers off by 5 each (drift since the doc was written) |
| `ARCHITECTURE.md` | Does not list `/agent/query`, `/agent/triage_rationale`, `/document/ingest`, `/agent/w2/dispatch`, `/evidence/search`, `/document/quarantine/*`, `/document/post-ingest-context`, `/document/{id}/chat`, `/agent/prefetch*`, `/agent/client-timing`, `/session/*` anywhere as actual routes | All present and live in `main.py`. | ❌ entire section omitted |
| `W2_ARCHITECTURE.md:382-385, 1382-1387` | Table lists `POST /document/ingest`, `POST /document/scan_unprocessed`, `GET /document/{id}/preview`, `POST /document/{id}/reclassify`, `POST /evidence/search`, `POST /agent/w2/dispatch` | Code reality: `/document/scan_unprocessed`, `/document/{id}/preview`, `/document/{id}/reclassify` **do not exist** in `main.py`. The path A passive-ingest endpoint is a Phase 8 deferral (acknowledged at `SUBMISSION.md:107`). | ❌ doc lists 3 routes that are not registered |
| `W2_ARCHITECTURE.md:1383-1387` (Surface table) | "POST /document/scan_unprocessed — Path A passive ingest" | Phase 8 deferral; not implemented. `SUBMISSION.md:107` acknowledges. | ❌ stale (route promised by W2_ARCH but not built; `SUBMISSION.md` is honest about this, `W2_ARCHITECTURE.md` is not) |
| `SUBMISSION.md:74-75` | `POST /agent/w2/dispatch` SSE; `POST /evidence/search` | ✅ both present at main.py:2708 and 2665. | ✅ matches |
| `README.md:33-35` | `POST /document/ingest`, `POST /agent/w2/dispatch (SSE)`, `POST /evidence/search` | ✅ all present. | ✅ matches |

---

## 6. Phase 2 — Architecture decision contradictions

| File:line | Claim | Ground truth | Verdict |
|---|---|---|---|
| `ARCHITECTURE.md:17` | "agent uses the raw Anthropic SDK rather than a framework like LangGraph" | True for W1 dispatcher; W2 ships LangGraph for the doc-ingest graph (`requirements.txt:62 langgraph==0.2.60`, `agent/graph/build.py`). | ⚠ stale framing; ARCH-level doc has not been updated for W2 split |
| `ARCHITECTURE.md:644-654` (§8.1) | "v1 uses the raw Anthropic SDK ... LangGraph is the designated v2 orchestration framework. Adoption is triggered by ... when the second agent type is built." | Already adopted: `agent/graph/{build.py,state.py,nodes/}` ships supervisor + 4 workers + critic; LangGraph 0.2.60 pinned. The trigger condition has fired but ARCH §8.1 is still phrased as a future. | ❌ stale — `agent-api/CLAUDE.md:72-78` already flags this drift |
| `ARCHITECTURE.md:564` | "set to `claude-sonnet-4-6` at time of writing" | Per `agent-api/config.py` — no direct check here, but the model variable is `CLAUDE_MODEL_ID`. ARCH never updated for any later Sonnet version. | 🟦 unverifiable from this audit (need to read config.py); flag for owner |
| `W2_ARCHITECTURE.md:158, 183, 283` and `SUBMISSION.md:88-90`, `README.md:43` | FHIR Binary POST returns 404; legacy REST upload returns 401; custom JWT path is the live ingestion. | ✅ multiple docs agree, `agent-api/CLAUDE.md:79-84` confirms the v1 deviation. | ✅ matches |
| `ARCHITECTURE.md` (entire doc) | Never mentions the FHIR Binary 404 / custom JWT-upload deviation; does not reference `oe-module-clinical-copilot/public/upload.php` or `observation.php` | `W2_ARCHITECTURE.md §4.2.1 / §4.2.2` documents the deviation. | ❌ ARCH is silent on the most consequential deployed-build deviation |

---

## 7. Phase 2 — Use-case contradictions

**Ground truth:** `USERS.md:124-258` defines UC-1 through UC-5 (Morning Triage / Pre-Encounter Briefing / Targeted Record Query / Medication Safety / Handoff Generation). 5 use cases. Latency targets at `USERS.md:308-312`.

| File:line | Claim | Ground truth | Verdict |
|---|---|---|---|
| `USERS.md:21` | "five use cases" | ✅ matches UC-1..UC-5. | ✅ matches |
| `ARCHITECTURE.md:227-327` (§4.1-4.5) | Maps 5 tools onto 5 use cases | ✅ structurally aligns. | ✅ matches |
| `USERS.md:308-312` SLA targets (UC-1 <15s, UC-2 <5s, UC-3 <3s, UC-4 <5s, UC-5 <20s) | vs `ARCHITECTURE.md:566-572` cost model (UC-1 ~$0.057, UC-2 ~$0.008, UC-3 ~$0.006, UC-4 ~$0.005, UC-5 ~$0.074) | Distinct figures, no contradiction; both consistent across docs. | ✅ matches |
| `EVAL.md:89` | "end-to-end latency benchmarks against UC-1/2/3 SLA targets" listed as not yet covered | `W2_ARCHITECTURE.md:1112-1156` (§10.4) commits to a p50/p95 latency report; `docs/latency_cost_report.md` referenced from `README.md:14`, `SUBMISSION.md:62`. | ⚠ EVAL.md is conservative; the latency-report commitment exists but the eval framework itself doesn't gate latency. Not a contradiction, just disjoint scope. |
| `AUDIT.md:71` | "UC-1 through UC-5 produce empty or nonsensical output" against demo DB | Audit predates synthetic-panel mitigation; current ground truth: synthetic 25-patient panel is the test/demo dataset, the empty-demo-DB issue is mitigated. | ⚠ stale (April 2026 audit; mitigations shipped since) |

No structural UC contradictions across docs — UC-1..UC-5 stable.

---

## 8. Phase 2 — Deployed-URL contradictions

| File:line | URL claim | Ground truth | Verdict |
|---|---|---|---|
| `SUBMISSION.md:11-13` | `https://copilot-agent-api-production.up.railway.app/health`, `/metrics`; `https://clinical-copilot-openemr-production.up.railway.app` | These are the canonical live URLs (as of 2026-05-07; `README.md:17,119` agree). | ✅ matches |
| `README.md:17, 119` | Same agent-api URL; same OpenEMR URL | ✅ matches | ✅ matches |
| `SUBMISSION.md:14`, `README.md:5` | "Demo video: `[VIDEO_LINK_HERE]`" — placeholder | Placeholder remains in submission docs as of audit date. | ⚠ ambiguous — known placeholder, not yet filled |
| `SUBMISSION.md:31` | "Failure screenshot / artifact: `[SCREENSHOT_HERE]`" | Placeholder. | ⚠ ambiguous |
| `SUBMISSION.md:15` | `https://github.com/Hirom0112/openemr/tree/clinical-copilot` | Personal fork URL — present in submission docs. | ✅ matches (assuming no rename) |
| `SUBMISSION.md:27` | Regression PR `#1`: `https://github.com/Hirom0112/openemr/pull/1` | Submission claim, not verified by audit (would require GH API). | 🟦 unverifiable from local audit |

---

## 9. Phase 2 — Cost / latency contradictions

**Ground truth:** the only authoritative numbers should come from `docs/latency_cost_report.md` (referenced but not in scope of this audit) and Prometheus scrapes. Doc claims:

| File:line | Claim | Ground truth / cross-ref | Verdict |
|---|---|---|---|
| `ARCHITECTURE.md:564, 566-574` | UC-1 cost ~$0.057, UC-5 ~$0.074, full session ~$0.25; based on `claude-sonnet-4-6` and Anthropic pricing $3 input / $15 output / $0.30 cached | Pricing is plausible but model name may be stale (no live verification this audit). | 🟦 unverifiable from local audit |
| `W2_ARCHITECTURE.md:1102-1108` | Per-document ~$0.030; per-query ~$0.016 | Plausible projection; depends on `docs/latency_cost_report.md` for actuals. | 🟦 unverifiable from local audit |
| `W2_ARCHITECTURE.md:1110, 1262` | "~$0.50 per full PR run" → "~$100/month budget"; "Haiku for 4 of 5 LLM-judged rubrics; Sonnet only on `factually_consistent`" | Code reality (`evals/rubrics_llm.py`) — not inspected line-by-line. The "4 of 5 LLM rubrics" claim conflicts with `evals/README.md:43-53` which lists only **3** LLM-graded rubrics (`factually_consistent`, `safe_refusal`, `nearest_label_grounded`). | ❌ "4 of 5" rubric count contradicts `evals/README.md` (3 LLM rubrics) |
| `README.md:14` and `SUBMISSION.md:62` | "98% prompt-cache hit rate" | A scraped metric value baked into doc text; not verifiable from this audit. | 🟦 unverifiable from local audit; pin or remove if /metrics drifts |
| `ARCHITECTURE.md:582-590` | Force-refresh prewarm cost ~$0.15 / login | Plausible. | 🟦 unverifiable |

---

## 10. Phase 2 — Additional findings

### 10.1 Doc files referenced but absent (or path-mismatched)

- `EVAL.md:13-25` references several test files that **do not exist** in `agent-api/tests/`:
  - `test_prompt_eval.py` — not present (`find agent-api/tests -name 'test_prompt_eval*'` returns nothing). Closest: `tests/fixtures/prompt_eval_cases.py` is empty (0 `case_id` lines).
  - `test_verification.py` — not present. Closest: `tests/test_dispatcher_verification.py`, `tests/test_citation_verifier.py`.
  - `test_medication_safety.py` — not present. Closest: `tests/test_medication_safety_cache.py`, `test_medication_safety_endpoint_shape.py`, `test_medication_safety_unification.py`.
- `EVAL.md:59-62` references `docs/clinical-copilot/smoke-matrix.md` — existence not verified by this audit; likely stale path.
- `SUBMISSION.md:65` references `docs/DEMO_SCRIPT.md` — likely exists but not verified.

### 10.2 `evals/README.md` self-contradiction

- `evals/README.md:7` claims "98 cases" via `grep -c case_id` — that grep actually returns **133**, not 98. Author count, grep count, and runtime count (156) are three different numbers in the same paragraph.

### 10.3 Phase 9.9 (W2 multimodal expansion) doc-coverage gap

`TODO.md:636-682` documents the Phase 9.9 / Wave 2C / Wave 2E expansions and the additive bucket strategy (new `document_modality` lanes routed via `baseline.json:per_modality`). None of `ARCHITECTURE.md`, `W2_ARCHITECTURE.md`, `SUBMISSION.md`, `EVAL.md`, or `agent-api/evals/README.md` mention the multimodal expansion or the new modalities (`hl7_v2`, `xlsx_workbook`, `docx_referral`, `tiff_fax`, `photo_capture`). The eval suite's growth from 50 → 124 → 156 is invisible in user-facing submission docs.

### 10.4 `W2_ARCHITECTURE.md` baseline JSON example is wildly wrong

`W2_ARCHITECTURE.md:1213-1219` shows aspirational/illustrative baseline values (`schema_valid: 1.00`, etc.). Actual `baseline.json` ships with `schema_valid: 0.8523`, `correct_critic_decision: 0.4091`, `safe_refusal: 0.5227`. A reviewer reading the spec sees a system at 96-100% pass; a reviewer running the gate sees 41-100%. This is the most defense-risk-laden contradiction in the suite.

### 10.5 W2_ARCH §11.2 missing `correct_critic_decision` justification

`W2_ARCHITECTURE.md:1187-1196` lists 6 rubrics including `correct_critic_decision`, contradicting the same doc's "five boolean rubrics" claim two pages earlier (`W2_ARCHITECTURE.md:13`).

### 10.6 ARCH §6.1 latency category has no enforcement

`ARCHITECTURE.md:538` claims "Latency — per use case | 5 | 95%" — there is no latency marker, no latency gate, no per-UC latency assertion in `tests/conftest.py`, `pytest.ini`, or any test file.

---

## 11. Phase 3 — Reconciliation plan (ordered by impact, capped 25)

### Reconciliation 1 — `W2_ARCHITECTURE.md §11` "50 cases" → 156 cases (W2-doc credibility)

- **Contradicting files:** `W2_ARCHITECTURE.md:13, 1126, 1173, 1247-1248, 1261, 1339, 1414, 1434, 1453`.
- **Ground truth:** 156 cases at runtime; 12 buckets; `tests/fixtures/w2_eval_cases.py:1401` (static `124`) + Wave 2C/2E + Phase 9.9 multimodal expansion (`tests/fixtures/w2_eval_cases.py:1552-1553`).
- **Proposed canonical phrasing:** "The W2 eval gate runs over 156 cases organized into 12 buckets (lab/intake nominal, wrong-patient, wrong-type-hint, blank-noise, mixed-content, low-quality-scan, intra-doc-conflict, evidence-retrieval, missing-data, unknown-nominal, plus 36 bbox-GT cases and 32 multimodal cases across HL7v2 / XLSX / DOCX / TIFF). 50 cases is the original spec floor; the suite has since grown 3×."
- **Files to update (file:line — action):**
  - `W2_ARCHITECTURE.md:13` — replace "fifty cases" with "≥150 cases" or current count.
  - `W2_ARCHITECTURE.md:1126` — replace "the 50 eval cases".
  - `W2_ARCHITECTURE.md:1173` — re-title §11.1 "Case mix (156 total)" and rebuild bucket table from `BUCKET_COUNTS`.
  - `W2_ARCHITECTURE.md:1247-1248, 1261, 1339, 1414, 1434, 1453` — replace each "50-case" string.
- **Risk:** Medium. Numbers churn each time the suite grows; consider linking to a generated count or marking as "≥N at submission time, see TODO.md for current".

### Reconciliation 2 — `W2_ARCHITECTURE.md §11.5` baseline-example numbers replaced with stub or live values

- **Contradicting files:** `W2_ARCHITECTURE.md:1213-1219` vs `agent-api/evals/baseline.json:2-10`.
- **Ground truth:** `baseline.json:2-18` — 18 distinct rubric keys with current pass-rates and floors.
- **Proposed canonical phrasing:** "Live baseline shipped at `agent-api/evals/baseline.json`. Sample shape (illustrative, not authoritative): ..." — followed by an explicit pointer to the live file.
- **Files to update:** `W2_ARCHITECTURE.md:1208-1228` — replace illustrative JSON with a stub plus a "see live `baseline.json`" pointer; explicitly state the example values are not the actual gate values.
- **Risk:** High — defense-doc risk. If a reviewer compares the doc to live baseline, they'll see `0.41` vs `0.96` (`correct_critic_decision`) and conclude the system has regressed by 50pp.

### Reconciliation 3 — `ARCHITECTURE.md §6.1` "47 test cases organized into five categories" → reality

- **Contradicting files:** `ARCHITECTURE.md:517, 519, 530-539, 564, 769, 827`.
- **Ground truth:** 1,250 pytest tests; required markers `{hard_failure, clinical_accuracy}`; no "five categories" structure exists in code.
- **Proposed canonical phrasing:** "The W1 test suite runs as part of the broader Co-Pilot pytest suite (~1,250 tests), with two enforcement markers: `hard_failure` (must all pass; failure blocks deploy) and `clinical_accuracy` (95% gate). The W2 eval gate is described separately in `W2_ARCHITECTURE.md §11`."
- **Files to update:**
  - `ARCHITECTURE.md:517-541` — rewrite §6.1 entirely; either delete the category table or rebuild it from `pytest.ini` markers.
  - `ARCHITECTURE.md:564, 769, 827` — replace "47-test eval suite" with "the full pytest suite + `diff_baseline.py`".
- **Risk:** Medium. ARCH is a W1 doc; the W1/W2 split is also fundamentally unaddressed.

### Reconciliation 4 — `EVAL.md` total count and stale file table

- **Contradicting files:** `EVAL.md:5, 13-25, 88-150, 158, 170-175`.
- **Ground truth:** 1,250 tests / 100 files / W2 fixture = 156 cases / 12 buckets.
- **Proposed canonical phrasing:** Replace headline "534 test cases across 44 test files" with the live count via the doc's own "Authoritative count" command. Replace W2 88-case section with W2 156-case section. Drop or fix table entries pointing at `test_prompt_eval.py`, `test_verification.py`, `test_medication_safety.py` (none exist).
- **Files to update:** `EVAL.md:5, 13-25, 88-101, 121, 127, 133, 137-150, 158`.
- **Risk:** Medium. Doc was clearly written from a snapshot; a single rebuild + grep validation closes it.

### Reconciliation 5 — `SUBMISSION.md` "50-case CI suite" / "204 W2 backend tests"

- **Contradicting files:** `SUBMISSION.md:3, 23, 29, 35, 51, 76`.
- **Ground truth:** 156 cases; 1,250 pytest tests (unclear what "204 W2 backend" refers to — needs definition).
- **Proposed canonical phrasing:** Pick one of: (a) "150+ case CI suite", (b) cite the live count at submission lock-time and footnote the lock-date. For "204 W2 backend tests": either define the marker selection that yields 204 or remove the claim.
- **Files to update:** `SUBMISSION.md:3, 23, 29, 35, 51, 76`.
- **Risk:** High — this is the graded artifact. Reviewers will read submission verbatim.

### Reconciliation 6 — `ARCHITECTURE.md §4.5 Tool Definitions` — add `get_triage_rationale`

- **Contradicting files:** `ARCHITECTURE.md:319-327, 654`.
- **Ground truth:** `agent/tool_registry.py:34` registers `get_triage_rationale` as direct-call.
- **Proposed canonical phrasing:** Add a row: "`get_triage_rationale` | UC-1 census-row click | Direct-call (excluded from dispatcher's TOOL_REGISTRY) | Per-patient triage rationale narration".
- **Files to update:** `ARCHITECTURE.md:319-327` — add row; `ARCHITECTURE.md:654` — update "five-tool" framing.
- **Risk:** Low.

### Reconciliation 7 — `W2_ARCHITECTURE.md §4` route table — remove unimplemented routes

- **Contradicting files:** `W2_ARCHITECTURE.md:382-385, 1382-1387`.
- **Ground truth:** `/document/scan_unprocessed`, `/document/{id}/preview`, `/document/{id}/reclassify` are not registered in `main.py`.
- **Proposed canonical phrasing:** Mark each as "(deferred to Phase 8)" or remove; `SUBMISSION.md:107` already declares Path A as a deferral.
- **Files to update:** `W2_ARCHITECTURE.md:382-385, 1382-1387`.
- **Risk:** Medium — graded; reviewer probing the route surface will get 404s.

### Reconciliation 8 — `ARCHITECTURE.md §8.1` LangGraph as v2 → reality (already shipped)

- **Contradicting files:** `ARCHITECTURE.md:17, 644-654`.
- **Ground truth:** `requirements.txt:62 langgraph==0.2.60`; `agent/graph/{build.py,state.py,nodes/}` ships supervisor + 4 workers + critic.
- **Proposed canonical phrasing:** "W1 dispatcher uses raw Anthropic SDK + custom Checkpointer. W2 doc-ingest graph uses LangGraph 0.2.60 (`agent/graph/`). Both are live."
- **Files to update:** `ARCHITECTURE.md:17, 644-654` — rewrite framing.
- **Risk:** Medium. `agent-api/CLAUDE.md:72-78` already documents this drift.

### Reconciliation 9 — `agent-api/evals/README.md` — fix the case-count and modality table

- **Contradicting files:** `agent-api/evals/README.md:7, 11-15, 18`.
- **Ground truth:** 156 cases; 12 modalities (the README is missing 5).
- **Proposed canonical phrasing:** Replace `Count: 98 cases` with `Count: 156 cases (run `python3 -c "from tests.fixtures.w2_eval_cases import CASES; print(len(CASES))"`)`. Rebuild the modality table from `_FIXTURE_MODALITY` + Phase 9.9 additions.
- **Files to update:** `agent-api/evals/README.md:7, 11-15, 18`.
- **Risk:** Low.

### Reconciliation 10 — Rubric-count truth across docs

- **Contradicting files:** `W2_ARCHITECTURE.md:13, 1187-1196`; `EVAL.md:69`; `SUBMISSION.md:51, 76`; `agent-api/evals/README.md:29-53`.
- **Ground truth:** `baseline.json` has **18 distinct rubric keys** (incl. per_modality); `evals/README.md` enumerates **11 mechanical + 3 LLM = 14**.
- **Proposed canonical phrasing:** "14 boolean rubrics (11 mechanical + 3 LLM-graded), with 18 keys in `baseline.json` after per-modality breakdown."
- **Files to update:** `W2_ARCHITECTURE.md:13, 1187-1196`; `SUBMISSION.md:51, 76`; `EVAL.md:69`.
- **Risk:** Medium — reviewer math will catch the "5" / "6" / "7" / "14" inconsistency.

### Reconciliation 11 — `ARCHITECTURE.md` silent on FHIR Binary 404 deviation

- **Contradicting files:** `ARCHITECTURE.md` (entire doc); cross-ref `W2_ARCHITECTURE.md §4.2.1, §4.2.2`; `SUBMISSION.md:88-90`; `README.md:43`.
- **Ground truth:** Custom JWT path is the live ingestion (`oe-module-clinical-copilot/public/upload.php`).
- **Proposed canonical phrasing:** Add a "W2 deployment deviation" subsection cross-referencing `W2_ARCHITECTURE.md §4.2.1`.
- **Files to update:** `ARCHITECTURE.md` — new subsection.
- **Risk:** Medium — without it, ARCH gives a misleading impression of deployed state.

### Reconciliation 12 — `EVAL.md` table references files that don't exist

- **Contradicting files:** `EVAL.md:13-25`.
- **Ground truth:** No `test_prompt_eval.py`, `test_verification.py`, or `test_medication_safety.py` in `agent-api/tests/`.
- **Proposed canonical phrasing:** Rebuild the table from `find agent-api/tests -name 'test_*.py'`.
- **Files to update:** `EVAL.md:13-25`.
- **Risk:** Medium — reviewer running cited file paths gets "no such file".

### Reconciliation 13 — `README.md:39` "W1 prompt-eval (47 cases)"

- **Contradicting files:** `README.md:39`.
- **Ground truth:** No prompt-eval file exists with 47 cases.
- **Proposed canonical phrasing:** "W1 dispatcher tests under the same pytest gate; see `EVAL.md`."
- **Files to update:** `README.md:39`.
- **Risk:** Low.

### Reconciliation 14 — `TODO.md` Phase 5 / Phase-prep wording → 156 cases

- **Contradicting files:** `TODO.md:62, 347, 373, 388`.
- **Ground truth:** Phase-5 closed at 50 cases; current is 156.
- **Proposed canonical phrasing:** Annotate Phase 5 entries with a "current count: 156 (see Phase 9.9)" footnote rather than rewrite history.
- **Files to update:** `TODO.md:62, 347, 373, 388`.
- **Risk:** Low — `TODO.md` already gets the truth right at line 636/709.

### Reconciliation 15 — `ARCHITECTURE.md §6.1` latency category has no code enforcement

- **Contradicting files:** `ARCHITECTURE.md:538`.
- **Ground truth:** No latency marker / no latency gate exists.
- **Proposed canonical phrasing:** Either build a `latency` marker and enforce it (code change — out of scope) or delete the row.
- **Files to update:** `ARCHITECTURE.md:538`.
- **Risk:** Low (doc-only) but tracks tech debt.

### Reconciliation 16 — `evals/README.md` mechanical rubric count vs LLM rubric count

- **Contradicting files:** `evals/README.md:29-53`; `W2_ARCHITECTURE.md:1262` claims "Haiku for 4 of 5 LLM-judged rubrics".
- **Ground truth:** `evals/README.md` lists 3 LLM-graded rubrics (`factually_consistent`, `safe_refusal`, `nearest_label_grounded`).
- **Proposed canonical phrasing:** "Haiku 4.5 for `safe_refusal` and `nearest_label_grounded`; Sonnet 4.6 for `factually_consistent` (auto-rerun on disagreement)".
- **Files to update:** `W2_ARCHITECTURE.md:1262`.
- **Risk:** Low.

### Reconciliation 17 — `agent-api/CLAUDE.md` line-number drift in self-flagged drift list

- **Contradicting files:** `agent-api/CLAUDE.md:67`.
- **Ground truth:** `/agent/triage_rationale` is at `main.py:816` (doc says 811); `/agent/query` is at `main.py:841` (doc says 836). Off by 5.
- **Proposed canonical phrasing:** Re-run `grep -nE '@app\.' main.py` and update.
- **Files to update:** `agent-api/CLAUDE.md:55-84`.
- **Risk:** Low — but this is the doc that's supposed to be the drift index, so it should be re-grepped on every Co-Pilot session.

### Reconciliation 18 — `evals/README.md:7` self-contradicts (98 vs 133 vs 156)

- **Contradicting files:** `evals/README.md:7`.
- **Ground truth:** Author claim 98, actual grep 133, actual runtime 156.
- **Proposed canonical phrasing:** Replace the grep-based methodology with a runtime introspection: `python3 -c "from tests.fixtures.w2_eval_cases import CASES; print(len(CASES))"`.
- **Files to update:** `evals/README.md:7`.
- **Risk:** Low (but high embarrassment risk for a one-line eval-suite README).

### Reconciliation 19 — `SUBMISSION.md:35` "204 W2 backend tests"

- **Contradicting files:** `SUBMISSION.md:35`.
- **Ground truth:** Not derivable from any pytest filter shown to this audit.
- **Proposed canonical phrasing:** Either define the selector (e.g. `pytest agent-api/tests -k "agent or graph or document or evidence" --collect-only`) and cite the live count, or replace with "1,250 pytest tests across 100 files".
- **Files to update:** `SUBMISSION.md:35`.
- **Risk:** Medium — reviewer running test count will get 1,250.

### Reconciliation 20 — `EVAL.md:88-101, 121, 127, 133, 137-150` "88-case" / per-bucket totals

- **Contradicting files:** `EVAL.md:88-150`.
- **Ground truth:** 156 cases, 12 buckets per `BUCKET_COUNTS`.
- **Proposed canonical phrasing:** Rebuild from `BUCKET_COUNTS` post-mutation; add Phase 9.9 modality lanes.
- **Files to update:** `EVAL.md:88-150`.
- **Risk:** Medium.

### Reconciliation 21 — `agent-api/evals/README.md:11-15` modality breakdown missing 5 modalities

- **Contradicting files:** `agent-api/evals/README.md:11-15`.
- **Ground truth:** 12 modalities live; README lists 6.
- **Proposed canonical phrasing:** Rebuild from runtime `Counter(c.document_modality for c in CASES)`.
- **Files to update:** `agent-api/evals/README.md:11-15`.
- **Risk:** Low.

### Reconciliation 22 — `ARCHITECTURE.md:564` `claude-sonnet-4-6` model name

- **Contradicting files:** `ARCHITECTURE.md:564`.
- **Ground truth:** Whatever `agent-api/config.py` resolves at deploy time. Audit has not verified.
- **Proposed canonical phrasing:** "Pricing is based on `${CLAUDE_MODEL_ID}` (current value: see `agent-api/config.py`)."
- **Files to update:** `ARCHITECTURE.md:564`.
- **Risk:** Low — flag for owner only.

### Reconciliation 23 — `W2_ARCHITECTURE.md` does not document Phase 9.9 multimodal expansion

- **Contradicting files:** `W2_ARCHITECTURE.md` (entire doc); cross-ref `TODO.md:636-682`.
- **Ground truth:** 32 cases added across HL7v2 / XLSX / DOCX / TIFF; 4 new modalities; 5 new mechanical rubrics (`quarantine_audit_emitted`, `no_unconfirmed_writes`, `stage_failure_audit_emitted`, `tiff_all_pages_ocrd`, `synthetic_marker_not_extracted`).
- **Proposed canonical phrasing:** Add §11.9 "Phase 9.9 multimodal expansion" linking to `parsers/{hl7,xlsx}/`, `documents/{docx_loader.py,tiff_loader.py}`, and the new rubrics.
- **Files to update:** `W2_ARCHITECTURE.md` — new subsection.
- **Risk:** Medium for review polish; low for demo-day.

### Reconciliation 24 — `README.md:14, SUBMISSION.md:62` "98% prompt-cache hit rate"

- **Contradicting files:** `README.md:14`; `SUBMISSION.md:62`.
- **Ground truth:** baked-in scrape value; not verified live.
- **Proposed canonical phrasing:** "≥95% prompt-cache hit rate per the latest `/metrics` scrape (see `docs/latency_cost_report.md` for live numbers)."
- **Files to update:** `README.md:14`; `SUBMISSION.md:62`.
- **Risk:** Low.

### Reconciliation 25 — `AUDIT.md` UC-1..UC-5 demo-data finding stale

- **Contradicting files:** `AUDIT.md:51, 71, 91-128`.
- **Ground truth:** Synthetic 25-patient panel + Wave 2C generated corpus is the live test/demo dataset.
- **Proposed canonical phrasing:** Add a top-of-document "April 2026 audit; resolved findings list at end" block; mark Findings #1, #5 as mitigated.
- **Files to update:** `AUDIT.md:1-26` (header) and findings table at `AUDIT.md:69-86`.
- **Risk:** Low — historical audit docs commonly stay frozen, but flagging mitigations is honest hygiene.

---

## 12. Summary

### Top 5 most damaging contradictions (defense-doc / demo-defensibility risk)

1. **`W2_ARCHITECTURE.md:1213-1219` baseline-JSON example values** — illustrative numbers (`0.96-1.00`) drift wildly from live `baseline.json` (`0.41-1.00`). A reviewer comparing spec to gate will see a 50pp regression that doesn't exist.
2. **`SUBMISSION.md:3, 23, 29, 51, 76` "50-case CI suite" claim** — the suite is 156 cases; the submission's headline number is the original spec floor, not the shipped reality. Worse, it makes the impressive 3× expansion invisible.
3. **`ARCHITECTURE.md:517-539` "47 test cases organized into five categories"** — the categories don't exist as markers, the count is off by 26×, and `agent-api/CLAUDE.md` already flags it but the fix is still unmade.
4. **`W2_ARCHITECTURE.md:382-385, 1382-1387` route surface lists 3 unimplemented endpoints** (`/document/scan_unprocessed`, `/document/{id}/preview`, `/document/{id}/reclassify`). A reviewer probing the API gets 404s.
5. **`EVAL.md:5, 13-25` headline "534 test cases across 44 test files" + table referencing files that don't exist** — the Eval doc cites 3 nonexistent test files (`test_prompt_eval.py`, `test_verification.py`, `test_medication_safety.py`), and the headline count is off by ~700 tests.

### Overall doc health: **LOW**

Phase 1 alone has 33 wrong/stale claims across 9 files about a single number (eval count) that the user spec singled out as "most acutely contradictory." The fact that `agent-api/CLAUDE.md` itself flags drift it has not fixed in `ARCHITECTURE.md` is a structural smell — the drift-index doc is documenting drift instead of forcing it shut. `TODO.md` is the only doc telling a consistent story. Submission-graded artifacts (`SUBMISSION.md`, `README.md`, `W2_ARCHITECTURE.md`, `EVAL.md`) all under-state the suite size by 3× and over-state the route surface. Reviewers running grep against the cited numbers will get four different answers depending on which doc they trust.

The fix path is mostly find-and-replace — there is no architectural lie, only stale snapshots. Reconciliations 1, 2, 3, 5, 7, 12 close the demo-defensibility risk in roughly 80 line edits.

---

*Audit generated read-only. No docs or code were modified by this run. All numeric claims are reproducible via the verification commands in §2.1.*
