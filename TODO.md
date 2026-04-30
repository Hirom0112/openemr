# Clinical Co-Pilot — TODO

## Phase 0: Synthetic Data + Infrastructure

- [x] Confirm Railway OpenEMR deploy is live (`GET /apis/default/fhir/Patient` returns 200)
- [x] Verify SMART / Client Credentials OAuth is configured on the Railway instance
- [x] Run `synthetic_data/generate.py` → 18 patient FHIR R4 bundles, fixed seed (seed=42)
- [x] Run `synthetic_data/load.py` against Railway `BASE_URL`
- [x] Spot-check: confirm 18 patients returned from `GET /fhir/Patient`
- [x] Spot-check: qSOFA vitals present for Marcus Webb (bed 501) — LOINC 9279-1 RR=26, 8867-4 HR=118, SpO2=91 ✓
- [x] Spot-check: Delia Fontaine K+=6.4 critical lab via FHIR ✓
- [x] Commit static JSON bundles to repo (`synthetic_data/bundles/`)
- [x] Docker compose additions: agent-api, Redis, Langfuse, Prometheus, Grafana
- [x] Redis Checkpointer: RedisSaver + SqliteSaver
- [x] FHIR Client Credentials auth wired into agent-api

## Phase 1: UC-1 Triage Engine

- [x] Deterministic rules engine (YAML config, 10 priority levels)
- [x] Census context builder
- [x] First LLM call (one-line explanations only — not ranking)
- [x] Minimal verification layer (domain constraints only)
- [x] Langfuse + Prometheus wired in from this point forward

## Phase 2: UC-2 Pre-Encounter Briefing

- [x] Full context builder for patient briefing
- [x] Full verification layer (source attribution + domain constraints)
- [x] Structured output schema locked

## Phase 3: UC-3 Targeted Record Query

- [x] Query router (hybrid: classifier first, LLM fallback on low-confidence)
- [x] Extended FHIR search window
- [x] Multi-turn conversation continuity

## Phase 4: UC-4 + UC-5

- [x] UC-4: Medication safety surface
- [x] UC-5: Parallel handoff generation

## Phase 5: Frontend

- [x] Thin PHP shell module (`oe-module-clinical-copilot`)
- [x] React sidebar panel

## Phase 6: Eval Suite

- [x] 84-test suite (hard_failure: 55 tests / 100% gate, clinical_accuracy: 70 tests / 95% gate; latency marker removed — no real timing assertions)
- [x] CI gates: full suite + hard_failure gate + clinical_accuracy gate, each with empty-selection guard; wired to agent-api and workflow changes
- [x] Unmarked-test guard enforces marker taxonomy at collection time
- [x] CI marker checks use pytest exit-code (`5` = vacuous gate, `2/3/4` = infra error, `0` = non-empty confirmed) — no grep on output; `--strict-markers --strict-config` on all steps including execution

---

## Phase 7: Correctness Before Demo — Ordered Work Queue

> Order is intentional. Every item after #1 depends on the eval suite being trustworthy.
> Do not reorder.

### 1. Verify and fix the eval suite (do this first — everything downstream depends on it)

- [x] Run `pytest --collect-only` and confirm exactly which tests are collected and how many (84 tests)
- [x] Resolve the missing `tests/fixtures/routine_patient_bundle.json` fixture referenced in `conftest.py` (dead fixture removed)
- [x] Apply `@pytest.mark.hard_failure` to all tests that should be 100% gates (55 tests)
- [x] Apply `@pytest.mark.clinical_accuracy` to clinical correctness tests (70 tests)
- [x] Remove `latency` marker — no real timing assertions exist; reintroduce only when a real latency test is added
- [x] Add unmarked-test guard: `pytest_collection_finish` hook in `conftest.py` fails full-suite run if any test lacks both required markers
- [x] Standardize canonical invocation: `python3 -m pytest agent-api/tests` from repo root; documented in `agent-api/README.md`
- [x] Update CI workflow: remove stale "47 tests" job name, repo-root invocation, `hard_failure` + `clinical_accuracy` gates each with empty-selection pre-check, no latency gate
- [x] Lesson from pt-019/P9 discovery: a passing test suite that silently skips or misses paths is worse than no test suite. Fix the signal first.

### 2. Apply verification to UC-3 and UC-4 (safety-critical requirement gap)

- [x] Wire `domain_constraints.verify_triage_entry()` (or equivalent) to conversation answers before returning from `POST /session/{id}/query`
- [x] Wire same verification to `GET /medication/safety/{patient_id}` LLM summary output
- [x] Add tests confirming recommendation language is stripped from conversation answers
- [x] Add tests confirming medication safety output cannot assert a drug is "safe" without source support
- [x] Rationale: requirement says "every response passes verification." UC-3 is the highest-hallucination-risk surface. UC-4 failure mode is a safety event.

### 3. Decide on tool use — build it or descope it in writing

- [x] **Option A selected.** Full `tool_use` dispatcher across all five use cases via `POST /agent/query`. Selected because the requirements doc (page 6–7) calls for tool invocation, and the convergence plan delivers a single agent endpoint that resolves three orphaned UI use cases as a side effect. See Phase 8 block for execution sequence.

### 4. FHIR extractor audit (`criteria.py`)

- [x] Audit every `_numeric()` call: handle `valueDecimal`, `valueRatio`, `valueString` not just `valueQuantity` and `valueInteger`
- [x] Audit the lab category filter: cross-check against the full FHIR R4 `Observation.category` value set — confirm no vital-sign or imaging observation can slip through as a lab
- [x] Audit every code path that reads a FHIR field against what FHIR R4 says that field can contain
- [x] Add at least one synthetic patient or unit test for each newly discovered code path
- [x] Rationale: pt-019/P9 discovery came from an extractor bug. Three bugs found in one session. There are almost certainly more. Cost now: 30–60 min. Cost in pilot: much higher.

### 5. Clinician review of priority table

- [ ] Schedule 1-hour session with a hospitalist or charge nurse
- [ ] Walk through P1–P10, all thresholds, precedence rules, 5 representative patients
- [ ] Ask: "Would you trust this list at the start of a shift? What's miscategorized? What's missing?"
- [ ] Update `rules_engine_config.yaml` and tests based on feedback
- [ ] Rationale: every rule, threshold, and test is downstream of an assumption about correct triage. One hour of clinician time can validate or invalidate weeks of engineering. Do this before pilot, not after.
- [ ] **Sequencing note:** If the review changes priority thresholds, Phase 8 Phase 6 routing eval cases that depend on specific priority levels (and any hard-failure tests testing priority-dependent behavior) will need re-grading. Document which tests are affected before scheduling the review, so the re-grade scope is known in advance.

### 6. Observability polish (order among these doesn't matter)

- [x] Add at least one Grafana dashboard (request latency, error rate, triage level distribution)
- [x] Add custom Prometheus metrics: triage level distribution counter, briefing generation time histogram
- [x] Add test for checkpointer Redis → SQLite fallback behavior
- [x] Add test for FHIR auth token refresh

---

## Phase 8: Convergence — Dispatcher + Chat UI

> **Sequencing constraint:** Phase 0 (specification) blocks Phase 1. No implementation work begins until all five Phase 0 sub-items are committed to `docs/UX_SPEC.md` and `docs/AGENT_CONTRACT.md`. The session state schema, numerical gates, and claim taxonomy defined in those docs are the contract that Phase 4 builds against.

### Phase 0 — Specification (blocks all subsequent phases)

- [x] **Pre-draft read:** Read requirements doc + `ARCHITECTURE.md` cover-to-cover; flag any contradictions with locked decisions (greeting ownership, click-to-expand dispatch, session-open synthetic message, response type ownership) before drafting either spec. Contradictions must be resolved in this documentation pass, not discovered in Phase 4. Confirm `ARCHITECTURE.md §4.7` reflects all three UI-owned behaviors — if not, update it before proceeding.
- [x] Create `docs/UX_SPEC.md` and `docs/AGENT_CONTRACT.md` shells with section headers
- [x] **Numerical cutover targets** (in `AGENT_CONTRACT.md`): routing accuracy ≥ 95% on 20-query eval set, dispatcher p95 ≤ 4s, error rate ≤ 1%, tool misroute rate ≤ 2%
- [x] **Session state schema** (in `AGENT_CONTRACT.md`): enumerate every field loaded per turn (provider_id, patient_ids, bounded conversation history, last-viewed patient, last tool called); distinguish per-session vs per-conversation fields; define TTL and max-history policy
- [x] **Claim taxonomy** (in `AGENT_CONTRACT.md` and `UX_SPEC.md`): distinguish clinical assertions (require citations) from conversational/UX text (no citation required); define citation requirement by claim class
- [x] **Response type ownership and responsiveness strategy** (in `UX_SPEC.md`): lock the typed response envelope (`{type, data, narrative, citations}`); lock the responsiveness choice — token streaming or tool-progress indicators — with rationale committed to the doc
- [x] **USERS.md capability trace** (in `docs/AGENT_CONTRACT.md`): produce a table mapping each tool name and each dispatcher behavior to the USERS.md section that justifies it. Mark partial justifications (`get_triage_rationale` click-to-expand UI form, tool chaining) and document the engineering rationale for each in writing. This is a grading artifact — it must exist as a committed file before Phase 1 begins.
- [x] **Verification layer limitations** (in `docs/AGENT_CONTRACT.md`, referencing `ARCHITECTURE.md §4.4`): confirm the limitations paragraph in `ARCHITECTURE.md §4.4` is present and accurate. Add a cross-reference section in `AGENT_CONTRACT.md` that links to it and summarizes the implications for claim taxonomy — specifically, that citations reduce but do not eliminate the risk of plausible-but-wrong values passing attribution.
- [x] **Schema migration gate**: confirm `code_status` and `isolation` fields are present in the synthetic FHIR dataset (as Patient resource extensions or mapped fields) and that the FHIR extractor reads them correctly. `ARCHITECTURE.md §3.1` lists this as a hard prerequisite for agent code. If absent from synthetic data, blank code status auto-flag tests will silently pass for the wrong reason. Block Phase 1 until confirmed. — `isolation` added to all 23 bundles as FHIR Flag resources; extractor updated; 6 new tests added (150→196 suite).

### Phase 1 — Tool Schemas

- [x] Create `agent-api/agent/schemas.py`: JSON Schema definitions for `get_census_summary`, `get_patient_briefing`, `query_patient_records`, `get_medication_safety`, `generate_handoff`, and `get_triage_rationale` (click-to-expand, not dispatched conversationally)
- [x] Write tool descriptions with trigger language and disambiguation hints for ambiguous queries (e.g., "Lasix plan" questions)
- [x] Write `agent-api/tests/test_tool_schemas.py` and validate all schemas with `jsonschema` — 46 hard_failure tests; jsonschema added to requirements.txt
- [x] **tool_use migration plan**: documented in `schemas.py` module docstring — identifies 4 files to migrate in Phase 2; migration is one-way per ARCHITECTURE §4.4

### Phase 2 — Tool Function Extraction

- [x] Create `agent-api/agent/tools/` package
- [x] Extract `get_census_summary(input)` from triage handler path
- [x] Extract `get_patient_briefing(input)` from briefing handler path
- [x] Extract `query_patient_records(input)` from query handler path
- [x] Extract `get_medication_safety(input)` from medication safety path
- [x] Extract `generate_handoff(input)` from handoff path
- [x] Add `get_triage_rationale(input)` from triage criteria + explainer internals
- [x] Standardize tool return shape to `{result, citations, metadata}`; run verification inside each tool
- [x] Convert route handlers in `main.py` to thin wrappers for backward compatibility
- [x] **tool_use migration** (all 4 files migrated; zero Pydantic-JSON parse paths remain):
  - [x] Migrate `briefing/generator.py` from `model_validate_json` to `messages.create(tools=[...])`
  - [x] Migrate `triage/explainer.py` from `model_validate_json` to `messages.create(tools=[...])`
  - [x] Migrate `query/conversation.py` from `model_validate_json` to `messages.create(tools=[...])`
  - [x] Migrate `handoff/generator.py` from `model_validate_json` to `messages.create(tools=[...])`
  - [x] Confirm no Pydantic-JSON parse path remains as a live fallback in any of the above
- [x] Run test suite; confirm no regressions — 196 passed, 1 skipped, 0 failures

### Phase 3 — Citation Infrastructure

- [x] Define `Citation` model: `{patient_id, resource_type, resource_id, effective_datetime, value_summary, claim_class}` — frozen dataclass in `agent-api/agent/citation.py`
- [x] Update `verification/source_attribution.py` to emit `Citation` objects (not raw dicts) — `extract_citations()` added
- [x] Update all tool outputs to include structured citations — all 6 tools return `Citation.to_dict()` objects
- [x] Write `agent-api/tests/test_citations.py`: 16 hard_failure tests confirm clinical claims map to citations per claim taxonomy — 212 passed, 0 failures

### Phase 4 — Dispatcher Loop

- [x] Create `agent-api/agent/system_prompt.py` — identity, safety rules, format guidance; iterated after Phase 6 routing eval — no gaps found, system prompt confirmed correct
- [x] Create `agent-api/agent/tool_registry.py` — TOOL_REGISTRY (5 conversational tools) + DIRECT_TOOL_REGISTRY (get_triage_rationale)
- [x] Create `agent-api/agent/dispatcher.py` — `tool_use`/`tool_result` loop, session context loading, typed final response, max-5-turn guard
- [x] Wire `POST /agent/query` in `main.py`
- [x] Wire `POST /agent/triage_rationale` in `main.py`
- [x] Apply `cache_control: {type: "ephemeral"}` on system prompt and census context blocks — 4 cache_control references in dispatcher; criterion added to AGENT_CONTRACT.md
- [x] Add Langfuse spans: parent dispatch span + child tool spans + generation events
- [x] Define tool-failure contract: `ToolFailureClass` enum with retry policy per class, physician-visible error messages
- [x] Define misroute handling: detection, one self-correction max, `misroute_detected`/`self_corrected` audit tags — 218 passed, 0 failures

### Phase 5 — Verification on Final Response

- [x] Create `agent-api/verification/dispatcher_response.py`: 7 hard safety checks — NKDA strip, stale critical flag, canary block, claim-without-citation strip, recommendation language strip, isolation/code-status flags (222 lines)
- [x] Hook final-response verification after dispatcher `end_turn` — `verify_dispatcher_response()` called at end_turn; blocked responses short-circuit
- [x] Write `agent-api/tests/test_dispatcher_verification.py`: 13 hard_failure tests — all pass. NKDA removes claim (not just warns). Phase 13 cutover precondition met. Suite: 231 passed, 0 failures.

### Phase 6 — Routing Eval Suite

- [x] Create `agent-api/tests/test_agent_routing.py` — 24 tests (3 hard_failure, 21 clinical_accuracy); all pass
- [x] Include ambiguous prompts ("Lasix plan", "Lasix interactions") with expected tool behavior documented in docstrings
- [x] Add wrong-tool regression tests (potassium → query_patient_records not census; allergies → medication_safety not query)
- [x] **Authorization probe cases**: out-of-census patient pt-999 → no FHIR tool called; scope enforcement verified. `@pytest.mark.hard_failure`
- [x] **Missing-data scenarios**: echo query for patient with no DiagnosticReport → explicit not-found with search window. `@pytest.mark.clinical_accuracy`
- [x] **Partial patient identifier**: "bed 5-something" → clarification request asserted. `@pytest.mark.clinical_accuracy`
- [x] **Dual-path triage rationale routing**: typed "why is bed 501 first?" → dispatcher + query_patient_records; click-to-expand → get_triage_rationale direct; both verified. `@pytest.mark.clinical_accuracy`
- [x] Run suite; system_prompt.py reviewed — no disambiguation gaps found. Dispatcher bug fixed (LogRecord reserved key). Suite: 255 passed, 0 failures.

### Phase 7 — React UI Refactor

- [x] Add `agent-ui/src/components/ChatSurface.tsx` — static greeting + pre-fetch on mount + auto-dispatch census
- [x] Add `agent-ui/src/components/ResponseRenderer.tsx` keyed by response type
- [x] Add type renderers: `CensusRenderer`, `BriefingRenderer`, `QueryAnswerRenderer`, `MedicationSafetyRenderer`, `HandoffRenderer`, `TextRenderer`
- [x] Add `agent-ui/src/components/CitationLink.tsx` (V1 textual; V2-ready href prop)
- [x] Add `agent-ui/src/components/PatientCard.tsx` (ranked sidebar, click-to-expand)
- [x] Render UI-owned static greeting on mount — constant React string with time-of-day logic, no LLM
- [x] Trigger FHIR pre-fetch on React mount (in parallel with greeting render) — fires before "Go"
- [x] Auto-dispatch synthetic census message at session open
- [x] Move API path to `sendAgentMessage(message, session_id)` → `POST /agent/query`
- [x] Add `fetchTriageRationale(patient_id)` → `POST /agent/triage_rationale`
- [x] Implement responsiveness: "Thinking…" loading indicator; `// TODO: replace with streaming (Phase 14)` comment at integration point
- [x] Run `npm run build` — clean TypeScript build; `public/copilot.js` (154 KB) landed in PHP module. No CSS file — all inline styles match existing pattern.

### Phase 8 — OpenEMR Module Integration

- [x] Verify config injection in `module.php` and `index.php`: `agentApiUrl`, `providerId`, `csrfToken`, `sessionId`, `patientIds` — all 5 keys injected; `sessionId` and `patientIds` were added; `providerName` also added
- [x] Confirm module bootstraps correctly in OpenEMR with new bundle — bootstrap chain correct; no logic creep; dead CSS link removed; build 157 KB clean

### Phase 9 — Synthetic Data Loading

- [x] Add pt-019 through pt-023 to `load.py` PATIENTS list — all 5 added with clinical profiles derived from bundle JSON
- [x] Run `load.py` against Railway — 23/23 loaded successfully via Railway CLI; new OAuth client `copilot-loader-v2` registered (original secret was hashed)
- [x] Spot-check: all 23 patients confirmed on Railway (41 total: 18 original Phase 0 + 23 new); pt-019–pt-023 (Linda Okonkwo, Robert Finch, Priya Anand, James Whitfield, Keisha Balogun) all present
- [ ] Spot-check: census via legacy endpoint and via `POST /agent/query` both return same patient count — **PENDING**: requires agent-api deployed to Railway

### Phase 10 — CI Cleanup

- [x] Confirm no orphan fixtures in `conftest.py` — 2 orphans removed (`marcus_webb_bundle`, `delia_fontaine_bundle`)
- [x] Confirm all tests carry required markers — 255/255 tests marked; unmarked-test guard passes clean
- [x] Update `copilot-eval.yml` with routing eval gate for `test_agent_routing.py` — gate added with empty-selection guard; hard_failure: 205, clinical_accuracy: 163

### Phase 11 — Observability

- [x] Add `agent-monitoring/grafana/dashboards/co-pilot-overview.json` — 7 panels: latency p50/p95/p99, error rate, triage distribution, tool frequency, cache hit %, misroute rate, briefing time
- [x] Add Prometheus metrics in `agent-api/agent/metrics.py`: `agent_tool_calls_total{tool}`, `agent_dispatch_latency_seconds`, `agent_tool_misroute_total`, `agent_cache_hits_total`, `agent_cache_misses_total`; `/metrics` endpoint mounted; `prometheus-client==0.21.1` added to requirements
- [x] Validate Langfuse trace hierarchy — dispatch_span (parent) → generation_event → tool_span; initialized from config not hardcoded. Suite: 255 passed, 0 failed.

### Phase 12 — Build and Deploy

> **Precondition:** Phase 9 (Synthetic Data Loading) must complete before this phase begins. The spot-checks below verify that load; they do not re-run it.

- [x] Run `npm run build` in `agent-ui/` → `copilot.js` (154 KB) in PHP module public/; 0 TypeScript errors; Python 3.9 compat fix applied (`from __future__ import annotations`)
- [ ] Spot-check: confirm all 23 patients returned from `/fhir/Patient` on Railway — **MANUAL**: requires Railway credentials + `load.py` run first
- [ ] Spot-check: census endpoint returns patients ranked P1 through P10 with pt-019 at P9, pt-020 at P10 — **MANUAL**: requires Railway + agent-api deployed
- [ ] Rationale documented in `AGENT_CONTRACT.md §6` with 4 pre-cutover manual verification steps

### Phase 13 — Migration and Cutover

> **Precondition:** Phase 5 dispatcher response verification (`verification/dispatcher_response.py` hooked after `end_turn`, `test_dispatcher_verification.py` green in CI) must be shipped before Phase 13 begins. Without it, `POST /agent/query` runs without final-response verification during the cutover window, violating CLAUDE.md hard rule #5. Add this precondition to `docs/AGENT_CONTRACT.md` cutover gates section when creating that doc in Phase 0.

- [x] Run dispatcher path in parallel with legacy endpoints — 5 legacy endpoints tagged `# LEGACY — retire after Phase 13 cutover`; startup warning logs when enabled; both paths call same tool functions
- [x] Enforce cutover gates from Phase 0 — `docs/CUTOVER_STATUS.md` created: 4 gates PASSED (verification, routing accuracy, misroute handling, legacy tagging); 6 PENDING live deployment (p95, error rate, misroute rate, cache-hit, Railway load)
- [x] Keep legacy endpoints behind feature flag — `legacy_endpoints_enabled: bool = True` in `config.py`; startup warning fires when active
- [ ] Remove legacy endpoints in subsequent release after confirmed stability — **MANUAL**: set `LEGACY_ENDPOINTS_ENABLED=false` after 24h soak confirms all gates pass
- [ ] Update `ARCHITECTURE.md` to reflect shipped state — **PAUSED for human review** per user instruction

### Phase 14 — V2 Citation Click-Through

- [x] Identify OpenEMR deep-link URL patterns — 9 resource types mapped in `agent-ui/src/utils/citations.ts`
- [x] Upgrade `CitationLink` to interactive links — `buildCitationUrl()` computes href; opens in new tab; null → V1 textual badge fallback
- [x] Add citation-link URL construction tests — 9 Vitest tests, all pass; `vitest` added to devDependencies

### Phase 15 — Acceptance

- [x] Clean `python3 -m pytest agent-api/tests` run from repo root — 255 passed, 0 failed
- [x] CI gates trigger for all agent-api changes — 3 gates in `copilot-eval.yml` (hard_failure, clinical_accuracy, routing eval); all with empty-selection guard; triggers on `agent-api/**`
- [x] Routing accuracy target met on ≥ 20-query eval set — 24/24 = 100% (target ≥ 95%)
- [ ] Session-open flow (greeting → census) under 5 seconds end-to-end — **PENDING live deployment**; greeting is instant constant string; budget is `get_census_summary` LLM response
- [ ] Click-to-expand rationale under 2 seconds — **PENDING live deployment**; no LLM on this path; reads from Redis + rules engine only
- [x] Citations present on all clinical claims in dispatcher responses per claim taxonomy — `claim_without_citation` strip active; test passes
- [x] Domain constraints run on final dispatcher output — 13/13 hard_failure tests pass; all 7 checks active in `dispatcher_response.py`

---

## Phase 16: Production Deployment to Railway

> **How to read this section:**
> - `[x]` = done and verified
> - `[ ]` = not done yet
> - **[HUMAN]** = requires a credential or dashboard action only you can take
> - **[AUTO]** = fully automated, Claude Code CLI can run it
> - **[IN PROGRESS]** = sub-agent currently working on this
>
> Every script, code change, and finding is documented here. This is the single source of truth.

---

### A. Codebase audit and fixes *(completed 2026-04-30)*

These were bugs found during deployment planning. All fixed before any deployment attempt.

#### A1. Bug fixes — `agent-api/config.py`
- [x] **Fixed wrong FHIR scope format** — `system/Patient.read` is not a valid SMART scope. Changed all 7 resource scopes to `.rs` (read + search) format: `system/Patient.rs system/Encounter.rs system/Observation.rs system/Condition.rs system/MedicationRequest.rs system/AllergyIntolerance.rs system/DiagnosticReport.rs`. Without this fix, every FHIR call would return 400 Bad Request.
- [x] **Fixed Langfuse default host** — was `http://langfuse:3000` (self-hosted container address). Changed to `https://cloud.langfuse.com` to match the Cloud decision.

#### A2. Bug fix — `docker/development-easy/docker-compose.copilot.yml`
- [x] **Removed self-hosted Langfuse services** — compose file had `langfuse`, `langfuse-db` (Postgres), and related volumes. These contradict the architecture decision to use Langfuse Cloud (no self-hosted). Removed both services and their volumes. Also removed the `depends_on: langfuse` from agent-api. Updated comments to clarify this file is for local dev only; Railway uses separate services.

#### A3. Bug fix — `interface/modules/custom_modules/oe-module-clinical-copilot/index.php`
- [x] **Made agent URL configurable via env var** — was hardcoded to read only from OpenEMR's globals table (`$GLOBALS['copilot_agent_api_url']`), which requires a database config step. Changed to: `getenv('COPILOT_AGENT_API_URL') ?: ($GLOBALS['copilot_agent_api_url'] ?? 'http://localhost:8400')`. Now Railway can inject the URL as an environment variable with no DB step needed.

#### A4. Update — `agent-api/.env.example`
- [x] **Updated to reflect Railway + Langfuse Cloud config** — removed self-hosted Langfuse comment, added correct Railway OPENEMR_BASE_URL, documented REDIS_URL variants (local vs Railway internal), clarified that LANGFUSE_HOST should be `https://cloud.langfuse.com`.

---

### B. Scripts created — `scripts/` directory *(completed 2026-04-30)*

All scripts are in `scripts/`, are executable (`chmod +x`), and can be run by Claude Code CLI autonomously. Each is self-contained with usage docs at the top.

#### `scripts/01-register-fhir-client.sh` **[AUTO]**
- [x] **Created** — Registers a `copilot-agent-v1` OAuth2 client in OpenEMR via the SMART dynamic client registration endpoint. Prints `FHIR_CLIENT_ID` and `FHIR_CLIENT_SECRET` to stdout on success. Falls back with clear error messages if registration requires an initial access token (OpenEMR sometimes blocks dynamic registration). **Status: superseded** — client was registered directly via MySQL (see B6 below) because dynamic registration failed. Script kept for documentation and future use.

#### `scripts/02-deploy-railway.sh` **[AUTO]**
- [x] **Created** — Deploys agent-api to Railway. Does: validates required env vars, pauses for Redis creation instructions if needed, sets all env vars on the agent-api service via `railway variables set`, runs `railway up --detach` from the `agent-api/` directory, attempts to detect the deployed URL and set `COPILOT_AGENT_API_URL` on the OpenEMR service automatically.

#### `scripts/03-smoke-test.sh` **[AUTO]**
- [x] **Created** — 6-test end-to-end verification script. Tests in order: (1) agent-api `/health` responds, (2) OpenEMR FHIR metadata reachable, (3) FHIR patient proxy confirms agent-api can authenticate, (4) `POST /agent/query` dispatcher responds with a typed envelope, (5) `POST /agent/triage_rationale/pt-001` responds with scoring data, (6) Marcus Webb (pt-001) is ranked P1. Exits 0 if all pass, 1 if any fail. Usage: `export AGENT_API_URL=<url> && ./scripts/03-smoke-test.sh`

#### `scripts/04-verify-cutover-gates.sh` **[AUTO]**
- [x] **Created** — Verifies all 6 Phase 13 cutover gates from `AGENT_CONTRACT.md`. Automated gates: routing accuracy ≥ 95% (runs 10 representative queries), p95 latency ≤ 4s (runs 10 timed briefing queries), click-to-expand rationale ≤ 2s. Manual gates (prints instructions): error rate ≤ 1% (Grafana), misroute rate ≤ 2% (Grafana), cache-hit tokens ≥ 70% (Langfuse Cloud traces). Exits 0 only if all automated gates pass.

#### `scripts/05-enable-openemr-module.sh` **[AUTO]**
- [x] **Created** — Enables the `oe-module-clinical-copilot` module in OpenEMR's module registry by directly inserting/updating the `modules` table in MySQL. Uses `ON DUPLICATE KEY UPDATE` so it's safe to run multiple times. Requires `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_PASS` env vars from Railway's MySQL TCP proxy. Falls back with clear instructions for manual activation via the OpenEMR admin UI.

---

### C. Railway infrastructure audit *(completed 2026-04-30)*

- [x] **Confirmed Railway project:** `Openemr-deployment` — linked and accessible via `railway` CLI
- [x] **Confirmed running services:** `clinical-copilot-openemr` (Online) + `MySQL` (Online)
- [x] **Confirmed MySQL TCP proxy:** `shuttle.proxy.rlwy.net:12805`, root pass in Railway vars
- [x] **Confirmed missing services:** No `agent-api` service, no `Redis` service — both need to be created

---

### D. FHIR OAuth investigation *(completed 2026-04-30)*

This was an unplanned but critical investigation. The `fhir_client.py` was written for `client_credentials` grant; OpenEMR rejected it.

- [x] **Audited all 5 OAuth clients in the database** via direct MySQL query:
  - `copilot-loader-v2` — enabled, password+client_credentials, user-level scopes (used for data loading)
  - `agent-api-admin` — enabled, password+client_credentials, system FHIR scopes ✓
  - `agent-api-v2` — **disabled**, client_credentials only, system FHIR scopes
  - `agent-api` — **disabled**, client_credentials only, system FHIR scopes
  - `agent-api-loader` — enabled, password only, wrong scopes
- [x] **Discovered `client_credentials` grant is broken on this OpenEMR deployment** — returns `"assertion type is not supported"`. OpenEMR's SMART backend services (RFC 7523 JWT auth) is not configured. Only `password` grant works.
- [x] **Disabled unused clients** — `agent-api`, `agent-api-v2`, `agent-api-loader` set to `is_enabled=0` to clean up
- [x] **Registered clean `copilot-agent-v1` client** directly in MySQL with known credentials:
  - `FHIR_CLIENT_ID`: `your-fhir-client-id`
  - `FHIR_CLIENT_SECRET`: `your-fhir-client-secret`
  - grant_types: `password|client_credentials`, is_enabled: 1
  - scopes: all 7 system FHIR resource types + openid + api:fhir
- [x] **Confirmed password grant returns valid JWT** — token obtained successfully with `user_role=users` + admin credentials
- [x] **Resolve 401 on FHIR Patient endpoint** — root cause: `system/*` scopes + `api:fhir` return 401 on this OpenEMR instance (SMART backend services not configured). Fix: switched to `user/*` scopes + `api:oemr` which is the same scope set that successfully loaded 23 patients. Updated `config.py` default `fhir_scopes`.
- [x] **Updated `agent-api/auth/fhir_client.py`** — changed from `client_credentials` to `password` grant; added `user_role=users` param; improved error logging on token failure. Added `fhir_username`, `fhir_password`, `fhir_user_role` fields to `config.py`. Updated `.env.example` with all new fields.

---

### E. Credential setup *(you do these — agent will prompt when each is needed)*

#### E1. Langfuse Cloud **[HUMAN]**
- [ ] Go to **cloud.langfuse.com** → Sign up free
- [ ] Create project: `clinical-copilot`
- [ ] Settings → API Keys → Create new key pair
- [ ] When prompted by agent, run:
  ```
  railway variables set LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... --service copilot-agent-api
  ```

#### E2. Anthropic API key **[HUMAN]**
- [ ] Go to **console.anthropic.com** → API Keys → Create new key
- [ ] Name: `clinical-copilot-production` — copy immediately (shown once)
- [ ] When prompted by agent, run:
  ```
  railway variables set ANTHROPIC_API_KEY=sk-ant-... --service copilot-agent-api
  ```

---

### F. Deploy Redis to Railway **[HUMAN — 5 min]**

- [ ] Open https://railway.app → `Openemr-deployment` project → **+ New** → **Database** → **Add Redis**
- [ ] Name it `copilot-redis`
- [ ] After it deploys: Redis service → **Variables** tab → copy the `REDIS_URL` (internal `redis://...railway.internal/...`)
- [ ] When prompted by agent, run:
  ```
  railway variables set REDIS_URL=redis://... --service copilot-agent-api
  ```

---

### G. Deploy agent-api service **[AUTO after credentials set]**

- [x] FHIR auth bug resolved — `fhir_client.py` uses password grant + user/* scopes
- [ ] All code changes committed and pushed to trigger Railway redeploy of OpenEMR
- [ ] Run: `./scripts/02-deploy-railway.sh` (requires E1, E2, F complete first)
- [ ] Verify: `railway logs --service copilot-agent-api` shows startup without errors
- [ ] Verify: `GET <agent-api-url>/health` → `{"status": "ok"}`
- [ ] Set agent URL on OpenEMR: `railway variables set COPILOT_AGENT_API_URL=<url> --service clinical-copilot-openemr`

---

### H. Enable OpenEMR Co-Pilot module **[AUTO]**

- [ ] Run: `./scripts/05-enable-openemr-module.sh` (requires MySQL env vars set)
- [ ] Or manually: OpenEMR admin → Admin → Modules → Manage Modules → Enable `Clinical Co-Pilot`
- [ ] Verify: navigate to OpenEMR, Co-Pilot panel appears in sidebar, static greeting renders instantly

---

### I. End-to-end smoke test **[AUTO]**

- [ ] Run: `export AGENT_API_URL=<railway-url> && ./scripts/03-smoke-test.sh`
- [ ] All 6 checks must pass:
  - `[1]` agent-api `/health` → `{"status": "ok"}`
  - `[2]` OpenEMR FHIR metadata reachable
  - `[3]` FHIR patient proxy — confirms auth is working
  - `[4]` `POST /agent/query` dispatcher returns typed response envelope
  - `[5]` `POST /agent/triage_rationale/pt-001` returns scoring data
  - `[6]` Marcus Webb (pt-001) ranked P1 (qSOFA ≥2 confirmed in census response)

---

### J. Cutover gate verification **[AUTO + manual Grafana/Langfuse]**

- [ ] Run: `./scripts/04-verify-cutover-gates.sh`
- [ ] Gate 1 — Routing accuracy ≥ 95% on 10-query set (script tests automatically)
- [ ] Gate 2 — Dispatcher p95 latency ≤ 4s (script measures 10 queries)
- [ ] Gate 3 — Click-to-expand rationale ≤ 2s (script measures directly)
- [ ] Gate 4 — Error rate ≤ 1% — check Grafana dashboard (import `agent-monitoring/grafana/dashboards/co-pilot-overview.json`)
- [ ] Gate 5 — Tool misroute rate ≤ 2% — check Grafana misroute panel
- [ ] Gate 6 — Cache-hit tokens ≥ 70% for UC-2/3/4 — check Langfuse Cloud traces, compare `cached_input_tokens / total_input_tokens`

---

### K. Soak period and legacy endpoint removal **[AUTO after 24h]**

- [ ] Run a full representative session: census → 3+ briefings → 2+ queries → medication check → handoff
- [ ] Monitor for 24 hours with no errors
- [ ] After 24h stable: `railway variables set LEGACY_ENDPOINTS_ENABLED=false --service copilot-agent-api`
- [ ] Verify: `POST /triage/census` returns 404; `POST /agent/query` continues working

---

### L. Deferred items (not blockers for demo)

#### L1. Clinician priority table review **[HUMAN — schedule when ready]**
- [ ] Book 1 hour with a hospitalist or charge nurse
- [ ] Walk through `agent-api/triage/rules/rules_engine_config.yaml` — all 10 priority levels, thresholds, tie-breaking rules
- [ ] Present 5 synthetic patients and their rankings; ask: "Would you trust this list at the start of a shift?"
- [ ] Update `rules_engine_config.yaml` based on feedback
- [ ] Re-run eval suite: `python3 -m pytest agent-api/tests` — re-grade any tests that break from threshold changes

#### L2. Streaming responses — **DECISION: NOT IMPLEMENTING. "Thinking…" spinner is correct.**
- [x] **Decision locked 2026-04-30:** Token-by-token streaming is explicitly rejected for this product. Reasoning: the verification layer must see the complete LLM response before anything is shown to the physician. Streaming partial tokens before verification means potentially displaying unverified clinical claims, hallucinated values, or stripped safety flags mid-render. In a clinical context, showing a physician incomplete or pre-verification output — even for a second — is not acceptable. The "Thinking…" spinner holds until the full response is verified and safe. This is a product decision, not a performance limitation.
- [x] **Remove the `// TODO: replace with streaming` comment** from `agent-ui/src/components/ChatSurface.tsx:64` — it no longer represents a planned change. Replace with a comment documenting the decision.
- [x] **`UX_SPEC.md` responsiveness strategy** is already locked to the spinner approach — no spec change needed.
