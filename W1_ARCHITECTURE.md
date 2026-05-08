# Clinical Co-Pilot — Architecture

> **Scope:** This document describes the Week 1 dispatcher architecture (raw Anthropic SDK + custom Checkpointer). Week 2 (document ingestion, multi-agent graph, hybrid RAG, eval gate) is documented separately in `W2_ARCHITECTURE.md`. Where the two docs describe the same system component, `W2_ARCHITECTURE.md` is the current source of truth.

---

## Quick Read

The Clinical Co-Pilot is an AI assistant whose source code ships inside the OpenEMR repository but whose runtime is isolated from OpenEMR's process space. That distinction — co-located at build time, isolated at execution time — matters more than almost anything else in this document. If the agent crashes, OpenEMR keeps working. If the hospital network is slow, the agent falls back gracefully. The agent is never in the critical path of clinical care. It's additive or it's nothing.

Here's what the system looks like in practice. Dr. Chen logs into OpenEMR as she normally would. She clicks the "Co-Pilot" tab in the OpenEMR nav bar, and a chat panel opens inside an iframe — same surface metaphor as ChatGPT or Claude Desktop, scoped to her clinical session. She confirms her patient list for the day, and at that moment the agent fetches data for all of her patients from OpenEMR's API simultaneously, stores it in memory, and within 15 seconds delivers a ranked triage list back into the chat thread, sorted by clinical urgency. Every query she makes for the rest of her shift — patient briefings, lab questions, medication checks, handoff generation — is served from that cached data. That's why the responses are fast enough to use in a hallway between rooms.

The agent is built as a collection of Docker containers declared in the OpenEMR repository's existing compose stack. There's the agent API (a Python service that handles all the logic), a thin OpenEMR custom module that serves the React chat panel inside an iframe mounted from the Co-Pilot nav tab (a PHP shell only — it does no request handling), Redis (which holds the cached patient data and conversation state), and a monitoring stack (Langfuse, Grafana, Prometheus — all self-hosted, none sending data outside the network). The agent never writes to OpenEMR. It reads from OpenEMR exclusively through its FHIR API — not directly from the database, not through screen-scraping, but through the same API that any external system would use. This matters for compliance: every data access the agent makes is automatically logged in OpenEMR's audit trail.

The triage ranking — the heart of use case one — is done by a rules engine, not by AI. The 10 priority levels are evaluated deterministically: qSOFA criteria for sepsis, unacknowledged critical labs, rapid response events, abnormal but non-critical labs, stable overnight. The AI is called only after the ranking is complete, to write the one-line explanation for each patient. Clinical priority ordering must be traceable and reproducible. AI inference can't provide that guarantee; a rules engine can.

Every AI response passes through a verification layer before Dr. Chen sees it. This layer does two things. First, it checks that every clinical claim traces back to a specific record — with value, source type, and timestamp. Claims that can't be sourced are removed or flagged. Second, it enforces hard rules that the AI can't override: it can never say "no known allergies" if the allergy section has blank fields, it must always flag blank code status, it must mark any critical value older than 30 minutes as potentially stale. These rules exist because prompt engineering alone cannot guarantee safety in a clinical context.

A few key decisions are worth understanding. The W1 dispatcher uses the raw Anthropic SDK + a custom `Checkpointer` rather than a framework like LangGraph — the five conversational tools are simple enough that transparency and auditability matter more than framework abstractions. The W2 doc-ingestion graph (supervisor + workers + critic) ships on LangGraph 0.2.60 and is live as of W2; both are described in `W2_ARCHITECTURE.md §5` and in §8.1 below. Real patient data doesn't enter any AI prompt until a Business Associate Agreement with Anthropic is signed and a breach response procedure is documented — that's not an engineering decision, it's a legal one, and the pilot runs on synthetic data until both are in place. The full reasoning behind these decisions, including the ones we debated and rejected, is in the sections below.

---

The Clinical Co-Pilot is a sidecar AI agent that gives rounding hospitalists fast, verified, citation-grounded answers about their patients in the 90-second window between rooms. It ships inside OpenEMR's repository as an isolated runtime alongside OpenEMR's processes, and accesses patient data exclusively through the FHIR R4 API. It does not replace the EHR. It does not write to the medical record. It does not make clinical decisions. It surfaces what the chart says, cites the source, and stops.

The central architectural principle is that the agent is additive and never in the critical path. A crashed agent container does not affect OpenEMR uptime. The OpenEMR instance continues to operate normally. The agent panel in the UI displays a degraded state with a direct link to the relevant chart section. This is not a fallback — it is a first-class design constraint that shaped every decision in this document.

Three hard constraints determined the architecture. First, HIPAA compliance: patient data cannot be transmitted to any external system without a Business Associate Agreement, observability tooling cannot log PHI, and every FHIR query the agent makes must appear in the OpenEMR audit log. Second, sub-5-second response latency for the four most common use cases (UC-2, UC-3, UC-4) and sub-15-second for the census-wide triage list (UC-1): these targets are structurally unachievable against cold FHIR calls through the current OpenEMR service layer, which generates 190+ database queries for moderate lab history due to known N+1 patterns. Third, zero confident incorrect answers: a clinical agent that states a potassium is normal when it is not, or cites an allergy section as clean when it has blank fields, destroys physician trust faster than any latency failure. The verification layer exists because this constraint cannot be satisfied by prompt engineering alone.

Key architectural decisions and their rationale:

**Sidecar runtime, repo co-located.** The agent's Python services (`agent-api`, Redis, monitoring) run as separate Docker containers declared in the OpenEMR repository's compose stack. All agent code lives inside this OpenEMR fork — not in a separate repository. The OpenEMR codebase does contain one agent artifact: a thin custom PHP module under `interface/modules/custom_modules/oe-module-clinical-copilot/` whose sole job is to authenticate the OpenEMR session and return an HTML page that loads the compiled React bundle. That PHP module does no request handling, no proxying, and no state management. Every rounding query travels directly from the React panel to `agent-api` — OpenEMR's PHP-FPM pool is not in the query path. This preserves the core failure isolation property: a crashed `agent-api` container does not affect OpenEMR uptime, and the PHP module failing to load degrades to a "Agent unavailable" message without disrupting clinical workflows. Embedding substantive agent logic in PHP would bind the agent's failure domain to OpenEMR's web tier and force synchronous execution on a workload that requires parallelism and persistent state — the thin-shell boundary is the line that prevents this. Specifically, the PHP module does not proxy agent API requests, does not transform agent responses, does not hold session state beyond what OpenEMR's session already provides, and is not in the request path for any rounding interaction. If a future change would require any of these, that change requires architectural review — it is not a routine module enhancement.

**FHIR, not direct SQL.** The agent reads from the OpenEMR FHIR R4 API exclusively. The standard REST API was eliminated because it has no lab result endpoint — labs are FHIR-only. Direct database access was eliminated because it bypasses audit logging and creates schema coupling that breaks silently on OpenEMR upgrades.

**Deterministic rules engine for triage ranking.** The UC-1 triage list is produced by a rules engine that evaluates a configurable 10-level priority table against cached FHIR data. The LLM is called only to generate the one-line explanation per patient after the rank is determined. LLM inference does not determine clinical priority ordering.

**Pre-fetch at session open.** All census patient FHIR bundles are fetched in parallel when Dr. Chen opens the agent. On-demand cold-path fetches cannot meet the UC-3 three-second latency target and defeat the UC-1 requirement that the triage list be available without any query. The cost of session-open latency is acceptable because she opens the agent twenty minutes before she needs it.

**Redis for cache and session state.** Redis provides sub-millisecond cache reads for tool calls during active rounding and survives the 20-minute interruptions USERS.md requires the agent to handle. In-process caches do not survive interruptions. PostgreSQL adds latency that violates the UC-3 target.

**Self-hosted observability.** Langfuse and Grafana run as Docker services in the same compose network. No observability data leaves local infrastructure. This eliminates any third-party PHI dependency for the observability pipeline, regardless of whether traces contain PHI or not.

**Synthetic data only for pilot.** The demo and pilot run exclusively on a synthetic FHIR dataset. No BAA with Anthropic is signed. Synthetic data is not PHI and carries no HIPAA obligation. The pilot measures workflow adoption and latency performance. Clinical accuracy against real chart complexity is a post-BAA measurement.

**Pull-only notifications in v1.** The qSOFA push alert daemon described in USERS.md is designed but not activated. The OpenEMR REST API inbox write endpoint requires verification before the daemon is safe to activate. The push exception becomes a v1 configuration flag set to disabled, flipped to enabled in v2 after the write endpoint is confirmed.

Known gaps are documented honestly in Section 8: voice input, real-patient pilot, push alert daemon, family meeting use case (Decision 4), and resident-facing RBAC views are all v2.

---

## 1. System Overview

### 1.1 Architecture Pattern

The agent runs as a set of Docker containers declared in `docker/development-easy/docker-compose.yml` in this OpenEMR fork. It communicates with OpenEMR exclusively via the FHIR R4 API at `/apis/default/fhir/*` and the OpenEMR REST API at `/apis/default/api/*`. The Python `agent-api` service shares no database connection and no process space with OpenEMR.

**Repo layout for agent code:**

```
interface/modules/custom_modules/oe-module-clinical-copilot/   ← thin PHP shell only
  module.php          ← registers the module with OpenEMR; reads OpenEMR session context
  index.php           ← returns HTML loading the compiled React bundle; no logic
  public/
    copilot.js        ← compiled React bundle (built from agent-ui/ source)
    copilot.css

agent-api/            ← Python FastAPI service; all agent logic lives here
agent-ui/             ← React source; compiled output goes to the module's public/ dir
agent-monitoring/     ← Langfuse, Grafana, Prometheus compose definitions
```

The PHP module is a thin shell. It authenticates the OpenEMR session, extracts the provider ID and session date, and returns an HTML page that loads `copilot.js`. It proxies nothing. It handles no agent requests. Every rounding query travels directly from the React panel in the physician's browser to `agent-api` over the internal Docker network. OpenEMR's PHP-FPM worker pool is not in the query path during rounding.

A crashed or unavailable `agent-api` container degrades gracefully. The React panel detects unavailability via a health check on load and displays: "Agent unavailable — view chart directly" with a direct link to the relevant OpenEMR chart section. OpenEMR continues to function normally. No clinical workflow is blocked.

**Precise failure isolation by component:**

| Component | Process boundary | OpenEMR impact if it crashes |
|---|---|---|
| `agent-api` | Separate Docker container | None — OpenEMR unaffected |
| `redis` | Separate Docker container | None — agent degrades to cold FHIR fetch (§3.4) |
| `monitoring-daemon` | Separate Docker container | None |
| PHP thin-shell module | OpenEMR PHP-FPM pool | Module returns error page; OpenEMR continues normally |

The PHP module is the one component that runs inside OpenEMR's process space. Because it does no work beyond session auth and static asset delivery, its failure surface is the same as any other OpenEMR page failing to load.

### 1.2 Docker Compose Service Map

| Service | Role |
|---|---|
| openemr | The forked OpenEMR instance. Contains the thin PHP module at `interface/modules/custom_modules/oe-module-clinical-copilot/`. Exposes the FHIR R4 API and REST API on the internal Docker network. |
| agent-api | Python FastAPI service. Handles all agent logic: FHIR pre-fetch, Redis cache management, triage rules engine, context construction, LLM calls, verification layer, tool dispatch. Separate container — not in the OpenEMR process space. |
| redis | Session state and FHIR context cache. Stores pre-fetched FHIR bundles, conversation history, and census membership per session. Separate container. |
| monitoring-daemon | Background qSOFA polling process. Designed and containerized in v1 but not activated. Activation requires verification of the OpenEMR REST API inbox write endpoint. Config flag `PUSH_ALERTS_ENABLED=false` in v1. |
| langfuse | Langfuse Cloud free tier. Receives the scrubbed event stream — no PHI. No self-hosted instance; no Railway service required. See §9.3 for BAA status. |
| grafana | Operational metrics dashboards. Reads from Prometheus. Displays latency histograms, cache hit rates, error rates, token consumption. |
| prometheus | Metrics collection. Scrapes agent-api and Redis for operational time-series data. |

Note: there is no standalone `agent-ui` container. The React panel is compiled to a static bundle and served by the OpenEMR PHP module. The React source lives in `agent-ui/` in the repo root; the compiled output is placed in the module's `public/` directory as part of the build step.

### 1.3 Data Flow Summary

The following sequence describes what happens from session open to first response:

1. Dr. Chen logs into OpenEMR using her existing OpenEMR credentials. No separate agent login is required.
2. The OpenEMR PHP module (`oe-module-clinical-copilot/index.php`) reads her provider session context and returns an HTML page loading the compiled React bundle (`copilot.js`).
3. The React panel extracts her provider ID and session date from the session context injected by the PHP module at page load. No subsequent PHP calls are made — all agent communication goes directly to `agent-api`.
4. The `agent-api` authenticates to the FHIR API via a Client Credentials grant with SMART system-level scopes. The OAuth token is held server-side and never exposed to the browser.
5. Census is resolved: Dr. Chen inputs her patient list manually at session open. FHIR CareTeam auto-populate is attempted and used if data quality is verified; otherwise manual input is used as the fallback.
6. FHIR bundles for all census patients are pre-fetched in parallel across seven resource types and stored in Redis. Pre-fetch fires as soon as the census list is confirmed.
7. The UC-1 triage ranking runs as a deterministic rules engine against the cached FHIR data. The LLM is not called for ranking.
8. The LLM is called to generate the one-line explanation per patient after ranks are assigned.
9. The ranked triage list is delivered to the UI in under 15 seconds (UC-1).
10. All subsequent queries hit Redis first. FHIR is called only for extended-window UC-3 queries where the physician explicitly requests a search beyond the default cache window.

---

## 2. Authentication and Authorization

### 2.1 Provider Authentication

Dr. Chen authenticates to OpenEMR using her existing OpenEMR credentials. The agent panel reads her provider session context — her OpenEMR user ID, provider ID, and active shift date — from the OpenEMR session. No separate agent login is presented. Session is tied to her OpenEMR provider ID and shift date. If her OpenEMR session expires, the agent session expires with it.

### 2.2 FHIR Authentication

The `agent-api` service authenticates to the OpenEMR FHIR endpoint using a machine-to-machine Client Credentials OAuth 2.0 grant. The service account is registered as a confidential client in OpenEMR's OAuth2 system. The token is held by the `agent-api` service and is never exposed to the browser client or included in any response to the React panel.

SMART system-level scopes are requested across exactly seven resource types: `system/Patient.rs`, `system/Encounter.rs`, `system/Observation.rs`, `system/Condition.rs`, `system/MedicationRequest.rs`, `system/AllergyIntolerance.rs`, `system/DiagnosticReport.rs`. No write scopes are requested. Token refresh is handled automatically by the `agent-api` before expiry, without user interaction.

### 2.3 Census Proxy and Access Control

A proxy layer is implemented within the `agent-api` service and sits logically between the tool dispatch layer and the FHIR API client. It maintains the active census membership list for the current session and validates every outbound FHIR request against that list before the request is issued.

Rules enforced by the proxy layer:

- The agent cannot receive a FHIR response for a patient not on the active census. Any request for an out-of-census patient ID is rejected before the FHIR call is made.
- Cross-coverage access requires explicit per-patient physician confirmation via the UI prompt: "Patient [name] is not on your current service. Do you have documented cross-coverage authorization for this patient?" The agent does not infer authorization.
- Cross-coverage access is session-scoped only. It expires at session end and does not persist to subsequent sessions.
- Every cross-coverage access event is written to the OpenEMR audit log with: provider user ID, patient ID, timestamp, and the action text "cross-coverage session access — physician confirmed."
- A persistent UI banner is displayed throughout any period of cross-coverage access: "Cross-coverage mode — [patient name] — access expires at session end."
- The agent never grants blanket access to all patients of a colleague from a single confirmation. Each patient requires a separate confirmation.

### 2.4 RBAC Model

| Role | Census Access | Cross-Coverage | Audit Log Written |
|---|---|---|---|
| Attending on service | Own census patients | With explicit per-patient confirmation | Yes — every FHIR query |
| Resident (PGY-1/2) | Assigned patients only | Not permitted in v1 | Yes — every FHIR query |
| Medical student | Read-only, assigned patients | Not permitted | Yes — every FHIR query |
| Nocturnist / cross-coverage attending | With explicit per-patient confirmation | Yes, with confirmation | Yes — every FHIR query |

---

## 3. Data Architecture

### 3.1 OpenEMR Schema — Clinically Relevant Tables

The agent reads from the following OpenEMR database tables via the FHIR R4 API. It does not query the database directly. The FHIR layer is the only access path, ensuring all reads are audit-logged and schema-decoupled.

- **patient_data** — demographics, code_status (to be added), isolation (to be added)
- **form_vitals** — vitals history, used for qSOFA and NEWS2 calculation
- **lists** — problems, medications, and allergies (contains mixed free-text and coded values — the agent must handle both and must not treat a free-text entry as absent coded data)
- **openemr_postcalendar_events** — scheduled encounters and appointments
- **form_encounter** — encounter records, admit diagnoses, discharge planning notes
- **documents** — clinical notes, scanned records, and attachments
- **procedure_result** — laboratory values and diagnostic results
- **prescriptions** — medication orders

**Critical prerequisite:** `code_status` and `isolation` columns must be added to `patient_data` via a schema migration before any agent code is written against patient data. These are mandatory auto-flag fields per USERS.md §4. There is no fallback if they are absent — the agent cannot flag missing code status or isolation if the fields do not exist in the schema.

### 3.2 FHIR Resource Mapping

| FHIR Resource | OpenEMR Source Table | Agent Use |
|---|---|---|
| Patient | patient_data | Demographics, code status, isolation status |
| Encounter | form_encounter | Admit diagnosis, encounter history, discharge planning |
| Observation | form_vitals, procedure_result | Vitals trend, lab values, qSOFA and NEWS2 calculation, auto-flags |
| Condition | lists (type = medical_problem) | Active problem list, admit diagnosis |
| MedicationRequest | prescriptions, lists (type = medication) | Active medications, overnight additions, allergy cross-reference |
| AllergyIntolerance | lists (type = allergy) | Allergy section, completeness check, allergy-medication conflict detection |
| DiagnosticReport | documents, procedure_result | Imaging results, lab reports, clinical notes |

### 3.3 Redis Cache Architecture

| Cache Key Pattern | Contents | TTL | Invalidation Trigger |
|---|---|---|---|
| `provider:{id}:session:{date}:census` | Patient ID list for the active session | 4 hours | Manual update by provider at session open |
| `patient:{id}:vitals` | Observation bundle, vitals | 15 minutes | New critical value posted during background polling |
| `patient:{id}:labs` | Observation bundle, labs | 15 minutes | New critical value posted during background polling |
| `patient:{id}:medications` | MedicationRequest bundle | 30 minutes | New medication order detected during background polling |
| `patient:{id}:static` | Patient resource, AllergyIntolerance, code status, isolation | 4 hours | Manual invalidation only |
| `provider:{id}:session:{date}:conversation` | Full conversation thread for session continuity | Session duration | Session end or explicit clear |

Cache invalidation is not TTL-only. A background polling loop within the `agent-api` service refreshes the Observation and MedicationRequest endpoints for all active census patients every 10 minutes during an active session. When a new observation posting carries a value outside critical thresholds, that patient's vitals and labs cache entries are immediately invalidated and re-fetched before the next tool call that touches them. This is the mechanism that satisfies the clinical freshness requirement identified by the Clinical Informatics review — a 15-minute TTL alone is insufficient when an overnight potassium of 6.1 could post at any time.

### 3.4 Redis Failure Mode

Redis is a single point of failure for session state, conversation history, and the FHIR context cache. Its unavailability is treated as a degraded-mode scenario, not a hard failure.

If Redis is unavailable at session open:
- The agent falls back to cold FHIR fetch on every tool call. Pre-fetch is skipped.
- A persistent banner is displayed: "Running in degraded mode — session cache unavailable — response times will be slower."
- Conversation history is not available. The agent operates stateless for the duration of the session.
- The census list falls back to manual entry by the physician — the FHIR CareTeam auto-populate and the cached census key are both unavailable.
- The latency targets for UC-1 and UC-2 may not be met in degraded mode. The physician is informed of this explicitly in the banner.
- Degraded mode does not block OpenEMR access. The agent continues to serve queries; it is slower and stateless.

If Redis becomes unavailable mid-session (a connection drop after a successful session open):
- Tool calls that hit a cache miss log a `cache.miss.redis_unavailable` event and fall back to a live FHIR fetch.
- The physician is not notified of individual cache misses — only if 3 or more consecutive tool calls hit this path does the degraded-mode banner appear.
- Conversation history is reconstructed from the in-process request context for the current tool call only. Prior turns in the conversation are not recoverable without Redis.

Redis restart recovery is automatic. When the Redis connection is restored, the agent writes a fresh census key and resumes normal operation on the next session open.

### 3.5 Data Quality Constraints

The following constraints are enforced by the context construction layer and the verification layer. They are not aspirational — they are hard rules that the agent must apply on every response.

The `lists` table mixes free-text and coded values for medications and allergies. The agent must handle both formats. A free-text medication entry ("Lisinopril") is not treated as an absent coded entry. The agent presents the text as-is and notes the absence of a coded identifier where relevant.

The allergy section may be incomplete without being empty. A single allergy entry with no `reaction` field and no `allergy_type` field is not a clean allergy section. The agent counts total fields versus populated fields and reports the ratio. It never states "no known allergies" when any allergy field is blank. It states: "Allergy section incomplete — [N] of [M] fields blank — verify directly."

`code_status` and `isolation` are not present in the default OpenEMR schema. They must be added via migration before agent deployment. The migration is a hard prerequisite. The agent's auto-flag logic for blank code status cannot be implemented against a column that does not exist.

Lab result timestamps must be read from the FHIR Observation `effectiveDateTime` field — the time the observation was made — not the cache fetch timestamp or the resource creation timestamp. Any critical value whose observation timestamp is older than 30 minutes at the time of surfacing must be flagged with: "Value as of [observation timestamp] — verify current value in chart."

---

## 4. Agent Architecture

### 4.1 Use Case to Component Mapping

| Use Case | Trigger | Primary Component | LLM Called | Latency Target |
|---|---|---|---|---|
| UC-1 Morning Triage | Session open | Rules engine + LLM for one-line explanations | Yes — explanations only, not ranking | < 15 seconds |
| UC-2 Pre-Encounter Briefing | Patient tap or name query | Context builder + LLM | Yes | < 5 seconds |
| UC-3 Targeted Record Query | Follow-up question | Query router + FHIR fetch if needed + LLM | Yes | < 3 seconds |
| UC-4 Medication Safety Surface | Medication query or auto-flag | Context builder + LLM | Yes | < 5 seconds |
| UC-5 Handoff Generation | "Give me handoff" trigger | Multi-patient context builder + LLM | Yes | < 20 seconds |

### 4.2 UC-1 Triage Engine

The triage ranking is a deterministic rules engine, not LLM inference. This is the most consequential architectural decision in the agent design. Clinical priority ordering must be reproducible, auditable, and traceable to specific data values — properties that LLM inference cannot guarantee. A patient ranked third instead of first because of a probabilistic model output is a patient safety risk. A patient ranked third instead of first because a specific qSOFA calculation evaluated to 1 rather than 2 is a traceable, debuggable, correctable outcome.

The engine evaluates the 10-level priority table from USERS.md §5 against the cached FHIR data for all census patients. Each patient is assigned the priority level of their single most critical active signal. The LLM is called after ranking is complete, once per patient, to generate the one-line explanation that Dr. Chen sees in the triage list. The explanation is grounded in the specific data value that determined the rank.

The ranking configuration is a deployment-time YAML file. Priority weights, labels, triggering conditions, and thresholds are all configurable without a code deployment. Changing the YAML file changes the ranking behavior. This is the mechanism that makes specialty adaptation possible per USERS.md §9 — a neurohospitalist deployment changes the YAML, not the code.

Tie-breaking: patients at the same priority level are ranked by recency of the triggering event. The patient whose critical signal crossed threshold most recently ranks higher. Multi-flag patients rank by their highest-priority flag only — ranking is not additive. A patient with three WATCH-level flags does not outrank a patient with one URGENT-level flag.

### 4.3 Context Construction Layer

Before any LLM call, the context construction layer extracts only the fields relevant to the four clinical decisions from the FHIR bundles stored in Redis. Full FHIR bundles are never passed to the LLM. This layer is both the primary cost control mechanism — enforcing a per-use-case token budget — and a quality control mechanism, ensuring the LLM reasons over a structured, minimal context rather than raw API output.

**Prompt caching.** Every LLM call uses Anthropic's prompt caching via `cache_control: {type: "ephemeral"}` on the stable portions of the prompt. Two blocks are cached per session:

1. **System prompt block** — clinical rules, tool definitions, refusal language, and output format instructions. Identical for all calls within a session. Cached on the first call and reused for the duration of the session (~5-minute TTL aligns with the rounding cadence between patients).
2. **Census context block** — the pre-fetched FHIR summary for all census patients, assembled at session open by `get_census_summary`. Appended after the system prompt block with its own `cache_control` marker. Reused for all UC-2, UC-3, UC-4, and UC-5 calls within the session without re-fetching.

The variable portion of each prompt — the physician's specific query and the per-patient extracted context for that query — is appended uncached after the two cached blocks.

At the $0.30/M cached input token rate versus $3.00/M uncached, a 14-patient session where the census context block (~6,000 tokens) is reused across 14 briefings and 10 targeted queries reduces input token cost for those calls by approximately 80%. This is the primary mechanism by which the per-session LLM cost stays within the $0.25 estimate in §7.1 as query volume grows.

**Prompt injection isolation.** All free-text clinical content passed to the LLM — nursing notes, SOAP notes, medication names, allergy descriptions, and any other user-originated or chart-originated text — is wrapped in a delimited user-data block that is structurally separated from the system instructions block. The prompt structure is:

```
[SYSTEM INSTRUCTIONS — cached, not injectable]
...rules, tool definitions, refusal language...

[CENSUS CONTEXT — cached, structured FHIR fields only]
...structured key: value pairs, no free-text prose blocks...

[PATIENT DATA — uncached, delimited]
<patient_data>
  <nursing_note timestamp="...">{{free text here}}</nursing_note>
  <lab_result code="..." value="..." timestamp="..."/>
</patient_data>

[PHYSICIAN QUERY — uncached]
{{query text}}
```

The `<patient_data>` XML boundary prevents clinical free text from being parsed as instruction. A canary string (`SYSTEM_BOUNDARY_TOKEN`) is embedded in the system instructions block. The verification layer checks that this token never appears verbatim in any LLM response — its presence in a response indicates the model was manipulated into echoing system content, which triggers a hard refusal and an `agent.security.injection_detected` event in the observability stream.

Per-use-case context fields:

**UC-2 context** includes: admit diagnosis and admit date, overnight events summary (nursing escalations, acute vitals changes, new lab results since last physician note, new overnight medication orders), current vitals with 6-hour trend indicators, critical and abnormal labs with observation timestamps, new medications added in the last 12 hours flagged as NEW, code status and isolation status (always included — never omitted), pending items (unanswered consults with elapsed time, unresulted imaging with elapsed time).

**UC-3 context** includes a query-relevant subset only. The query router parses the physician's question, determines which resource types are required, and constructs a context from only those resources. A question about creatinine trend fetches Observation resources filtered to renal function lab codes. A question about prior steroid use fetches MedicationRequest history with the extended search window if requested. The context is never broader than the query requires.

**UC-4 context** includes: the complete AllergyIntolerance bundle with a completeness assessment (fields present vs. populated), the active Condition list (problems), relevant Observation values for electrolytes, renal function, and hepatic function, and the active MedicationRequest list with any new overnight additions flagged.

**UC-5 context** is assembled per patient across the full census. For each patient it includes: admit diagnosis, key morning events (from encounter notes if available, from FHIR resources otherwise), current plan elements, and pending items (unresolved consults, pending results, open plan elements). The context for each patient is constructed independently and parallel LLM calls are issued for all census patients simultaneously to meet the 20-second target.

### 4.4 Verification Layer

Every LLM response passes through the verification layer before delivery to the physician.

**Structured output contract.** All LLM calls use Anthropic's tool_use response format to produce machine-parseable output. Free-text prose responses are not accepted for any use case. The response schema per use case:

- **UC-1 / UC-2:** `{ critical_flags: [{value, source_type, timestamp, fhir_resource_id}], body: string, citations: [{claim_text, source_type, date, time}], code_status: string, isolation: string }`
- **UC-3:** `{ answer: string | null, source: {note_type, date, clinician} | null, search_window: string, not_found_reason: string | null }`
- **UC-4:** `{ allergies: [{substance, reaction, completeness_status}], relevant_conditions: [...], relevant_labs: [...], new_overnight_meds: [...] }`
- **UC-5:** `{ patients: [{ pid, diagnosis, key_morning_event, current_plan, open_item }] }`

The structured format makes the source attribution check a direct field comparison (does `citations[0].fhir_resource_id` exist in the context that was passed?) rather than NLP over free text. This makes the verification layer deterministic.

The verification layer performs two sequential checks after parsing the structured response.

**Source attribution check.** Every clinical claim in the response is compared against the context that was passed to the LLM. A claim is attributed if its value, its source type, and its timestamp all match a specific FHIR resource in the context. Claims that cannot be attributed to a source are one of two things: fabricated values (hallucinations) or inferences from the LLM's training data rather than from the patient's chart. Both are failures. Fabricated claims are removed and replaced with an explicit statement that the claim could not be sourced. Inferences are flagged as not sourced from this patient's chart.

**Domain constraint check.** After attribution, the response is evaluated against a set of hard rules before delivery. These rules cannot be satisfied by the LLM and are not delegated to it:

- If any allergy field in the AllergyIntolerance resource is blank, the response must not contain the phrase "no known allergies" or any equivalent. If the LLM output contains it, the verification layer removes it and inserts the completeness warning.
- If the code status field is blank or absent, the response must include the code status flag for that patient. If the LLM omitted it, the verification layer appends it.
- If any critical value's FHIR observation timestamp is older than 30 minutes at the time of delivery, the response must include a staleness flag on that value. If the LLM omitted it, the verification layer adds it.
- The response must not contain a diagnosis, a specific medication order recommendation, or a direct ICU transfer recommendation. If the LLM output contains any of these, the verification layer removes the offending claim and appends the appropriate refusal language from USERS.md §6.

If the verification layer catches a violation, it either rewrites the specific claim or appends an explicit correction before delivery. It does not silently pass a failing response. Every verification layer intervention is counted in the `agent.response.delivered` observability event as `verification_flags_count`.

### 4.5 Tool Definitions

| Tool Name | Triggered By | FHIR Resources Queried | Output |
|---|---|---|---|
| `get_census_summary` | UC-1 session open | All 7 resource types across all census patients | Ranked patient list with urgency tags, key flags, and one-line explanations |
| `get_patient_briefing` | UC-2 patient tap or name query | Patient, Encounter, Observation, Condition, MedicationRequest, AllergyIntolerance | Structured briefing context for default and expanded states |
| `query_patient_records` | UC-3 follow-up question | Query-determined subset; extended FHIR window if "search all records" is requested | Direct answer context with source citations and search window statement |
| `get_medication_safety` | UC-4 medication query or auto-flag | AllergyIntolerance, Condition, Observation (electrolytes, renal, hepatic), MedicationRequest | Chart-data-only safety surface context |
| `generate_handoff` | UC-5 "give me handoff" trigger | All 7 resource types across all census patients | Per-patient handoff paragraph context, assembled in parallel |
| `get_triage_rationale` | UC-1 census-row click | Direct-call (excluded from dispatcher's `TOOL_REGISTRY`) | Per-patient triage rationale narration |

The first five tools are the conversational-loop set, registered in `agent/tool_registry.py:24-30 TOOL_REGISTRY` and dispatched via `tools=[...]` on the Anthropic API. `get_triage_rationale` is a direct-call tool registered in `agent/tool_registry.py:34 DIRECT_TOOL_REGISTRY` — it bypasses the dispatcher loop because the 2-second click-to-expand latency target cannot be met through tool selection + execution + final response. Both surfaces resolve to the same content contract; only the invocation path differs.

### 4.6 HTTP Routes

**Methodology:** Routes are registered in two places: directly in `agent-api/main.py` via `@app.*` decorators (26 routes) and via `app.include_router(_staging_router)` at `main.py:3657`, which mounts 6 routes from `agent-api/staging/router.py` (the human-in-the-loop document approval workflow). Total live route surface: **32**. Future enumeration must grep both files. Grouped below by functional purpose (W1 = dispatcher / tools / sessions; W2 = document ingestion / graph / evidence / staging; shared = infrastructure):

**W1 — dispatcher, tools, sessions, FHIR proxy**

| Route | Method | File:line |
|---|---|---|
| `/triage/census` | POST | `main.py:494` |
| `/briefing/{patient_id}` | POST | `main.py:603` |
| `/session/{session_id}/query` | POST | `main.py:635` |
| `/medication/safety/{patient_id}` | GET | `main.py:652` |
| `/handoff/generate` | POST | `main.py:698` |
| `/handoff/generate/stream` | POST | `main.py:724` |
| `/agent/triage_rationale/{patient_id}` | POST | `main.py:816` |
| `/agent/query` | POST | `main.py:841` |
| `/agent/prefetch/status` | GET | `main.py:928` |
| `/agent/prefetch` | POST | `main.py:951` |
| `/agent/client-timing` | POST | `main.py:1235` |
| `/session/{session_id}/message` | POST | `main.py:1261` |
| `/session/{session_id}/history` | GET | `main.py:1275` |
| `/fhir/patient/{patient_id}` | GET | `main.py:401` |

**W2 — document ingestion, graph, evidence, quarantine**

| Route | Method | File:line |
|---|---|---|
| `/document/ingest` | POST | `main.py:2035` |
| `/evidence/search` | POST | `main.py:2665` |
| `/agent/w2/dispatch` | POST | `main.py:2708` |
| `/document/post-ingest-context` | POST | `main.py:3054` |
| `/document/{document_reference_id}/chat` | POST | `main.py:3183` |
| `/document/quarantine` | GET | `main.py:3379` |
| `/document/quarantine/{quarantine_id}/claim` | POST | `main.py:3413` |
| `/document/quarantine/{quarantine_id}/match` | POST | `main.py:3483` |
| `/document/quarantine/{quarantine_id}/reject` | POST | `main.py:3566` |

**W2 — staging / human-in-the-loop approval** (mounted via `app.include_router(_staging_router)` at `main.py:3657`)

| Route | Method | File:line |
|---|---|---|
| `/pending-extractions` | GET | `staging/router.py:111` |
| `/pending-extractions/{pending_id}` | GET | `staging/router.py:151` |
| `/pending-extractions/{pending_id}/approve` | POST | `staging/router.py:221` |
| `/pending-extractions/batch-approve` | POST | `staging/router.py:281` |
| `/pending-extractions/{pending_id}/reject` | POST | `staging/router.py:367` |
| `/pending-extractions/{pending_id}/retry` | POST | `staging/router.py:405` |

**Shared — infrastructure**

| Route | Method | File:line |
|---|---|---|
| `/health` | GET | `main.py:387` |
| `/diag/fhir` | GET | `main.py:412` |
| `/audit/destruction-record` | POST | `main.py:301` |

This classification is documentation-only — routes are not split across
router modules. The dispatcher's W1 surface and the W2 graph share a
single FastAPI app instance.

### 4.7 [W2] Deployment deviation: FHIR Binary fallback chain

Document writes on the deployed Railway build do not flow through OpenEMR's FHIR `Binary` POST. The `Binary` route returns 404 on this OpenEMR build, the legacy REST `/api/patient/.../document` route returns 401 unrelated to OAuth scope, and ingestion falls through to a custom JWT-protected upload endpoint (`oe-module-clinical-copilot/public/upload.php`). FHIR `DocumentReference` GET also returns `total=0` because OpenEMR's `DocumentService::search` calls `can_access($_SESSION['authUser'])` and OAuth-bearer requests don't bind `authUser` into the session on this build. The provenance chain (Observation → DocumentReference → documents) holds end-to-end via the agent-api response envelope and direct `copilot_observations` MySQL queries; v2 bridges into OpenEMR's standard FHIR read controllers without rewriting v1.

Full analysis: `W2_ARCHITECTURE.md §4.2.1` (DocumentReference 404), `W2_ARCHITECTURE.md §4.2.2` (security tradeoff of the shared-HMAC custom path), `W2_ARCHITECTURE.md §4.2.4` (Observation write deviation), `docs/SECURITY_TRADEOFFS.md` (full reversibility plan). The deviation is gated by an environment variable (`COPILOT_JWT_SECRET`) — unsetting it disables both custom tiers cleanly.

---

## 5. Observability

### 5.1 Observability Stack

**Langfuse:** The agent uses **Langfuse Cloud free tier** (cloud.langfuse.com). The `agent-api` sends the scrubbed event stream directly to the Langfuse Cloud ingest endpoint via the Langfuse Python SDK. No self-hosted Langfuse container, no PostgreSQL, no ClickHouse. Integration is two environment variables: `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`.

**Why Cloud over self-hosted — the definitive answer:**

Self-hosted Langfuse requires three additional services: the Langfuse server, PostgreSQL, and ClickHouse. On Railway that is three additional paid services with their own failure modes, cold-start latency, and maintenance surface. The only reason to self-host Langfuse would be if the event stream contained PHI — and by design it does not. The scrubbed event schema in §5.2 contains only hashed provider IDs, tool names, boolean cache hits, latency values, and token counts. No patient identifiers, no prompt text, no completion text, no clinical values of any kind. A stream with no PHI requires no BAA and no self-hosted data residency control. Langfuse Cloud free tier gives 50,000 observations per month — more than sufficient for a demo and a pilot phase — and a significantly richer trace UI than the self-hosted version. The integration cost is two env vars versus a three-service ops problem. Self-hosting is the right answer if and only if PHI enters the stream. It does not.

**Prometheus** collects operational metrics from the `agent-api` and from Redis: request counts, latency histograms per use case, error rates, cache hit and miss rates, and Redis memory usage. **Grafana** reads from Prometheus and renders the operational dashboards.

Prometheus and Grafana run as Docker services in the Railway project. Langfuse does not — it is external. No Langfuse data residency or self-hosting requirement exists because the stream contains no PHI.

### 5.2 Scrubbed Event Schema

| Event Type | Fields Included | Fields Explicitly Excluded |
|---|---|---|
| `agent.session.opened` | provider_id (hashed), session_date, census_size, timestamp | Patient IDs, patient names, PHI of any kind |
| `agent.tool.called` | tool_name, resource_types_queried, cache_hit (boolean), duration_ms, error_code (if applicable), timestamp | Patient IDs, query text, response content, any free-text value |
| `agent.llm.inference` | use_case_id, model_id, input_tokens, output_tokens, latency_ms, verification_pass (boolean), verification_flags_count, timestamp | Prompt text, completion text, patient identifiers, any clinical value |
| `agent.response.delivered` | use_case_id, duration_ms, verification_flags_count, cache_hits, timestamp | Response text, patient data, any free-text value |

### 5.3 PHI Audit Log

A separate PHI audit log captures full prompt and completion content for every LLM call. This log is written to an internal log store within the deployment infrastructure and is never exported to any external platform, including Langfuse. Access requires an explicit privileged process — it is not browsable by default by any team member.

The PHI audit log is retained per the deployment's data retention policy. It is written in addition to the OpenEMR audit log, not instead of it.

The OpenEMR audit log captures every FHIR query the agent makes via the `agent-api` service account. Each log entry contains the provider's OpenEMR user ID, the queried patient ID, the resource type, the timestamp, and the action. This is the HIPAA-required audit trail for PHI access. The agent's service account must be distinguishable from human user accounts in the OpenEMR audit log — it is registered under a named service account, not a shared human credential.

### 5.4 Minimum Observable Questions

The observability stack must be able to answer the following questions at any time from the stored events and metrics:

- What did the agent do on a specific request, and in what order?
- How long did each step take?
- Did any tools fail, and if so, what was the error code and which resource type was being queried?
- How many tokens were consumed, and at what estimated cost?
- What is the cache hit rate for the current and recent sessions?
- How many verification layer interventions occurred, and at what rate?

### 5.5 Structured Logging, Metric Catalog, and Request-ID Contract

The observability primitives that satisfy §5.4 live in a dedicated leaf package, `agent-api/observability/`. The package contains the JSON log formatter, the `request_id` ContextVar and filter, and the shared `log_tool_outcome` helper. It must not import from any business sibling — this is enforced by the `observability-is-leaf` contract in `agent-api/.importlinter` alongside the existing `auth-is-leaf` and `checkpointer-is-leaf` rules. This guarantees that observability remains a cross-cutting concern that any layer can call into without creating a cycle.

**`X-Request-ID` propagation contract (agent-ui ↔ agent-api).**

- `agent-ui` generates a request ID per outbound request and sends it as the `X-Request-ID` header.
- `RequestIdMiddleware` in `agent-api/main.py` honors any inbound `X-Request-ID`. If absent, it generates one. The value is bound to a `request_id` ContextVar for the request's async lifetime and echoed back on the response.
- Every structured log line emitted from any layer during that request — middleware, dispatcher, tools, FHIR client, checkpointer — automatically carries the same `request_id` field via `RequestIdFilter`. A single ID is sufficient to grep the entire request lifecycle across processes.
- The header name is fixed (`X-Request-ID`). Either side may originate the value; neither side may rewrite it mid-request.
- The frontend logs the request ID alongside its own `agent_client_timing_seconds` submissions to `POST /agent/client-timing`, so client-perceived timings can be joined back to server-side spans.

**Metric catalog.** All metrics are exported from the `agent-api` Prometheus endpoint. New metrics added in this pass:

| Metric | Type | Labels | Emitted from |
|---|---|---|---|
| `agent_prewarm_duration_seconds` | Histogram | `outcome` | `main.py:_warm` (session-open prefetch) |
| `agent_prewarm_runs_total` | Counter | `outcome` | `main.py:_warm` |
| `agent_data_cache_hits_total` | Counter | `cache` (`bundle`/`briefing`/`census`/`explanation`) | `agent/tools/__init__.py`, `triage/census.py` |
| `agent_data_cache_misses_total` | Counter | `cache` | as above |
| `agent_prompt_cache_hits_total` | Counter | — | dispatcher / Anthropic call sites |
| `agent_prompt_cache_misses_total` | Counter | — | dispatcher / Anthropic call sites |
| `agent_fhir_token_cache_hits_total` | Counter | — | `auth/fhir_client.py` |
| `agent_fhir_token_cache_misses_total` | Counter | — | `auth/fhir_client.py` |
| `agent_checkpointer_ops_total` | Counter | `op` (`load`/`save`), `backend`, `outcome` | `agent/dispatcher.py` |
| `agent_checkpointer_op_duration_seconds` | Histogram | `op`, `backend` | `agent/dispatcher.py` |
| `agent_client_timing_seconds` | Histogram | `action` | `POST /agent/client-timing` (frontend-submitted) |
| `agent_census_dropped_patients_total` | Counter | — | `triage/census.py:_build_entry` |
| `agent_pid_resolution_total` | Counter | `method` (`normalize`/`name_match`) | `agent/dispatcher.py` (pre-scope tool_input rewrite) |
| `agent_citation_repoint_total` | Counter | `field` (`name`/`dob`/`sex`/`mrn`/`address`/`chief_concern`/`medication`/`allergy`/`family`/`unknown`), `outcome` (`kept`/`repointed_with_anchor`/`repointed_no_anchor`/`no_match`) | `extractors/intake.py:_repoint_citation` (Wave 2B spatial selector) |
| `agent_post_ingest_requests_total` | Counter | `endpoint` (`post_ingest_context`/`document_chat`), `outcome` (`success`/`error`) | `main.py::document_post_ingest_context`, `main.py::document_chat` |
| `agent_post_ingest_duration_seconds` | Histogram | `endpoint` (`post_ingest_context`/`document_chat`) | `main.py::document_post_ingest_context`, `main.py::document_chat` |

**Structured log event catalog.** All events are JSON-formatted via `JsonLogFormatter` and carry the per-request `request_id` field automatically. Listed fields are in addition to the standard log envelope (`timestamp`, `level`, `logger`, `message`, `request_id`).

| Event name | Level | Source | Fields |
|---|---|---|---|
| `prewarm_start` | INFO | `main.py:_warm` | `provider_id`, `census_size` |
| `prewarm_complete` | INFO | `main.py:_warm` | `duration_ms`, `bundle_outcomes`, `briefing_outcomes`, `census_outcome` |
| `prewarm_failed` | ERROR | `main.py:_warm` | `duration_ms`, `error` |
| `Pre-fetch patient warmed` | INFO | `main.py:_warm._warm_one` | `patient_id`, `triage_rank`, `warmup_order`, `duration_ms`, `cache` (`miss` when force-refresh, else `n/a`) |
| `tool_call_start` | INFO | `agent/dispatcher.py` | `tool_name`, `session_id`, `patient_id` |
| `tool_call_end` | INFO | `agent/dispatcher.py` | `tool_name`, `duration_ms`, `outcome` |
| `tool_outcome` | INFO | `observability/tool_logging.py:log_tool_outcome` | `tool_name`, `duration_ms`, `cache` (`hit`/`miss`/`n/a`), `session_id`, `patient_id` |
| `checkpointer_op` | INFO | `agent/dispatcher.py` | `op` (`load`/`save`), `backend`, `outcome`, `duration_ms` |
| `fhir_token_cache` | DEBUG | `auth/fhir_client.py` | `outcome` (`hit`/`miss`), `expires_in_s` |
| `client_timing` | INFO | `POST /agent/client-timing` | `action`, `duration_ms` |
| `dispatcher.pid_resolution` | INFO | `agent/dispatcher.py` | `original_input`, `resolved_pid`, `resolution_method` (`normalize`/`name_match`), `tool_name`, `session_id` |
| `tool_outcome` (post-ingest) | INFO | `main.py::document_post_ingest_context`, `main.py::document_chat` (via `observability/tool_logging.py:log_tool_outcome`) | `tool_name` (`post_ingest_context`/`document_chat`), `duration_ms`, `cache` (`n/a`), `patient_id`, `endpoint`, `outcome` (`success`/`error`) |
| `extractor_citation_repointed` | INFO | `extractors/intake.py:_repoint_citation` | `tool` (`intake`), `field_name`, `outcome` (`kept`/`repointed_with_anchor`/`repointed_no_anchor`/`no_match`), `from`, `to`, `candidate_count`, `chosen_bbox_id`, `chosen_granularity` (`WORD`/`LINE`/null), `anchor_bbox_id` (null when no anchor was used), `anchor_text_preview` (≤32 chars), `y_distance` (rounded PDF-points; null when no anchor), `value_preview` (≤32 chars) |

**W2 metric catalog (Phase 6.1 additions).** These instrument the document-ingest path, the LangGraph hybrid-RAG retriever, the critic, and the demographic comparator. All are defined in `agent-api/agent/metrics.py`. Spec lives in `W2_ARCHITECTURE.md` §10.2; this table reflects what is actually emitted by the code today.

| Metric | Type | Labels |
|---|---|---|
| `agent_w2_document_ingest_total` | Counter | `path`, `doc_type`, `outcome` |
| `agent_w2_extraction_duration_seconds` | Histogram | `doc_type`, `classifier_confidence_bucket` |
| `agent_w2_retrieval_duration_seconds` | Histogram | `mode` (`sparse`/`dense`/`rerank`/`merge`) |
| `agent_w2_retrieval_hits_total` | Counter | `mode` |
| `agent_w2_critic_decisions_total` | Counter | `decision` (`pass`/`soft_warn`/`hard_block`), `reason` |
| `agent_w2_demographic_checks_total` | Counter | `outcome` |
| `agent_w2_classifier_confidence` | Histogram | `doc_type` |
| `agent_w2_ocr_confidence` | Histogram | `doc_type` |
| `agent_watchdog_last_run_timestamp_seconds` | Gauge | — |

**Phase 9 multimodal-expansion metrics (slices 9.2 / 9.3 / 9.4 / 9.5 / 9.6 / 9.7).** New formats (HL7 v2, XLSX, DOCX, TIFF), the pre-extraction resolver + quarantine, the pending-write staging state machine, and the cross-source conflict pass each emit one Prometheus instrument per call site per the CLAUDE.md "Observability — verifiable latency claims" rule. Counters and histograms registered outside `agent/metrics.py` are owned by their package's `_metrics.py` module — `agent/metrics.py` carries marker-comment blocks pointing operators at each carve-out (lines ~325, 357, 366, 377, 385, 395). The carve-outs exist because importlinter contracts (`parsers-hl7-isolated`, `parsers-xlsx-isolated`, `staging-isolated`, `conflict-is-mostly-leaf`) forbid those packages from importing `agent`.

| Metric | Type | Labels | Emitted from |
|---|---|---|---|
| `agent_quarantine_total` | Counter | `reason_code` | `agent/metrics.py` (used by `main.py::document_ingest` + `demographics/quarantine.py`) |
| `agent_quarantine_transitions_total` | Counter | `from`, `to`, `role` | `demographics/quarantine.py` state-machine transitions |
| `agent_quarantine_resolver_decisions_total` | Counter | `outcome`, `format` | `demographics/resolver.py` (via `main.py::document_ingest`) |
| `agent_resolver_duration_seconds` | Histogram | `format` | `demographics/resolver.py` (via `main.py::document_ingest`) |
| `agent_hl7_parse_total` | Counter | `message_type`, `outcome` | `parsers/hl7/_metrics.py` (consumed by `parsers/hl7/dispatch.py`) |
| `agent_hl7_parse_duration_seconds` | Histogram | `message_type` | `parsers/hl7/_metrics.py` (consumed by `parsers/hl7/dispatch.py`) |
| `agent_xlsx_parse_total` | Counter | `outcome` | `parsers/xlsx/_metrics.py` (consumed by `parsers/xlsx/parser.py`) |
| `agent_xlsx_parse_duration_seconds` | Histogram | `outcome` | `parsers/xlsx/_metrics.py` (consumed by `parsers/xlsx/parser.py`) |
| `agent_xlsx_rows_extracted_total` | Counter | `sheet` | `parsers/xlsx/_metrics.py` (consumed by `parsers/xlsx/parser.py`) |
| `agent_doc_parser_calls_total` | Counter | `format`, `outcome` | `documents/docx_loader.py`, `documents/tiff_loader.py` |
| `agent_tiff_parse_duration_seconds` | Histogram | `outcome` | `documents/tiff_loader.py` |
| `agent_docx_parse_duration_seconds` | Histogram | `outcome` | `documents/docx_loader.py` |
| `agent_staging_transitions_total` | Counter | `from`, `to`, `role` | `staging/_metrics.py` (consumed by `staging/store.py`) |
| `agent_staging_endpoint_total` | Counter | `endpoint`, `outcome` | `staging/_metrics.py` (consumed by `staging/router.py`) |
| `agent_staging_endpoint_duration_seconds` | Histogram | `endpoint` | `staging/_metrics.py` (consumed by `staging/router.py`) |
| `agent_staging_writer_total` | Counter | `target_resource_type`, `outcome`, `write_error` | `staging/_metrics.py` (consumed by `staging/store.py::approve`/`write`) |
| `agent_staging_watchdog_runs_total` | Counter | `job`, `outcome` | `staging/_metrics.py` (consumed by `staging/watchdog.py`) |
| `agent_staging_watchdog_rows_total` | Counter | `job`, `action` | `staging/_metrics.py` (consumed by `staging/watchdog.py`) |
| `agent_staging_watchdog_duration_seconds` | Histogram | `job` | `staging/_metrics.py` (consumed by `staging/watchdog.py`) |
| `agent_cross_source_conflict_total` | Counter | `outcome`, `source_pair`, `tier` | `conflict/_metrics.py` (consumed by `graph/nodes/cross_source_conflict.py`) |

**Phase 9 multimodal-expansion log events.** Each parser, state-machine transition, and graph node emits one structured log line at the same boundary as its Prometheus instrument. Quarantine and staging audit-emit failures degrade to a single warning log line so a broken audit pool never silently drops the underlying transition.

| Event name | Level | Source | Fields |
|---|---|---|---|
| `hl7_parse_completed` | INFO | `parsers/hl7/dispatch.py` | `message_type`, `control_id`, `duration_ms`, `outcome` (`ok`), `document_reference_id` |
| `hl7_parse_failed` | WARNING | `parsers/hl7/dispatch.py` | `message_type`, `outcome` (`malformed`/`unsupported`), `code`, `document_reference_id` |
| `xlsx_parse_completed` | INFO | `parsers/xlsx/parser.py` | `document_reference_id`, `duration_ms`, `outcome`, `sheets_present`, `patient_rows`, `medications_rows`, `labs_trend_rows`, `care_gaps_rows`, `lab_reports_emitted`, `pending_tasks_emitted`, `warnings` |
| `xlsx_parse_failed` | WARNING | `parsers/xlsx/parser.py` | `document_reference_id`, `outcome`, `code`, `duration_ms` |
| `tool_outcome` (`docx_loader`) | INFO | `documents/docx_loader.py` | `tool_name` (`docx_loader`), `duration_ms`, `cache` (`n/a`), `outcome`, `n_paragraphs`, `tracked_changes_present`, `embedded_images_dropped` |
| `tool_outcome` (`tiff_loader`) | INFO | `documents/tiff_loader.py` | `tool_name` (`tiff_loader`), `duration_ms`, `cache` (`n/a`), `outcome`, `n_pages`, `n_blocks` |
| `staging_list` / `staging_get_one` / `staging_approve` / `staging_batch_approve` / `staging_reject` / `staging_retry` | INFO | `staging/router.py` | `endpoint`, `outcome`, `duration_ms`, `panel_id`, `staging_id` (where applicable) |
| `staging_audit_emit_failed` | WARNING | `staging/store.py` | `event_type` (the audit event whose emit was dropped) |
| `staging_watchdog_started` | INFO | `staging/watchdog.py` | `n_jobs` |
| `watchdog_audit_emit_failed` | WARNING | `staging/watchdog.py` | `event_type` |
| `quarantine_list` / `quarantine_claimed` / `quarantine_matched` / `quarantine_rejected` / `quarantine_*_rejected` | INFO / WARNING | `main.py::quarantine_*` | `panel_id`, `quarantine_id`, `outcome`, `duration_ms` |
| `cross_source_conflict_pass_complete` | INFO | `graph/nodes/cross_source_conflict.py` | `n_groups`, `n_collapsed`, `n_soft_warns`, `n_date_missing`, `duration_ms` |
| `cross_source_conflict_pass_failed` | WARNING | `graph/nodes/cross_source_conflict.py` | `error` |
| `cross_source_conflict_metric_failed` | WARNING | `graph/nodes/cross_source_conflict.py` | `error` |
| `cross_source_conflict_audit_emit_failed` | WARNING | `graph/nodes/cross_source_conflict.py` | `error` |

**Phase 9 audit `event_type` additions** (written via `audit/writer.py`, PHI-safe `detail_json`):

| `event_type` | Emitted from | `detail_json` shape |
|---|---|---|
| `document_quarantined` | `main.py::document_ingest` | `quarantine_id`, `reason_code`, `format` |
| `document_quarantine_claimed` | `main.py::quarantine_claim` | `quarantine_id`, `panel_id` |
| `document_quarantine_matched` | `main.py::quarantine_match` | `quarantine_id`, `resolved_patient_id` (FHIR id only) |
| `document_quarantine_rejected` | `main.py::quarantine_reject` | `quarantine_id`, `reason_code` |
| `extraction_staged` | `staging/store.py` | `staging_id`, `target_resource_type`, `panel_id` |
| `extraction_approved` | `staging/store.py` | `staging_id`, `target_resource_type` |
| `extraction_rejected` | `staging/store.py` | `staging_id`, `reason_code` |
| `extraction_written` | `staging/store.py` | `staging_id`, `target_resource_type`, `target_resource_id` |
| `extraction_write_failed` | `staging/store.py` | `staging_id`, `target_resource_type`, `write_error` |
| `cross_source_conflict_detected` | `graph/nodes/cross_source_conflict.py` | counts, source types, source IDs, ISO collection dates (no values, no prose, no free clinical text) |

**W2 audit event-type catalog.** PHI-safe audit events written via `audit/writer.py`. Spec lives in `W2_ARCHITECTURE.md` §9.4; this table reflects only what is currently emitted by the code (verified by `git grep "event_type=" agent-api/`). Events from §9.4 that are not yet emitted (e.g. `document_processing_timeout`, `document_extraction_abandoned`, `intra_doc_conflict_detected`, `record_evidence_contradiction`, `classifier_verdict`) are intentionally omitted here until the call sites land.

| `event_type` | Emitted from | `detail_json` shape |
|---|---|---|
| `document_ingested` | `main.py::document_ingest` | `path`, `size_bytes`, `page_count` |
| `document_extracted` | `main.py::document_ingest` | `kind`, `classifier_confidence`, `ocr_confidence_range`, `n_fields` |
| `node_handoff` | `graph/nodes/*.py` (supervisor, retriever, extractor, structured, critic, finalize) | `from_node`, `to_node`, `decision_reason`, `duration_ms` |
| `critic_decision` | `graph/nodes/critic.py` | `decision`, `violation_codes` |
| `demographic_check` | `graph/nodes/demographics.py` | `decision`, `reason_code` |
| `retrieval_completed` | `graph/nodes/retriever.py` | `sparse_hits`, `dense_hits`, `after_rerank`, `rerank_used` |

When adding a new tool, cache, or background task, follow the same pattern: emit one `tool_outcome` (or equivalent) structured log line via `log_tool_outcome` and at least one Prometheus counter or histogram. This is the rule that keeps every latency or cache-hit claim verifiable from logs and metrics without re-reading the code.

---

## 6. Evaluation Framework

### 6.1 Test Suite Structure

> **Status note (2026-W2 update).** The 47-test, five-category framework below is the original W1 design plan; it remains the design intent and is partially codified in W1 test docstrings (`tests/test_triage_rules.py`, `tests/test_query_router.py`, `tests/test_medication_safety.py`, `tests/test_verification.py` each declare which slice of the 47 they cover). Live enforcement, however, has shifted to two pytest markers — `hard_failure` (100% gate) and `clinical_accuracy` (95% gate) — declared in `agent-api/pytest.ini` and required by `agent-api/tests/conftest.py:21 REQUIRED_MARKERS`. Each test in the W1 suite carries one of the two markers; the category labels in the table below describe the *intent* but are not pytest markers themselves. The W2 eval gate (W2_ARCHITECTURE.md §11) is a separate, additive layer.

The original W1 evaluation framework targets 47 test cases organized into five categories, on the synthetic FHIR dataset with a fixed seed for reproducibility, run on every prompt change, every model version change, and every tool definition change.

**CI gate (current).** The full pytest suite runs on every PR. The `hard_failure` marker enforces 100% pass; the `clinical_accuracy` marker enforces 95%. Both are declared mandatory in `tests/conftest.py`. The W2 eval gate (`.github/workflows/copilot-eval.yml`) runs alongside, gating regression against `evals/baseline.json`. Any commit that modifies any of the following files triggers the full pytest suite automatically:

- `agent_api/prompts/system_prompt.txt` — any wording change
- `agent_api/tools/` — any tool definition change
- `agent_api/rules_engine_config.yaml` — any priority weight or threshold change
- `agent_api/config.py` or `.env` — any `CLAUDE_MODEL_ID` change

Merges to the main branch are blocked on any `hard_failure`-marked test failing. Merges are blocked if `clinical_accuracy`-marked tests fall below 95%. A passing eval run is required — not optional.

If a model version bump causes a hard-failure regression, the previous `CLAUDE_MODEL_ID` value is restored immediately and the new model is evaluated against the full suite + `diff_baseline.py` before any re-attempt.

| Category (design intent) | Test Count (planned) | Pass Threshold | Description |
|---|---|---|---|
| Hard failure — wrong patient | 8 | 100% | Agent returns data for a patient not on the census or outside scope without triggering the confirmation prompt |
| Hard failure — stale data | 6 | 100% | Agent presents a critical value without flagging that the observation timestamp is older than 30 minutes |
| Hard failure — blank allergy stated clean | 5 | 100% | Agent states "no known allergies" or equivalent when the allergy section has one or more blank fields |
| Hard failure — scope enforcement | 5 | 100% | Agent responds to a query that requires cross-coverage access without issuing the confirmation prompt |
| Hard failure — graceful degradation | 6 | 100% | Agent fails silently, returns a hallucinated response, or blocks OpenEMR access when the FHIR feed is unavailable |
| Clinical accuracy — auto-flags | 8 | 95% | Agent correctly surfaces all auto-highlight triggers defined in USERS.md §4 for patients meeting each triggering condition |
| Latency — per use case | 5 | 95% **(design intent — not enforced in pytest)**. Verified by grep: no `@pytest.mark.latency`, no latency assertions in `tests/conftest.py` or `pytest.ini`. UC-1..UC-5 SLA targets are tracked instead via `COST_LATENCY_REPORT.md` (live `/metrics` scrape — p50/p95 histograms per pipeline stage) and the per-UC budgets at `USERS.md` §SLA targets (UC-1 <15s, UC-2 <5s, UC-3 <3s, UC-4 <5s, UC-5 <20s). Treat this row as design-intent surface, not a gate. | Agent meets the latency target for each of the five use cases under the synthetic dataset load |
| Conversation continuity | 4 | 100% | Agent maintains full conversation context across a simulated 20-minute interruption and resumes without requiring re-explanation |

The category labels above describe the original design intent; the live gate is enforced by the two pytest markers (`hard_failure`, `clinical_accuracy`). One failing test marked `hard_failure` stops the ship.

### 6.2 Synthetic Dataset Requirements

The synthetic FHIR dataset must cover at least 12 patients and must include at least one patient exercising each of the following scenarios. The dataset is seeded with a fixed value for reproducibility across test runs.

- A patient with qSOFA ≥ 2 on current vitals — tests UC-1 urgent ranking, UC-2 flag-first briefing, and the qSOFA auto-highlight trigger
- A patient with a critical lab value posted with no physician acknowledgment in the chart — tests UC-1 unacknowledged lab ranking and UC-2 flag surfacing
- A patient with blank code status — tests the code status flag requirement on every response
- A patient with an incomplete allergy section (allergy entry present but `reaction` and `allergy_type` fields empty) — tests the allergy completeness check and the prohibition on stating "no known allergies"
- A patient with a new overnight medication matching a documented allergy — tests UC-2 and UC-4 auto-flag and allergy-medication conflict detection
- A patient with a discharge plan documented but a critical result still pending — tests the discharge-with-pending-result flag
- A patient with a consult request unanswered for more than 6 hours — tests the unanswered consult auto-flag
- A patient with conflicting values for the same data field across two notes — tests the conflict surfacing requirement from USERS.md §8
- A census of more than 16 patients — tests the alert fatigue compression behavior where the top 3 urgent flags are shown individually and the remainder are grouped
- A patient not on the primary census who Dr. Chen queries by name — tests the cross-coverage confirmation prompt and census proxy enforcement

---

## 7. Cost Analysis

### 7.1 Per-Request Cost Model

Pricing is based on `${CLAUDE_MODEL_ID}` (current value: see `agent-api/config.py`). Re-run the full pytest suite + `diff_baseline.py` before promoting any model bump to production. Standard Anthropic pricing tiers apply: input tokens at $3.00 per million, output tokens at $15.00 per million, cached input tokens at $0.30 per million (see §4.3 for caching strategy).

| Use Case | Avg Input Tokens | Avg Output Tokens | Cost per Request | Notes |
|---|---|---|---|---|
| UC-1 Morning Triage (14 patients) | 8,000 | 1,400 | ~$0.057 | One LLM call per session open, not per patient |
| UC-2 Pre-Encounter Briefing | 2,000 | 400 | ~$0.008 | Per patient tap or name query |
| UC-3 Targeted Record Query | 1,500 | 300 | ~$0.006 | Per follow-up question |
| UC-4 Medication Safety Surface | 1,200 | 250 | ~$0.005 | Per medication query or auto-flag |
| UC-5 Handoff Generation (14 patients) | 10,000 | 2,800 | ~$0.074 | One LLM call per session end |

UC-1 and UC-5 are one call per session regardless of census size — the context for all patients is assembled into a single prompt. UC-2 through UC-4 depend on how many queries Dr. Chen makes during rounding. A representative session with 14 patient briefings, 10 targeted record queries, and 4 medication safety surfaces produces an estimated total LLM cost of approximately $0.25 per session. The $0.02 per patient briefing cost target (USERS.md §7) is met for UC-2 individually at $0.008 per call.

### 7.1.1 Force-Refresh Prewarm Cost

The OpenEMR landing-page prefetch (`interface/main/tabs/main.php`) fires on `DOMContentLoaded` after login and warms census + per-patient bundles + briefings + medication safety in the background, so the physician's first interaction with the Co-Pilot iframe is read-from-cache fast.

When the prewarm runs in **force-refresh** mode (the default for clinical correctness — see `PREFETCH_FORCE_REFRESH_ON_LOGIN` env var), every cache layer is bypassed and regenerated against live FHIR data. This burns Anthropic and FHIR cost on every login, even if the physician never opens the Co-Pilot tab afterward.

Per-login force-refresh cost (10-patient census):

| Component | Calls | Approx LLM cost |
|---|---|---|
| Census ranking + one-line explanations | 1 (UC-1) | $0.020 |
| Per-patient briefing × 10 | 10 (UC-2) | $0.080 |
| Per-patient medication safety × 10 | 10 (UC-4) | $0.050 |
| Per-patient bundle FHIR fetch | 10 | $0 (FHIR, not LLM) |
| **Total per login** | — | **~$0.15** |

At scale:

| Deployment | Logins/day | Daily prewarm cost | Annual prewarm cost |
|---|---|---|---|
| Single physician demo (Sara) | 1-3 | ~$0.45 | ~$165 |
| 10-physician unit | 30-50 | ~$5-8 | ~$2,000-3,000 |
| 100-physician hospital | 300-500 | ~$50-75 | ~$18,000-27,000 |
| 1,000-physician health system | 3,000-5,000 | ~$500-750 | ~$180,000-270,000 |

This is on top of the per-session interactive cost in §7.1. For Sara's demo it's negligible. For real deployment it justifies thinking carefully about when force-refresh fires (`PREFETCH_FORCE_REFRESH_ON_LOGIN=false` falls back to TTL-based cache reuse, dropping prewarm cost by ~80% but accepting up to 5 minutes of staleness on cached data).

Recommendations for production:

- **Pilot phase**: keep `PREFETCH_FORCE_REFRESH_ON_LOGIN=true`. Cost is bounded and clinical-correctness matters more than $50/day.
- **Post-pilot**: switch to `=false` and add a shift-aware warming cron — e.g. force-refresh census for every provider 5 minutes before their shift starts, then rely on TTL + bundle-fingerprint invalidation for the rest of the shift. This captures the freshness benefit at shift boundaries without the per-login multiplier.
- **Cost guardrail**: add a Prometheus alert when daily prewarm cost exceeds 20% of total LLM spend — that signals either overly aggressive force-refresh or excessive login churn.

### 7.2 Scaling Cost Projections

| Scale | Daily Sessions | Monthly LLM Cost | Infrastructure Notes |
|---|---|---|---|
| 100 users | 100 | ~$750 | Single Docker host, current Docker Compose architecture is sufficient |
| 1,000 users | 1,000 | ~$7,500 | Redis cluster, load-balanced agent-api behind a reverse proxy, managed PostgreSQL for audit logs |
| 10,000 users | 10,000 | ~$75,000 | Regional deployment, CDN for UI assets, dedicated FHIR proxy cluster, contract pricing with Anthropic likely required |
| 100,000 users | 100,000 | ~$750,000 | Full cloud-native architecture, multi-region, dedicated data residency per hospital system, Anthropic enterprise agreement required, dedicated SRE and compliance teams |

### 7.3 Architectural Changes at Scale

At 100 users the current architecture is sufficient. A single Docker host running the full compose stack can sustain this load without modification. Redis standalone is adequate. Audit logs can be file-based or written to the OpenEMR database.

At 1,000 users Redis must become a cluster. Standalone Redis becomes a single point of failure and a memory bottleneck when the FHIR cache for 1,000 concurrent sessions is resident. The `agent-api` must be load-balanced behind a reverse proxy — FastAPI handles concurrent requests well but a single instance cannot absorb the connection volume. Audit logs must move from file-based storage to a managed database with replication. The FHIR proxy layer becomes a bottleneck and must be horizontally scaled with a shared census store.

At 10,000 users the FHIR layer becomes the dominant cost and complexity. OpenEMR's FHIR API was not designed for the query volume that 10,000 concurrent sessions generate. The N+1 patterns documented in the performance audit compound at scale. A read replica or a dedicated FHIR server (HAPI FHIR) fed by OpenEMR replication becomes necessary to absorb the read load without impacting OpenEMR clinical operations. LLM costs at this scale require contract pricing with Anthropic.

At 100,000 users this is a platform, not a product deployment. The architecture requires multi-tenant isolation, regional data residency for HIPAA compliance across multiple hospital systems, dedicated infrastructure per tenant, an Anthropic enterprise agreement, and dedicated SRE and compliance teams. The current architecture is a correct foundation but the operational model changes entirely at this scale.

---

## 8. Known Gaps and V2 Roadmap

| Gap | Impact | V2 Path |
|---|---|---|
| Voice input | Typed input only in the 90-second hallway window — UX friction for a physician on a moving workstation with gloved hands | Speech-to-text pipeline with a post-processing clinical term disambiguation layer to handle misrecognition of drug names and patient identifiers |
| Push alert daemon | qSOFA deterioration alert requires Dr. Chen to have the agent session open — active deterioration while she is off the floor is not surfaced proactively | Verify OpenEMR REST API inbox write endpoint; activate the `monitoring-daemon` container by setting `PUSH_ALERTS_ENABLED=true` in the compose config |
| Real-patient pilot | Pilot validates performance and adoption metrics only — clinical accuracy against real chart complexity is not measured | Sign BAA with Anthropic; implement minimum-necessary PHI filter in the context construction layer; run real-patient pilot against the full go/no-go criteria in USERS.md §7 |
| Family meeting use case | Decision 4 (family meeting) is not covered in v1 — no use case surfaces goals-of-care signals, prognosis documentation, or family communication history | Extend the UC-3 query tool to support unstructured clinical narrative retrieval; the extension point is the `query_patient_records` tool which already handles arbitrary question routing |
| Multi-tenant RBAC | Current architecture is single-hospital deployment only | Hospital-scoped tenant isolation in the census proxy layer; cross-hospital data separation at the Redis key level |
| Resident-facing agent view | Residents use the attending view in v1; no scope differentiation between attending and resident | Resident-specific RBAC scope in the census proxy; attending supervision overlay that restricts certain tool outputs for resident sessions |
| LangGraph orchestration | W1 dispatcher uses raw Anthropic SDK + custom Checkpointer — kept transparent for clinical auditability | **W2 ships on LangGraph 0.2.60** (`requirements.txt:62`, `agent/graph/{build,state,nodes}.py`) — supervisor + workers + critic for the doc-ingest graph. The 'second agent type' trigger condition has fired; both runtimes are now live. See §8.1 below. |

---

### 8.1 Orchestration Framework Decision — Raw SDK for W1, LangGraph for W2 (both shipped)

**The decision (current state):** The W1 dispatcher uses the raw Anthropic SDK with a thin custom `Checkpointer` abstraction. The W2 doc-ingestion graph uses **LangGraph 0.2.60** (pinned in `requirements.txt:62`); the supervisor, four worker nodes (extractor / retriever / critic / cross-source-conflict), and the LangGraph state machine live in `agent/graph/{build,state,nodes}.py`. Both are live as of W2 ship.

The original §8.1 framing — "LangGraph is the designated v2 orchestration framework, adoption is triggered by the second agent type" — was written when LangGraph was prospective. The trigger condition has fired: the W2 multi-agent graph is the second agent type. The doc retains the migration-cost rationale below for historical context but the trigger is now in the past.

**Why not LangGraph in v1.**

The case for LangGraph was evaluated seriously. The checkpointer abstraction (swappable session persistence — Redis in production, Postgres or SQLite as fallback) is genuinely valuable and the argument for it is legitimate. The stateful session requirement (20-minute interruptions, conversation history that survives) and the tool dispatch routing are also cited as LangGraph use cases.

The counterargument that won:

- **This is a W1 single-agent, six-tool problem (5 dispatcher tools + 1 direct-call tool).** LangGraph's multi-agent coordination primitives — supervisor patterns, agent handoffs, parallel sub-graphs — are not needed when there is one agent with five statically-defined dispatcher tools, one direct-call tool (`get_triage_rationale`), and deterministic routing. Conditional edges in a StateGraph are the right abstraction for dynamic multi-agent routing, not for "if the physician asks about a medication, call `get_medication_safety`." (W2 ships a multi-agent supervisor + workers + critic on LangGraph; see `W2_ARCHITECTURE.md §5` and §8.1 of this doc for the W1/W2 split.)
- **Clinical auditability requires transparency.** Every step between a physician's question and a clinical response must be explicitly traceable. LangGraph abstractions that obscure what the agent is doing between nodes are a liability in this domain, not a convenience. A clear Python call stack beats a framework graph trace when the question is "why did the agent surface potassium as normal when it was not?"
- **Scaling to more users is an infrastructure problem.** Going from 3 attendings to 10,000 users requires Redis cluster, load-balanced agent-api, and FHIR proxy scaling (§7.3). None of that is solved by changing the orchestration framework. User volume does not change the orchestration requirements.
- **The migration cost is bounded if v1 is designed correctly.** The `Checkpointer` abstraction below is interface-compatible with LangGraph's `BaseCheckpointSaver`. When LangGraph is adopted, the persistence layer swaps cleanly without rewriting the agent loop.

**Why LangGraph becomes the right answer at v2.**

USERS.md §9 documents three physician types that require a full use case layer redesign: ED attendings, ICU intensivists, and outpatient PCPs. When any of these is built, the system has multiple distinct agent types — a hospitalist rounding agent, an ED triage agent, an ICU monitoring agent — that may need to coordinate, hand off session context, or run sub-graphs in parallel against the same patient. That is the problem LangGraph's multi-agent supervisor pattern is built for. At that point, hand-rolling coordination logic in FastAPI exceeds the cost of learning LangGraph.

**The trigger condition for v2 LangGraph adoption:** The first commit that implements a second agent type (ED, ICU, or outpatient workflow) is the trigger. Not a calendar date. Not a user count. The second agent type.

**V1 Checkpointer abstraction.**

The v1 implementation uses a thin abstract class designed to be interface-compatible with LangGraph's `BaseCheckpointSaver`:

```python
from abc import ABC, abstractmethod
from typing import Any

class Checkpointer(ABC):
    @abstractmethod
    def get(self, session_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def put(self, session_id: str, state: dict[str, Any]) -> None: ...

    @abstractmethod
    def delete(self, session_id: str) -> None: ...


class RedisSaver(Checkpointer):
    """Production path — backed by the Redis instance in the compose network."""
    ...

class SqliteSaver(Checkpointer):
    """Degraded-mode fallback — used when Redis is unavailable (see §3.4).
    Writes to a local SQLite file. Not suitable for multi-instance agent-api deployments."""
    ...
```

The `agent-api` session manager holds a `Checkpointer` reference injected at startup. `REDIS_URL` present → `RedisSaver`. Redis connection failure at startup → `SqliteSaver` with a degraded-mode banner. No other code in the agent loop knows which implementation is active.

When LangGraph is adopted in v2, `RedisSaver` is replaced by `langgraph.checkpoint.redis.RedisSaver` and `SqliteSaver` by `langgraph.checkpoint.sqlite.SqliteSaver`. The session manager injection point is unchanged. The migration is a dependency swap, not a rewrite.

---

## 9. Security and Compliance Summary

### 9.1 HIPAA Technical Safeguards Addressed

The table below reflects implemented technical controls. Implemented controls are not the same as a HIPAA compliance program. The operational and organizational layer — breach notification workflow, retention/destruction policy, legal hold procedures, incident response — is documented in §9.6 and §9.7 and is required before production deployment regardless of whether all technical controls below are marked complete.

| Safeguard | Implementation | Status |
|---|---|---|
| Access control | OAuth 2.0 Client Credentials grant; census proxy layer enforcing per-patient access; session-scoped cross-coverage with explicit confirmation | Controls implemented — operational policy required |
| Audit controls | OpenEMR EventAuditLogger captures every FHIR query by the agent's service account; PHI audit log captures every LLM prompt and completion | Controls implemented — retention policy required (§9.7) |
| Integrity | FHIR responses validated against census membership before use; verification layer catches fabricated claims before delivery; conflicting values surfaced rather than silently resolved | Implemented |
| Transmission security | All FHIR and REST API traffic over TLS; agent-api communicates with OpenEMR via HTTPS only; HTTP port 8300 is not used for any agent traffic | Implemented |
| Automatic logoff | Session TTL enforced by Redis; cross-coverage access expires at session end; agent session expires with the OpenEMR session | Controls implemented — Redis failure fallback documented in §3.4 |

### 9.2 PHI Handling Rules

1. Real patient data does not enter any LLM prompt until a signed BAA with Anthropic is in place. The system is designed and built on synthetic data only until that condition is met.
2. The synthetic dataset used for development and pilot contains no real patient identifiers. It is generated from a fixed seed and does not include any records derived from real patients.
3. The scrubbed observability event stream contains no patient identifiers, no prompt text, no completion text, and no free-text clinical values. It contains only operational metadata.
4. The PHI audit log is internal only and is never exported to any external platform, including Langfuse, Grafana, or any monitoring service.
5. Agent-generated output is ephemeral. It is not written to the EHR or the official medical record without explicit physician action. When the session ends, agent output is not persisted anywhere in OpenEMR.
6. All data in transit uses TLS. No unencrypted PHI transmission occurs between any two services in the compose network for clinical data paths.
7. Minimum-necessary principle: the context construction layer passes only the fields relevant to the four clinical decisions to the LLM. Full FHIR bundles are never passed to the LLM. SSN is excluded unconditionally from all context construction.

### 9.3 BAA Status

| Party | BAA Status | Consequence |
|---|---|---|
| Anthropic | Not signed | Synthetic data only. Production PHI pipeline is designed and documented but not activated. No real patient data enters any LLM prompt until signed. |
| Langfuse Cloud (free tier) | No BAA required | Scrubbed event stream contains no PHI per §5.2 — only operational metadata with hashed IDs. No patient data enters Langfuse. Cloud free tier is acceptable without a BAA on this basis. |
| All other services | No other third-party services receive data from this system in the current deployment configuration. | — |

### 9.4 API Key Management

The Claude API key is the highest-privilege secret in the deployment. Its management follows these rules:

- The key is stored as a Docker secret (`docker secret create`) and mounted into the `agent-api` container at `/run/secrets/claude_api_key`. It is never stored in `.env` files, never written to logs, and never included in any observability event.
- The key is scoped to the minimum required API surface: inference calls only. No admin API access.
- Rotation: the key is rotated on a 90-day schedule or immediately upon any suspected exposure. Rotation is a zero-downtime operation — the new key is staged as a second Docker secret, the `agent-api` service is updated to read the new key, and the old secret is removed after the deployment is confirmed healthy.
- The `CLAUDE_MODEL_ID` env var is stored in the Docker Compose environment file (`.env`) and is not a secret. It controls which model the agent calls but carries no credentials.

### 9.5 Prompt Injection Mitigation

The prompt injection mitigations documented in §4.3 (structured prompt layout, XML-delimited patient data block, canary token detection) are the primary defenses. Additional measures:

- **Input length limits.** The context construction layer enforces a maximum token budget per field. A nursing note exceeding 2,000 tokens is truncated with an explicit marker: "[Note truncated — view full note in chart]". This prevents a large injected payload from consuming the model's attention budget.
- **Observability hook.** Every `agent.security.injection_detected` event triggers an immediate Grafana alert (PagerDuty if configured). The affected session is terminated and the physician is shown: "Session terminated — security event detected. Please open a new session." The event is written to the PHI audit log with the full prompt content for forensic review.
- **No tool-call passthrough.** The agent's tool definitions do not include any tool that can write to OpenEMR, send messages, or execute shell commands. The tool surface is read-only FHIR. An injection that attempts to invoke a write operation has no tool to call.

### 9.6 Incident Response — LLM Provider Breach

If Anthropic reports a security incident involving the agent's prompts or completions, or if the project team discovers unauthorized access to the PHI audit log, the following response path applies. This is not an OpenEMR incident response plan — it is the agent-specific path for events involving the LLM provider.

**Step 1 — Containment (within 1 hour of discovery).**
Rotate the Claude API key immediately (see §9.4 rotation procedure). Set `CLAUDE_MODEL_ID` env var to a sentinel value that causes the `agent-api` to return a static "Agent temporarily unavailable" response to all requests. This stops new PHI from entering the LLM pipeline without disrupting OpenEMR. Do not delete the PHI audit log — it is evidence.

**Step 2 — Evidence preservation (within 4 hours).**
Snapshot the PHI audit log to an isolated, write-protected store. Snapshot the Redis conversation history keys before TTL expiry. Preserve Langfuse trace metadata (not PHI, but establishes timeline). Document the exact scope: which sessions, which patients, which time window, what data was in the prompts.

**Step 3 — Breach classification (within 24 hours).**
Determine whether the incident meets the HIPAA definition of a breach under §164.402: was there an unauthorized acquisition, access, use, or disclosure of PHI that compromises its security or privacy? If Anthropic experienced a security incident but there is no evidence that the agent's specific prompts were accessed, this is a risk assessment, not an automatic breach finding. Document the risk assessment with: the nature of the incident, the PHI in scope, the likelihood of compromise, the mitigating factors.

**Step 4 — Notification (within 60 days of discovery if breach is confirmed).**
- Affected patients: individual written notice per §164.404.
- HHS: annual summary report (or immediate if >500 patients affected).
- Media: if >500 residents of a single state or jurisdiction are affected.
- Responsible role for notification: designated Privacy Officer (to be named at deployment).

**Step 5 — Post-incident.**
Run the full pytest suite + diff_baseline.py against the current system state before re-enabling the agent. Rotate all secrets. Review and tighten the minimum-necessary PHI filter in the context construction layer based on what the incident exposed.

**Pre-production gate:** The incident response path above must be reviewed by the designated Privacy Officer and documented as approved before the BAA with Anthropic is signed and before real patient data enters any LLM prompt.

### 9.7 AI Artifact Retention and Destruction Policy

The following retention windows apply to AI-specific artifacts. These are policy commitments, not TTL configurations. Redis TTLs in §3.3 are latency parameters; they do not substitute for a retention policy.

| Artifact | Location | Retention Window | Destruction Method | Legal Hold |
|---|---|---|---|---|
| LLM prompts and completions | PHI audit log (internal log store) | 6 years from creation (HIPAA minimum) | Secure deletion with verification | Suspend automated deletion; preserve in isolated store |
| FHIR context cache | Redis | Per §3.3 TTLs (15 min – 4 hr) | TTL expiry; confirmed no residual PHI after session end | Not applicable — ephemeral by design |
| Conversation history | Redis | Session duration; deleted on session end | TTL expiry + explicit `DEL` on session close | Not applicable — ephemeral by design |
| Langfuse traces | Self-hosted Langfuse | 90 days (operational review window) | Langfuse admin deletion API | Suspend TTL; preserve in isolated export |
| Synthetic dataset | Docker volume | Duration of development/pilot phase | Volume deletion on environment teardown | Not applicable — not PHI |
| Backup snapshots containing PHI audit log | Deployment backup system | Per deployment backup policy (minimum 6 years) | Secure deletion per backup policy | Preserve snapshot; document hold in backup system |

**Destruction verification.** When any artifact is deleted, a destruction record is written to a separate compliance log: artifact type, retention window applied, deletion timestamp, method, responsible party. This log is itself retained for 6 years.

**No residual PHI in unmanaged stores.** Before production activation, the deployment must confirm that PHI does not appear in: Docker container stdout/stderr logs, Grafana dashboard labels or annotations, Prometheus metric labels, Langfuse trace fields beyond the defined scrubbed schema (§5.2), or any developer workstation outside the deployment environment.

### 9.8 Alerting Thresholds

The following conditions generate automated alerts in Grafana. These are operational thresholds, not patient safety alerts.

| Condition | Threshold | Alert Severity |
|---|---|---|
| UC-2 latency p95 | > 10 seconds | Warning |
| UC-3 latency p95 | > 8 seconds | Warning |
| Verification flags rate | > 5% of responses in a 10-minute window | Critical |
| Cache hit rate | < 50% over a 5-minute window | Warning |
| Redis connection errors | Any | Critical |
| FHIR 401 errors | > 3 in a 5-minute window | Critical |
| `agent.security.injection_detected` | Any | Critical — immediate |
| `agent-api` health check failure | 2 consecutive failures | Warning |

Critical alerts require acknowledgment within 15 minutes. Warning alerts are reviewed at the end of the shift. No alerting escalation to pagers is configured in v1 — all alerts surface in the Grafana dashboard only. PagerDuty integration is a v2 operational item.

---

## 10. Repo Co-location: Constraints and Open Questions

Building inside the OpenEMR repository introduces three constraints that are distinct from the engineering decisions documented in Sections 1–9. One requires ongoing organizational commitment (§10.2).

### 10.1 GPL Licensing — Note for Future Commercialization

OpenEMR is licensed under the GNU General Public License v3. For this project, all agent code lives in this fork as a single co-located codebase — GPL-licensed, fully open. This is the right choice for a project: it keeps the build simple, the CI pipeline in one place, and the eval suite in §6.1 working as written.

If this project ever moves toward commercial deployment or distribution, GPL copyleft becomes a material question. At that point, the artifacts most likely to require review are: `agent-api/prompts/system_prompt.txt`, `agent-api/rules_engine_config.yaml`, and the verification layer logic — the parts that represent clinical design decisions rather than generic engineering. Separating proprietary agent logic into a private repository at that stage would introduce cross-repo CI complexity (two repos need synchronized pipelines; prompt changes in the private repo must trigger the eval suite; OpenEMR fork changes must pull the latest agent image). That work is not scoped here and would need to be designed if the split is ever needed.

For now: everything co-located, everything GPL, one pipeline.

### 10.2 Upstream Upgrade Cost — Ongoing Operational Budget

Every OpenEMR upstream release is a merge that this fork owns in perpetuity. OpenEMR releases approximately 3–4 times per year. Each release requires:

1. Pull upstream changes into `clinical-copilot` and resolve merge conflicts against the compose additions and the PHP module.
2. Verify that the FHIR API surface has not changed in ways that break the agent's resource mapping (§3.2). OpenEMR's FHIR layer has evolved between releases — endpoint paths, resource field names, and SMART scope requirements have all changed in prior versions.
3. Re-run the full pytest suite + diff_baseline.py against the updated OpenEMR image.
4. Validate that the PHP module still loads correctly under the new OpenEMR release.

This is not a one-time cost. It is a recurring operational commitment that requires a named owner and a budgeted maintenance window per release cycle. It should be factored into the project timeline before committing to repo co-location as the permanent model.

### 10.3 Decision: Nav-Tab Iframe Integration Over Persistent Sidebar

The original UI plan was a persistent sidebar mounted into the OpenEMR shell so the agent panel sat alongside the chart at all times. In practice every implementation attempt broke OpenEMR's existing UI: CSS specificity collisions with Bootstrap 4.6 and the legacy theme bundles, layout reflow that pushed unrelated panes off-screen, and intermittent breakage of unrelated nav elements that depend on the shell's exact DOM shape. The pivot is to integrate the same way OpenEMR's own custom modules do — a "Co-Pilot" nav tab registered through the standard module loader at `interface/modules/custom_modules/oe-module-clinical-copilot/` that mounts the React bundle inside an iframe and presents a chat surface (chat input plus a scrolling message thread with per-response renderers for briefing, census, handoff, medication, and query). The tradeoff is real: less visual integration with the chart and an extra click to reach the agent.

The reasons the chatbox-via-iframe approach won out over the sidebar are concrete and additive: (1) the sidebar approach kept breaking OpenEMR's existing UI through CSS specificity collisions with Bootstrap 4.6 and the legacy theme bundles, layout reflow, and intermittent breakage of unrelated nav, and no amount of scoping selectors fully contained it; (2) a chat surface inside an iframe gives total CSS and JS isolation, so there is zero contamination risk for the rest of OpenEMR — the iframe boundary enforces this structurally, which is the same first-class constraint from §1.1 that the agent is never in the critical path of clinical care; (3) the chat metaphor matches how clinicians already think about asking the agent questions ("ask in a chat") and is the dominant LLM-product mental model, so there is no new UI pattern to learn on top of an already-loaded clinical workflow; (4) it mirrors how OpenEMR's own custom modules are integrated (nav tab → module page), so the integration follows the platform's existing extension contract instead of fighting it, which also limits the upstream-merge surface area discussed in §10.2; (5) it is materially faster to ship and iterate — changes to `agent-ui/` deploy independently of OpenEMR with no PHP, Twig, or Smarty work required to ship a UI change, which keeps the eval-gated release cadence in §6.1 cheap.

### 10.4 OpenEMR Module Conventions — Engineering Constraints

OpenEMR custom modules under `interface/modules/custom_modules/` are expected to follow specific registration conventions. The thin PHP module must conform to these to load correctly:

- `module.php` must register the module with OpenEMR's module manager and declare its name, description, and version
- The module directory name (`oe-module-clinical-copilot`) must match the expected naming pattern
- Any changes to OpenEMR's module loading mechanism in upstream releases become a merge concern (see §10.2)

The module is intentionally minimal to reduce the surface area exposed to these conventions. A module that only registers itself and serves a static asset has far fewer breakage vectors than one that hooks into OpenEMR's request lifecycle, renders templates, or accesses the database.
