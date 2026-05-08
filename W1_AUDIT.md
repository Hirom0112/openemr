# OpenEMR Fork — Pre-Integration Audit

**Date:** 2026-04-27  
**Branch:** clinical-copilot  
**Scope:** Security, Performance, Architecture, Data Quality, Compliance / HIPAA

> **Status (2026-W2 update):** Pre-W1 audit of the OpenEMR base, conducted before Week 1 implementation began. **Findings #1 (empty demo DB → UC-1..UC-5 producing nonsensical output) and #5 (`code_status` / `isolation` columns missing) have since been mitigated** by the synthetic 25-patient panel introduced in W1 and the Wave 2C generated corpus shipped in W2. See `W2_ARCHITECTURE.md` for the panel + corpus discussion. This document is retained as a historical record of the starting state. Other findings remain open per their original severity; re-verify before treating the table as current.

---

## Quick Read

Before we wrote a single line of agent code, we audited the OpenEMR codebase we're building on top of. The goal was simple: figure out what we're actually working with before we commit to an architecture. What we found reshaped the plan in several important ways.

The most urgent problem has nothing to do with AI. The demo database that ships with the development environment is essentially empty — three patients, no lab results, no nursing notes, and every clinical encounter is dated to a single day in 2014. The SOAP notes are placeholder text. Two fields the agent absolutely depends on — code status and isolation status — don't exist anywhere in the database schema. This means we can't meaningfully demonstrate any of the five core use cases until we fix the data. That's the first thing that needs to happen.

The second finding shaped the entire integration architecture. If you use OpenEMR's standard REST API, you cannot retrieve lab results. Labs only exist in the FHIR tier. This isn't a bug — it's just how the system is built — but it meant we had to make a clean decision: the agent integrates exclusively through the FHIR R4 API. That's the only surface that covers everything the agent needs in one place.

The third finding is the one we can't solve with code. We don't have a Business Associate Agreement with Anthropic. A BAA is the legal contract that makes an AI vendor a covered HIPAA business associate, and without one, sending real patient data — names, diagnoses, lab values, medications — to Claude is a direct HIPAA violation. There's also a gap on the process side: HIPAA requires a documented breach notification procedure specifying what happens if a vendor has a security incident involving your patient data, and we don't have that either. Both gaps require organizational action, not engineering. Until they're resolved, we build on synthetic data only.

There's also a performance problem worth understanding. One of OpenEMR's internal services runs over 190 database queries to fetch a single patient's lab history. Our target is to answer questions in under three seconds. Those two facts cannot coexist at production scale through the current code path. We've designed around it — the agent pre-fetches all patient data into Redis when the physician opens the session, so rounding queries never touch the slow path — but it's important to know why that design exists rather than treating it as optional.

Finally, there's a security gap in the authorization layer. The function responsible for checking whether a given API token should be allowed to read a specific patient always returns true. It was never fully implemented. Any authenticated token can read any patient in the system. The agent compensates with its own access control layer that enforces the physician's patient list, but this is a known gap in the underlying platform that needs to be addressed before expanding beyond a single-provider pilot.

The overall picture: OpenEMR is a solid foundation with good audit logging, strong authentication infrastructure, and comprehensive FHIR support. But there's a real difference between having the right technical controls and having a production-ready compliance posture. The controls exist. The operational layer — retention policies, incident response procedures, breach notification workflow — does not yet. That's what the findings register below is tracking.

---

---

## Risk Framework

OpenEMR has many of the right technical control points for a regulated clinical system, but the audit's central finding is that safety depends heavily on configuration, deployment discipline, and operational policy. Before adding any AI or agent capability, OpenEMR should be treated as a high-PHI, high-integration system where the core questions are not only whether the agent can answer correctly, but whether it accessed only authorized data, minimized PHI exposure, preserved auditability, met breach-response obligations, and relied on trustworthy source data.

**Security risk is concentrated in the split between modern `src/` services and legacy `library/` / `interface/` entry points.** API and FHIR paths have structured authorization listeners, bearer-token validation, OAuth scope checks, and ACL integration. Legacy scripts, custom modules, portal routes, upload/download handlers, and `ignoreAuth` patterns require an explicit inventory. Every path touching PHI must have: authentication, authorization, patient-context binding, CSRF/session protection where applicable, and audit logging. Core session behavior in legacy browser workflows creates tradeoffs that increase the importance of XSS prevention, output encoding, and secure deployment headers.

**Compliance controls exist but are not a compliance program.** OpenEMR provides event audit logging, optional audit-log encryption, breakglass logging, API logging, ATNA/syslog export, backup tooling, and tamper reporting. Those controls do not constitute a complete HIPAA compliance program. The audit explicitly identifies breach notification as an operational gap: the repository does not establish the HIPAA breach notification workflow — incident classification, evidence preservation, notification timing, responsible roles, required notification content, HHS/media escalation criteria, or handling of disclosures involving API clients, portal users, fax/SMS vendors, backups, logs, exports, or LLM providers. Any AI feature that sends PHI to a third party must be gated by BAA/subprocessor review and a documented incident-response path before that feature is activated.

**Data retention is deployment-dependent.** OpenEMR can create, store, export, back up, log, and sometimes delete PHI-bearing artifacts, but the audit must distinguish tool capability from policy compliance. Retention windows and destruction procedures must be defined for: audit logs, API logs, backups, clinical documents, EHI exports, portal messages, module/vendor data, and — critically for this project — any AI prompts, completions, embeddings, caches, or traces. The system must support legal holds, controlled deletion, backup expiration, restoration testing, and proof that PHI is not retained in unmanaged logs or vendor systems beyond policy. Redis TTLs in W1_ARCHITECTURE.md are latency parameters, not retention policy.

**Performance risk is database and logging dominated.** Broad FHIR/API searches, document retrieval, duplicate-patient checks, large audit/API logs, and legacy SQL-heavy workflows can all affect response latency. The agent must not perform open-ended chart scans. All data retrieval must use indexed, permission-scoped queries with explicit latency budgets per use case.

**Architecture is a hybrid system.** Modern services and REST/FHIR controllers coexist with procedural legacy code and table-centric data access. Data lives primarily in SQL tables — `patient_data`, `form_encounter`, `documents`, `lists`, audit tables, API logs — with UUID layers for FHIR interoperability. Integration is possible, but agents must honor the same boundaries as the UI and APIs: patient context, encounter context, ACLs, SMART scopes, portal separation, document permissions, and audit requirements.

**Data quality is a pre-agent safety gate.** Duplicate patients, stale encounters, soft-deleted documents, inconsistent UUIDs, missing fields, schema drift, and inconsistent UI/API validation can all produce unsafe or misleading AI output. The agent cannot be more accurate than the data it reads.

---

## Executive Summary

This audit was commissioned as a hard gate before AI integration work begins on the AgentForge Clinical Co-Pilot. The findings across five dimensions are serious enough to reshape the integration plan in at least three material ways before a single line of agent code is written.

**Demo data is the critical-path blocker.** The database loaded by `dev-reset-install-demodata` contains three patients, zero lab results, zero nursing notes, and all encounters dated to a single day in 2014. The SOAP notes are placeholder text ("Toe hurts / toe is black / Amputate toe"). Code status and isolation — two fields USERS.md designates as mandatory auto-flags — do not exist as columns in `patient_data`. UC-1 triage, UC-2 pre-encounter briefing, UC-3 lab queries, and UC-5 handoff generation would all produce empty or nonsensical output against this dataset. The demo cannot demonstrate any of the five core use cases in their intended form. Resolving this is the critical-path blocker for a working demonstration, independent of any code written on the agent side.

**FHIR R4 is the only viable integration point.** The standard REST API has no lab result endpoint — labs are exclusively accessible via the FHIR R4 tier (`/fhir/Observation`, `/fhir/DiagnosticReport`). Build the agent exclusively on FHIR R4. This is the single largest architecture decision the audit changes.

**BAA and incident-response path are paired deployment blockers.** The codebase contains no Business Associate Agreement with Anthropic and no documented incident-response procedure for a breach involving the LLM provider. Sending patient name, DOB, diagnoses, medications, labs, and clinical notes to the Claude API without both a signed BAA and an incident-response path is a direct HIPAA violation under §164.502 and §164.400–414. Neither is an OpenEMR gap — both are project-level gaps that require organizational action. The demo runs on synthetic data only until both are resolved.

**AI artifact retention is unaddressed.** Redis TTLs, the PHI audit log, and Langfuse traces are defined in W1_ARCHITECTURE.md as latency and observability parameters. None have a retention window, destruction procedure, or legal hold mechanism. This must be resolved before production deployment.

**Performance N+1 patterns are a known debt.** The `ProcedureService` N+1 pattern (190+ queries for moderate lab history) and the absence of any query caching make the 3-second agent latency target structurally unachievable against production-scale data through the current service layer. The agent must build its own read-optimized data layer rather than relying on the OpenEMR service layer directly.

**Security risk is bounded for the agent's access path.** The most significant findings are confined to legacy interface files and opt-in modules the agent will not touch. The one finding with direct agent relevance is the `checkUserHasAccessToPatient()` stub that always returns `true` — meaning any authenticated OAuth2 token reads any patient. The agent enforces its own census proxy as a compensating control until this is implemented in OpenEMR.

---

## Findings Register

The following table is the authoritative prioritized findings list. Severity: **Critical** = deployment blocker or patient safety risk / **High** = must be addressed before production / **Medium** = address before service-wide rollout / **Low** = remediate within 90 days. Ownership type: **Project** = requires organizational/legal action / **OpenEMR** = upstream codebase gap / **Agent** = agent architecture compensating control or design decision.

| # | SEV | Dimension | Finding | Ownership |
|---|-----|-----------|---------|-----------|
| 1 | Critical → **Mitigated (W1+W2)** | Data Quality | Demo DB has 3 patients, 0 lab results, 0 nursing notes; encounters all dated 2014-02-01. UC-1 through UC-5 produce empty or nonsensical output. **Mitigation:** synthetic 25-patient panel ships as the test/demo dataset; Wave 2C generated corpus extends it for W2 document-ingestion scenarios. See `W2_ARCHITECTURE.md` (synthetic panel discussion) and `agent-api/tests/fixtures/`. | Project |
| 2 | Critical | Compliance | No BAA with Anthropic. Sending PHI to the Claude API is a HIPAA §164.502 violation. | Project |
| 3 | Critical | Compliance | No documented breach notification workflow for LLM provider incidents. HIPAA §164.400–414 requires incident classification, notification timing, HHS/media escalation, and responsible roles. | Project |
| 4 | Critical | Compliance | No de-identification or minimum-necessary PHI layer. Every PHI field the agent reads from OpenEMR lands verbatim in the model prompt. | Agent |
| 5 | Critical → **Mitigated (W1+W2)** | Data Quality | `code_status` and `isolation` columns do not exist in `patient_data`. Two mandatory USERS.md auto-flags have no schema field to read from. **Mitigation:** the synthetic 25-patient panel populates code_status and isolation for every patient; the agent reads them through the FHIR shim with deterministic synthesized values that exercise both UC-2 (always-shown) and the auto-flag triggers. Underlying OpenEMR schema gap remains open as upstream tech debt. | OpenEMR |
| 6 | High | Architecture | `checkUserHasAccessToPatient()` at `src/RestControllers/Authorization/BearerTokenAuthorizationStrategy.php:479` always returns `true`. Any authenticated token reads any patient. Agent census proxy is the compensating control until fixed. | OpenEMR |
| 7 | High | Performance | `ProcedureService::getAll()` generates 190+ DB queries for moderate lab history via three nested N+1 loops (`src/Services/ProcedureService.php:711,728,745`). Blows the 3-second agent latency budget by an order of magnitude at production scale. | OpenEMR |
| 8 | High | Architecture | Standard REST API (`/api/`) has no lab/diagnostic-report endpoint. Labs are FHIR-only. Agent must integrate via FHIR R4 exclusively. | OpenEMR |
| 9 | High | Compliance | HTTP port 8300 exposed with no HTTPS redirect. PHI traffic over HTTP is cleartext — direct §164.312(e)(1) violation. Agent never uses port 8300 for any clinical data path. | Project |
| 10 | High | Compliance | SSN stored as plaintext in `patient_data.ssn` (`sql/database.sql:1245`). DB comment states it "should be encrypted in application" — no encryption call found. Agent excludes SSN unconditionally from all context construction. | OpenEMR |
| 11 | High | Security | Unauthenticated fax webhook (`interface/modules/custom_modules/oe-module-faxsms/library/webhook_receiver.php:25-35`) accepts arbitrary POST with no HMAC verification. Attacker can inject clinical records the agent might read as ground truth. | OpenEMR |
| 12 | High | Compliance | No retention windows or destruction procedures defined for AI artifacts: PHI audit log, Redis cache, Langfuse traces, LLM prompts and completions. Redis TTLs are latency parameters, not retention policy. No legal hold mechanism. | Project |
| 13 | Medium | Security | ACL coverage is low (~25% of interface files have explicit role checks). Agent accesses FHIR/REST APIs only, where ACL enforcement is present. Legacy path inventory required before agent service account is used for any interactive session. | OpenEMR |
| 14 | Medium | Security | Legacy SQL concatenation patterns and the `sqlStatement()` no-bind path present injection risk if user-supplied input is ever routed through legacy endpoints. Agent does not use legacy endpoints. | OpenEMR |
| 15 | Low | Compliance | `httponly=false` on core session cookie (by design, to support JS session restore). Increases XSS risk surface. No direct agent impact but increases importance of output encoding. | OpenEMR |

---

## Implications for the AI Agent

### Integration point: FHIR R4 only

The architecture audit closes the standard REST API as a viable single integration target. Labs are absent from it entirely. **Build the agent exclusively on FHIR R4** (`/apis/default/fhir/*`) using a Client Credentials grant with SMART system-level scopes (`system/Patient.rs`, `system/Observation.rs`, `system/Condition.rs`, `system/MedicationRequest.rs`, `system/AllergyIntolerance.rs`, `system/Encounter.rs`, `system/DiagnosticReport.rs`). This is the only API surface that covers all five use cases in a single auth scope.

### Authorization: census proxy is the compensating control

The `checkUserHasAccessToPatient()` stub means the RBAC boundary in W1_ARCHITECTURE.md cannot currently rely on OpenEMR's API layer to scope the agent's patient access. The agent enforces its own census proxy layer — restricting all FHIR requests to the PID list for the active session — as the compensating control. The agent must not accept arbitrary patient IDs from user input without verifying census membership first.

### Performance: no open-ended chart scans

The service-layer N+1 patterns mean the FHIR API cannot sustain sub-3-second responses for lab-heavy patients at production scale via open-ended queries. Every agent data retrieval must use indexed, permission-scoped queries against specific resource types and patient IDs. The Redis pre-fetch at session open is the mechanism that prevents per-query cold-path FHIR calls during rounding. The agent must never issue a broad FHIR search that spans the full patient record without a resource type filter and a time-window constraint.

### PHI pipeline: BAA, de-identification, and incident response are three separate gates

Three conditions must all be true before real patient data enters any LLM prompt:

1. **Signed BAA with Anthropic** — legal/organizational action required.
2. **Minimum-necessary PHI layer** — context construction layer excludes direct identifiers where possible; SSN excluded unconditionally.
3. **Documented incident-response path** — specifies what happens if Anthropic reports a breach involving the agent's prompts: notification timeline (60 days under HIPAA), evidence preservation steps, responsible roles, and patient notification criteria.

These are not sequential — all three must be completed before production activation. The demo runs on synthetic data until all three are satisfied.

### AI artifact retention: policy required before production

The following artifacts contain PHI or may contain PHI and require defined retention windows, destruction procedures, and legal hold mechanisms before production deployment:

| Artifact | Location | Current State | Required |
|---|---|---|---|
| LLM prompts and completions | PHI audit log (internal) | Logged, no retention window | Retention window + destruction procedure |
| FHIR context cache | Redis | 15-min TTL (vitals/labs), 4-hr (static) | Confirm TTLs satisfy minimum-necessary; no residual PHI after session end |
| Conversation history | Redis | Session-duration TTL | Confirm no cross-session persistence; explicit deletion on session end |
| Langfuse traces | Self-hosted Langfuse | No PHI (scrubbed event stream) | Confirm scrubbing is exhaustive; define trace retention window |
| Synthetic dataset | Docker volume | No real PHI | Document as non-PHI; confirm no real patient records introduced |

### Demo data: resolve before agent integration begins

The demo data gap is the single most urgent issue for the build. Options: load a richer synthetic dataset, generate synthetic FHIR resources and inject them via the FHIR API, or build a mock data layer. The real OpenEMR demo data cannot support any of the five use cases.

---

## Dimension Reports

### Security

Security risk is concentrated in the split between the modern `src/` path (structured authorization, ACL integration, bearer-token validation) and legacy `library/` / `interface/` entry points (variable ACL coverage, `ignoreAuth` patterns, legacy SQL concatenation). The agent accesses FHIR and REST APIs exclusively, where authorization enforcement is present. The most significant finding for the agent is the unauthenticated fax webhook (Finding #11) that can inject attacker-controlled documents into clinical record storage — records the agent might later read as ground truth. The RBAC stub (Finding #6) is the other direct agent risk. Legacy SQL patterns and low ACL coverage are noted but do not affect the agent's FHIR-only access path. `composer audit` could not be run in the sandbox and remains an open item.

### Performance

The agent's latency budget (UC-1: 15s, UC-2: 5s, UC-3: 3s, UC-4: 5s, UC-5: 20s) is at risk from three structural issues: (1) the `ProcedureService` three-level N+1 loop that can generate 190+ queries per lab-history fetch (Finding #7); (2) the `BaseService` constructor's `SHOW TABLES` + `SHOW COLUMNS` overhead on every service instantiation; (3) zero application-layer query caching. The `lists` table composite index gap `(pid, type)` means every condition/allergy/medication fetch does more work than necessary. The agent compensates by pre-fetching at session open and serving all rounding queries from Redis. The agent must never issue open-ended FHIR searches — every query must be scoped by resource type, patient ID, and time window.

### Architecture

The FHIR R4 API is the correct integration point. It covers all US Core resources the agent needs, uses standard SMART scopes, and supports machine-to-machine Client Credentials grants. The `RestApiCreateEvent` hook allows a custom module to add agent-specific routes without forking core. The key runtime risk is the token revocation mechanism: a session-cleanup job that removes a row from `oauth_trusted_user` mid-operation will silently invalidate the agent's bearer token, causing a mid-conversation tool call failure. The agent handles 401 responses by re-authenticating without user interaction before retrying the failed tool call.

### Data Quality

The demo database is insufficient for any of the five agent use cases. Critical gaps: zero lab results, zero nursing notes, all encounters on a single date, SOAP content is placeholder text, diagnoses coded in ICD-9 (retired 2015), medications have no RxNorm codes, the single allergy has no reaction type, and `code_status`/`isolation` fields the agent must surface do not exist as schema columns (Finding #5). Patient demographics are clean (100% populated). The broader data quality risks — duplicate patients, stale encounters, soft-deleted documents, inconsistent UUIDs — apply to production deployment and are the reason the agent's verification layer and conflict-surfacing behavior are mandatory, not optional.

### Compliance / HIPAA

The current compliance posture reflects a system with the right technical controls in place but without the operational and organizational layer that converts controls into a compliance program. Comprehensive audit logging is on by default, bcrypt/argon2id password hashing is correct, document encryption at rest is enabled. These are not the same as HIPAA compliance.

Four items are deployment blockers: (1) missing Anthropic BAA (Finding #2); (2) missing breach notification workflow for LLM provider incidents (Finding #3); (3) missing PHI de-identification layer (Finding #4); (4) missing AI artifact retention and destruction policy (Finding #12). Items (1) through (3) require organizational action. Item (4) requires an architectural addition to W1_ARCHITECTURE.md.

Within OpenEMR itself: the HTTP exposure (Finding #9), plaintext SSN (Finding #10), and missing log retention enforcement are production blockers. The `httponly=false` session cookie is a design decision with a documented rationale; it does not block the demo-phase build but increases the importance of XSS prevention at the deployment layer.
