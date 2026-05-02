# Clinical Co-Pilot — TODO

> Completed phase history: [docs/archive/copilot-todo-history-2026-04.md](docs/archive/copilot-todo-history-2026-04.md)
> Last archived: 2026-04-30

---

## Now — Must Do This Week

- [ ] Fix `scripts/04-verify-cutover-gates.sh` Gate 2 (p95 latency): add a census warm-up call before the 10 timed briefing queries. The 4s target applies to cached in-session calls, not cold first-call latency (~34s). Gate test must warm Redis first, then measure.
- [ ] Schedule clinician priority review: 1-hour session with a hospitalist or charge nurse. Walk through P1–P10, all thresholds, 5 representative patients in `agent-api/triage/rules/rules_engine_config.yaml`. Ask: "Would you trust this list at the start of a shift?" — update config and re-run eval suite (`python3 -m pytest agent-api/tests`) based on feedback.
- [ ] Finish in-flight `agent-api/` work and commit: Redis briefing cache (`agent/tools/__init__.py`), required-sections retry + `max_tokens` 2048→4096 (`briefing/generator.py`), `abnormal_labs` from FHIR Observation interpretations (`triage/census.py`, `triage/explainer.py`).

---

## Copilot UI/UX & Performance Audit (2026-05-02) — IMPLEMENTED, awaiting manual verification

Approved plan: `/Users/hirom/.claude/plans/humming-toasting-narwhal.md`. All six issues implemented in parallel sub-agents, statically verified (276 pytest pass, lint-imports 8/8, `npx tsc --noEmit` clean, `php -l` clean). Built and deployed locally on 2026-05-02: `agent-ui` rebuilt → `copilot.js` 170KB; Redis flushed; `agent-api` container restarted (healthy). **Not committed yet** — manual end-to-end verification still required.

### Issue 4 — Greeting copy ✅
- [x] `agent-ui/src/components/ChatSurface.tsx` greeting → `Good day, ${displayName} — ready for your census`; `timeGreeting()` removed.
- [x] `interface/modules/custom_modules/oe-module-clinical-copilot/index.php`: looks up `users.title/fname/lname` for `authUserID` so `{User}` renders "Dr. Sarah Chen", not `schen`.

### Issue 6 — Playwright verification harness ✅
- [x] `@playwright/test` added to `tools/ui-bug-hunter/package.json` + `playwright.config.ts` (admin + sara projects).
- [x] `global-setup.ts` logs in via `/interface/login/login.php?site=default`, persists `.auth/{admin,sara}.json`. SARA_PASSWORD env override supported.
- [x] Specs created: `greeting.spec.ts`, `census-stability.spec.ts` (gated by fixme until Issue 2 verified), `theme.spec.ts` (gated by fixme until Issue 1 verified), `chart-button.spec.ts`, `latency.spec.ts`. Shared helpers in `specs/_helpers.ts`.
- [x] `specs/README.md` documents prerequisites and run command.
- [ ] **Run before merge:** `cd tools/ui-bug-hunter && npm install && npx playwright install chromium && npm test`.
- [ ] **Deferred** to follow-up: add stable `data-testid`s to copilot React bundle (skipped to avoid conflict with theme refactor).

### Issue 3 — Chart button opens empty page (UUID vs integer pid) ✅
- [x] `agent-api/triage/census.py:_patient_pid()` returns `""` instead of FHIR UUID when no integer pid identifier is present (defensive — UUID→pid REST surface not currently plumbed in agent-api).
- [x] `interface/patient_file/summary/demographics_full.php`: defensive UUID detection + `PatientService::getPidByUuid(UuidRegistry::uuidToBytes(...))` resolution; falls through to existing not-found handling on failure.
- [x] `agent-ui/src/components/PatientCard.tsx`: now consumes `openemr_pid` with the same fallback pattern as `CensusRenderer`.
- [x] `agent-ui/src/components/CensusRenderer.tsx`: `openPatientChart` digits-only guard (`/^\d+$/`); console.warn + return on non-numeric.
- [x] `openemr_pid` added to `PatientSummary` type in `agent-ui/src/types.ts`.

### Issues 2 + 5 — Census determinism + cache plumbing ✅
- [x] `agent-api/config.py`: split TTLs — `bundle_cache_ttl_seconds=7200`, `briefing_cache_ttl_seconds=1800`, `census_cache_ttl_seconds=900`, `explanation_cache_ttl_seconds=86400`.
- [x] `_CACHE_TTL = 300` removed from `triage/census.py`; imports updated in `agent/tools/__init__.py` and `triage/explainer.py`; `tests/test_briefing_cache.py` updated.
- [x] Census cache key reshaped to `copilot:census:{provider_id|"unknown"}:{sha8(sorted(patient_ids))}` via shared `census_cache_key()` helper. Both `main.py` (prefetch) and `agent/tools/__init__.py` (dispatcher) use it.
- [x] Prefetch `_warm()` now passes `provider_id=request.provider_id` (no more provider=None vs provider="prov-chen" race).
- [x] `triage/census.py` sorts `patient_ids` before fan-out.
- [x] `auth/fhir_client.py`: dropped `status=finished` from Encounter participant query (synthetic encounters are `in-progress`); added `_sort=_id` to both that query and the `Patient?_count=200` fallback; results sorted on return.
- [x] `_build_entry` drops now WARN-log + increment `agent_census_dropped_patients_total` counter (no more silent drops).
- [x] `synthetic_data/generate.py`: `BASE_DATE` honours `SYNTHETIC_DATA_BASE_DATE` env var (ISO 8601) for deterministic fixtures.
- [x] `main.py:_warm()` fans out bundle + briefing warmers under `asyncio.Semaphore(4)`; each EXISTS-checks the key first. Shared helpers `_get_cached_bundle/_set_cached_bundle/_get_cached_briefing/_set_cached_briefing` and exported `warm_bundle_for_patient/warm_briefing_for_patient` in `agent/tools/__init__.py` (no Anthropic/FHIR call duplication).
- [x] `agent/metrics.py`: existing counters renamed to `agent_prompt_cache_{hits,misses}_total`; added labelled `agent_data_cache_{hits,misses}_total{cache="bundle|briefing|census|explanation"}` and `agent_census_dropped_patients_total`. All Redis read sites instrumented.
- [x] New tests: `agent-api/tests/test_census_cache_key.py` (6 cases — shape, order-independence, provider distinctness, empty-list sentinel, None-vs-real provider, patient-set distinctness). 276 total passing.

### Issue 1 — UI theme unification ✅
- [x] `agent-ui/src/styles/tokens.ts`: single source of color tokens; added `summaryCardStyle`, `metricCardStyle`, `patientRowStyle`, `livePillStyle`, `admitBadgeStyle`, and `tierColor(level)` mapping P1–P3→RED, P4–P7→AMB, P8+→NEU.
- [x] `agent-ui/src/components/primitives/index.tsx`: added `TierDot`, `MetricCard`, `MetricStrip`, `PatientRow`, `LivePill`, `AdmitBadge`, `DisclaimerFooter`.
- [x] All renderers refactored — local color redeclarations removed in `CensusRenderer.tsx` and `BriefingRenderer.tsx`; `HandoffRenderer.tsx` uses `PatientRow` (no more `ClaimRow` misuse); `QueryAnswerRenderer/MedicationSafetyRenderer/TextRenderer/CitationsPanel` wrapped in `cardStyle(NEU)` and use `DisclaimerFooter`.
- [x] `PatientCard.tsx` rewritten: `PRIORITY_COLORS` replaced with `tierColor(patient.priority)`; `#2c3e9e` button replaced with `primaryButtonStyle(color)`.
- [x] Hex-literal guard script: `tools/check-renderer-hex-literals.sh` (advisory, allowlists `#111`/`#fff`/`#16a34a` sentinels). Not yet wired to CI.

### Manual verification still required
- [ ] **Greeting:** open Co-Pilot as Sara → see "Good day, Dr. Sarah Chen — ready for your census".
- [ ] **Census stability:** reload Co-Pilot 5–10× → patient count identical each time.
- [ ] **Chart button:** click Chart on 3 different patients → demographics page populates (not empty).
- [ ] **Theme:** trigger briefing/query/handoff/medication outputs → all visually match census schema.
- [ ] **Latency:** observe whether second-and-later loads are noticeably faster (cache warming working).
- [ ] **Playwright suite:** `cd tools/ui-bug-hunter && npm install && npx playwright install chromium && npm test`.
- [ ] **Commit per-issue** with `Assisted-by: Claude Code` trailer once verification passes.

---

## Observability / Tracing pass (2026-05-02) — IMPLEMENTED, awaiting verification

**Why.** Latency claims ("warm cache hits", "pre-warmed in Xms") were unverifiable from logs alone. Cache hit/miss only existed as Prometheus counters; tool durations were only in response metadata; pre-warm was fire-and-forget with no completion log; the FHIR token cache had zero observability; the frontend had zero timing telemetry. Goal: make the request lifecycle greppable end-to-end and every latency claim falsifiable from a log line or a metric.

### What landed

- [x] **Structured JSON logging + `request_id` propagation.** New `agent-api/observability/` package (`json_logging.py`) with `JsonLogFormatter`, `RequestIdFilter`, and a `request_id` ContextVar. `RequestIdMiddleware` in `main.py` honors/generates `X-Request-ID` per request. Every existing `extra={}` log call now serializes structured. Leaf package — enforced by `agent-api/.importlinter` (`observability-is-leaf`).
- [x] **Pre-warm telemetry.** `_warm` background task in `main.py` logs start/complete/failure with `duration_ms` and per-cache outcomes. New metrics: `agent_prewarm_duration_seconds{outcome}` (Histogram), `agent_prewarm_runs_total{outcome}` (Counter).
- [x] **Tool / cache decision log.** Every tool in `agent-api/agent/tools/__init__.py` emits a single `tool_outcome` INFO via shared helper `observability/tool_logging.py:log_tool_outcome` with `tool_name`, `duration_ms`, `cache=hit|miss|n/a`, `session_id`, `patient_id`. Dispatcher emits `tool_call_start` / `tool_call_end` for lifecycle.
- [x] **Client-side timing.** `POST /agent/client-timing` (FastAPI, Pydantic-validated, 204). Frontend (`agent-ui/`) posts `agent_client_timing_seconds{action}` for chat-submit timings.
- [x] **FHIR token + checkpointer metrics.** `agent_fhir_token_cache_hits_total` / `_misses_total` in `auth/fhir_client.py` (auth must remain a leaf, so counters live there). Checkpointer load/save instrumented in `dispatcher.py` with `agent_checkpointer_ops_total{op,backend,outcome}` and `agent_checkpointer_op_duration_seconds{op,backend}`.

See `ARCHITECTURE.md` §5.5 for the full metric / structured-log catalog and the `X-Request-ID` propagation contract.

### Verification still required

- [ ] Open Co-Pilot, run a census + briefing + query, then `docker compose logs agent-api | jq -c 'select(.request_id)'` — confirm a single `request_id` threads end-to-end across `RequestIdMiddleware`, dispatcher, every `tool_outcome`, and `tool_call_end`.
- [ ] `curl -s localhost:9091/metrics | grep -E '^agent_(prewarm|data_cache|fhir_token_cache|checkpointer|client_timing)_'` — confirm every new metric surfaces with non-zero samples after one warm-up + one chat submit.
- [ ] After a session-open prefetch, confirm exactly one `prewarm_complete` log line with `duration_ms` and outcome counts per cache.
- [ ] Frontend → backend `X-Request-ID` round-trip: send a request from the UI, confirm the value in the browser's request header matches the one in the agent-api log line.

### Known gaps / next

- [ ] Grafana dashboard JSON in `agent-monitoring/grafana/dashboards/` does not yet plot any of the new metrics — add panels for prewarm duration, per-tool cache hit ratio, FHIR token cache hit ratio, checkpointer op duration, and client-timing.
- [ ] No alert rules wired for prewarm-failure spikes or checkpointer-save failure rate yet.
- [ ] `request_id` is not yet propagated into Langfuse trace metadata (currently only in Python logs and Prometheus exemplars are not configured).
- [ ] Verify no PHI leaks into the new structured log fields — `tool_outcome` carries `patient_id` (hashed elsewhere; raw here). Decide whether to hash at the `log_tool_outcome` boundary before exporting structured logs off-host.

---

## Tooling — UI bug hunter

- [ ] **UI bug hunter agent** scaffolded at `tools/ui-bug-hunter/` — Claude Agent SDK + Playwright MCP. Runs against local Docker OpenEMR (`http://localhost:8300`, `admin`/`pass`), explores the UI, writes findings to `tools/ui-bug-hunter/reports/ui-findings-<ts>.md`. To run: `cd tools/ui-bug-hunter && npm install && npm run install-browsers && ANTHROPIC_API_KEY=... npm run hunt`. Env vars: `TARGET_URL`, `USERNAME`, `PASSWORD`, `MAX_TURNS`, `HEADED=1`, `CLAUDE_MODEL`. Deps not yet installed; not yet exercised against a live stack.

---

## Next up — Architectural follow-ups (from clinical-copilot audit)

Prioritized; (1) and (2) are highest value.

- [ ] **(1) `import-linter` contracts for `agent-api/`.** Define layers (`agent.tools`, `briefing`, `triage`, `query`, `verification`, `medication`, `handoff`, `auth`) and contracts: `verification` is a leaf, `triage` does not depend on `query`, `agent.tools` is the only entry point. Wire into CI alongside the existing eval gates.
- [ ] **(2) Test coverage for `briefing/generator.py`.** The new required-sections directive + retry-on-empty path is uncovered. Add unit tests for: schema-valid first-pass, empty-sections retry success, retry-still-empty failure mode, schema-validation error logging.
- [ ] **(3) Test coverage for the new Redis cache path** in `agent-api/agent/tools/__init__.py`: cache hit, cache miss, Redis-error-non-fatal fallback.
- [ ] **(4) OpenAPI contract** between `agent-api/` and consumers (`agent-ui/` + PHP iframe). Hand-maintained interface doc is the only artifact that draws the cross-runtime edge — generate from FastAPI, commit, and reference from both consumers.
- [ ] **(5) `pydeps` one-shot PNG of `agent-api/`** for onboarding (low priority, optional).

---

## Blocked / Manual — Human-Only External Actions

### Deployment verification (Railway live session)

- [ ] **[HUMAN]** Run a full representative session: census → 3+ briefings → 2+ queries → medication check → handoff. Monitor for 24 hours with no errors.
- [ ] **[HUMAN]** After 24h stable soak, disable legacy endpoints: `railway variables set LEGACY_ENDPOINTS_ENABLED=false --service copilot-agent-api`. Verify `POST /triage/census` returns 404 and `POST /agent/query` continues working.
- [ ] **[HUMAN]** Confirm pt-019 at P9, pt-020 at P10 in full 23-patient live census (requires live session).
- [ ] **[HUMAN]** Check Grafana: error rate ≤ 1% (Gate 4) and misroute rate ≤ 2% (Gate 5) — import `agent-monitoring/grafana/dashboards/co-pilot-overview.json`.
- [ ] **[HUMAN]** Check Langfuse Cloud: cache-hit tokens ≥ 70% for UC-2/3/4 (Gate 6) — compare `cached_input_tokens / total_input_tokens` in traces.
- [ ] **[HUMAN]** Update `ARCHITECTURE.md` to reflect shipped state (paused per prior instruction — resume when ready).

### OpenEMR verification (Railway)

- [ ] **[HUMAN]** Log in to OpenEMR → confirm "Co-Pilot" appears in the navigation bar.
- [ ] **[HUMAN]** Click "Co-Pilot" tab → confirm chat panel opens, greets by provider name, census auto-dispatches within ~5 seconds.
- [ ] **[HUMAN]** Confirm session-open flow (greeting → census) under 5 seconds end-to-end.
- [ ] **[HUMAN]** Confirm click-to-expand rationale under 2 seconds.
- [ ] **[HUMAN]** Confirm all other nav bar items (Calendar, Flow, Recalls, Messages, Patient) work normally.
- [ ] **[HUMAN]** Confirm Co-Pilot tab persists when switching to other tabs and back.

### OpenEMR verification (local Docker)

- [ ] **[HUMAN]** Log in as `sara` at `http://localhost:8300` → confirm Co-Pilot nav tab appears and census auto-dispatches within ~5 seconds.
- [ ] **[HUMAN]** Enable module if needed via Admin UI (`http://localhost:8300/interface/modules/zend_modules/public/Installer/`) or `scripts/05-enable-openemr-module.sh` against local MySQL.
- [ ] **[HUMAN]** Confirm sidebar does NOT appear on the OpenEMR login page (unauthenticated — bootstrap skips injection).
- [ ] **[HUMAN]** Confirm Co-Pilot tab persists across OpenEMR page navigation within the same session.

---

## Next — Queued

### Clinician follow-up (after priority table review)

- [ ] Re-grade eval tests whose expected priority levels change based on clinician feedback. Document which tests were re-graded and why in `agent-api/tests/` docstrings.
- [ ] If any threshold changes propagate to Phase 8 routing eval cases, update `agent-api/tests/test_agent_routing.py` accordingly.

### Secret rotation checklist (verify after any credentials are shared or copied)

- [ ] Verify the Anthropic API key in `docker/development-easy/.env.copilot` is the intended dev key; rotate at <https://console.anthropic.com> if any doubt.
- [ ] Rotate the local FHIR OAuth client secret if the local dev instance was shared or the secret was logged.
- [ ] Rotate the Langfuse secret key at <https://cloud.langfuse.com> if the project was shared with others.

---

## Recently completed

- [x] **2026-04-30 (`9c6aadc9d`)** Briefing returns populated sections, not just alerts.
- [x] **2026-04-30 (`7513ce8d1`)** Surface briefing schema-validation errors in logs.
- [x] **2026-04-30 (`0c899882d`)** Cover P1–P10 on Sara Chen panel and unstick triage signals.
- [x] **2026-04-30 (`95375e7b8`)** Make FHIR errors visible and recover from stale tokens.
- [x] **2026-04-30 (`dfabbc679`)** Cast `providerId` to string in iframe config.

---

## Monthly Hygiene

- [ ] Archive any completed items from this file to `docs/archive/` (next sweep: 2026-05-30).
- [ ] Run `detect-secrets scan --baseline .secrets.baseline` and review any new findings.
