# Clinical Co-Pilot — Agent Contract

**Status:** Authoritative — this is a grading artifact. It must be committed before Phase 8 Phase 1 begins. Any changes to the dispatcher envelope shape, session state fields, cutover gates, or claim taxonomy must update this document first.
**Cross-reference:** `ARCHITECTURE.md`, `USERS.md`, `docs/UX_SPEC.md`

This document defines the observable contract between the agent-api and the rest of the system. It is the source of truth for: what gates must pass before dispatcher cutover, what state the dispatcher loads per turn, which tools are justified by which user requirements, and what limitations the verification layer has.

---

## 1. Overview

The Clinical Co-Pilot agent contract governs the behavior of the dispatcher (`POST /agent/query`), the session state it loads, the tools it may invoke, and the conditions under which the legacy per-use-case endpoints may be retired.

This document is a grading artifact. The Phase 8 Phase 0 specification pass is not complete until this file is committed to the repository. Implementation work in Phase 1 (tool schemas) and Phase 4 (dispatcher loop) must trace back to sections in this document.

Items that appear in this document but are not yet implemented are marked **[PENDING]**. Items that are confirmed implemented are marked **[IMPLEMENTED]** or left unmarked. Discrepancies between this document and the actual implementation are bugs in the implementation, not in this document.

---

## 2. Numerical Cutover Targets

These gates must ALL pass before the dispatcher can replace the legacy per-use-case endpoints. Gates are measured in the Phase 12 parallel-run comparison. They are enforced in the Phase 13 cutover checklist. None can be waived.

| Gate | Threshold | Measurement Method |
|---|---|---|
| Routing accuracy | ≥ 95% on the 20-query eval set | `agent-api/tests/test_agent_routing.py`, Phase 6 eval suite |
| Dispatcher p95 latency | ≤ 4 seconds end-to-end | Production-equivalent load test (Phase 12 parallel run) |
| Error rate | ≤ 1% over 24-hour soak | Prometheus `agent_errors_total` / `agent_requests_total` |
| Tool misroute rate | ≤ 2% | Langfuse trace tagging: `misroute_detected` events / total dispatches |
| Cache-hit input tokens | ≥ 70% of total input tokens for UC-2/3/4 calls within a session | Langfuse token telemetry: `cached_input_tokens` / `input_tokens` per session, averaged across UC-2/3/4 calls |

**Cache-hit criterion detail:** The 70% threshold applies to UC-2, UC-3, and UC-4 calls only — not UC-1 or UC-5, which are one-call-per-session operations where the census context block is assembled fresh. For UC-2/3/4 calls, the system prompt block and census context block must both hit the Anthropic prompt cache. If this threshold is not met, the `cache_control: {type: "ephemeral"}` placement in `agent/dispatcher.py` must be debugged before cutover.

---

## 3. Session State Schema

The dispatcher loads the following state from Redis on every call to `POST /agent/query`. Changes to this schema require updating this document first, then updating `agent/dispatcher.py`.

### 3.1 Per-Session Fields

These fields are set at session open and are stable for the duration of the session (12-hour TTL).

| Field | Type | Description |
|---|---|---|
| `provider_id` | `str` | OpenEMR provider ID extracted from the session context by the PHP module. Hashed in Langfuse traces. |
| `patient_ids` | `list[str]` | Active census — the list of patient FHIR IDs confirmed by the physician at session open. The census proxy validates every outbound FHIR request against this list. |
| `session_start_utc` | `str` (ISO 8601) | UTC timestamp of session open. Used for freshness calculations and audit trail. |
| `last_viewed_patient_id` | `str \| null` | The patient ID most recently surfaced in a briefing or query response. Used by the dispatcher to resolve ambiguous references ("her", "this patient") without requiring clarification. Null at session open. |
| `last_tool_called` | `str \| null` | Name of the last tool invoked in this session. Used for misroute detection and audit. Null at session open. |
| `fhir_bundles_cached` | `bool` | True once the FHIR pre-fetch completes and all census patient bundles are in Redis. The dispatcher should not attempt to serve UC-1 until this is True. |

### 3.2 Per-Conversation Fields

These fields accumulate over the session and are bounded to prevent unbounded growth.

| Field | Type | Description |
|---|---|---|
| `conversation_history` | `list[dict]` | Bounded list of conversation turns in the format expected by the Anthropic SDK (`{"role": "user" \| "assistant", "content": ...}`). The system prompt and census context blocks are NOT included here — they are prepended to each API call separately (with `cache_control`) and are not stored in the conversation history. |
| `turn_count` | `int` | Total number of dispatcher turns in the current session. Used for audit and analytics. |

### 3.3 TTL and Eviction Policy

- **Session TTL:** 12 hours from `session_start_utc`. This covers a full day shift. At TTL expiry, the entire session key namespace is deleted from Redis. A new session requires a fresh "Go" press.
- **Conversation history max turns:** 20. When the history length reaches 20 turns, the oldest turn is evicted (FIFO) before appending the new turn. The system prompt and census context blocks are never evicted — they are not in the history list.
- **Redis failure fallback:** If Redis is unavailable, the dispatcher operates stateless for the current request: no history, no `last_viewed_patient_id`, no `last_tool_called`. See `ARCHITECTURE.md §3.4` for full degraded-mode behavior.

---

## 4. Tool → USERS.md Capability Trace

Every physician-facing capability must trace to a use case in USERS.md. This table is a grading artifact — it documents the justification for each tool and flags partial justifications for review.

| Tool | USERS.md Section | Justification | Notes |
|---|---|---|---|
| `get_census_summary` | UC-1 (Morning Priority Triage) | Produces the ranked census list delivered automatically on session open. Deterministic rules engine produces the P1–P10 ranking; LLM writes one-line explanations only. | Full justification. Rules engine config maps directly to USERS.md §5 UC-1 ranking priority weights table. |
| `get_patient_briefing` | UC-2 (Pre-Encounter Briefing) | Produces the 3–4 sentence default briefing and the expanded state on physician tap or name query. | Full justification. Output schema maps to USERS.md §5 UC-2 default and expanded state specifications. |
| `query_patient_records` | UC-3 (Targeted Record Query) | Produces direct answers to follow-up questions during rounding. Default search windows: 24 months encounters, 12 months labs. Extended search on explicit physician request. Multi-turn continuity maintained via session history. | Full justification. The conversational form of "why is bed 7 first?" routes through this tool as a UC-3 query. |
| `get_medication_safety` | UC-4 (Medication Safety Surface) | Produces the chart-data-only safety surface: documented allergies, active conditions, relevant labs (electrolytes, renal, hepatic), active medications. | Full justification. Never recommends dosing, never diagnoses interactions. Surfaces chart data only. |
| `generate_handoff` | UC-5 (End-of-Rounds Handoff Generation) | Produces one structured paragraph per patient: diagnosis, key morning event, current plan, one open item. Full census, parallel LLM calls inside one tool. Clipboard-copy only in v1. | Full justification. No auto-persistence to EHR in v1 per USERS.md §5 UC-5 persistence policy. |
| `get_triage_rationale` | UC-3 / ARCHITECTURE.md §4.7 | Click-to-expand rationale for a specific patient. Returns per-criterion scoring breakdown and plain-language explanation. Invoked directly via `POST /agent/triage_rationale` — NOT dispatched through the conversational loop. | **Partial justification — document engineering rationale explicitly:** USERS.md UC-1 implies rationale explanability ("she can ask 'why is bed 7 first?' and get a one-sentence answer"). The click-to-expand UI form is an ARCHITECTURE.md §4.7 design decision, not a USERS.md-specified endpoint. The two paths (conversational "why is bed 7?" → dispatcher → UC-3 query; UI expand tap → direct `POST /agent/triage_rationale`) must produce equivalent rationale content. Engineering rationale for the direct endpoint: the 2-second click-to-expand latency target (CLAUDE.md latency budgets) cannot be met through the dispatcher loop (tool selection + tool execution + final response = 3+ steps). The direct endpoint bypasses dispatcher overhead for a deterministic, bounded, latency-sensitive operation. |

---

## 5. Verification Layer Limitations

Cross-reference: `ARCHITECTURE.md §4.4` ("Known limitations of the verification layer")

The verification layer is mandatory on every LLM response. It performs source attribution and domain constraint checks. It is not optional and cannot be bypassed.

However, it has documented limitations that physicians must be aware of. These limitations are why the mandatory disclaimer footer ("Verify critical values directly in the chart") is non-negotiable on every response.

### 5.1 What the Verification Layer Catches

- Claims whose value, source type, and timestamp do not match any FHIR resource in the context passed to the LLM → removed and replaced with an explicit "could not be sourced" statement
- "No known allergies" or equivalent when any allergy field is blank → removed, replaced with completeness warning
- Blank or absent code status → flag appended
- Critical values with observation timestamps older than 30 minutes → staleness flag appended
- Diagnosis statements, specific medication order recommendations, direct ICU transfer recommendations → removed and replaced with appropriate refusal language

### 5.2 What the Verification Layer Does NOT Catch

The following failure modes are NOT prevented by the verification layer:

1. **Plausible-but-wrong values:** If a hallucinated value (e.g., K+ 5.1) matches a real value that exists elsewhere in the context (a different observation, a different timestamp), the attribution check will pass. The model associated the wrong observation with the claim, but the value is numerically plausible and present in the context. This is the highest-risk residual failure mode for clinical misuse.

2. **Structured hallucinations conforming to the schema:** The `tool_use` output schema enforces field presence but not value correctness. A value that is syntactically valid (a number in a numeric field) but clinically wrong will pass schema validation.

3. **Claims outside the cache window:** The verification layer checks claims against the context that was passed to the LLM. If a physician asks about data outside the default search window and the tool did not extend the window, the verification layer has no way to detect that a relevant FHIR resource exists in the chart outside the context.

4. **Omission errors:** If the LLM fails to surface a critical flag that exists in the data (e.g., fails to mention an elevated potassium), the verification layer appends hard-rule flags (blank code status, blank allergy, stale critical value) but cannot guarantee exhaustive coverage for non-rule-based clinical signals that were present in the context but not mentioned in the response.

### 5.3 Clinical Implications by Claim Class

| Claim Class | Risk Level | Physician Action Required |
|---|---|---|
| Lab values (K+, Na+, Cr, etc.) | High | Always verify directly in the chart before acting, regardless of citation |
| Critical vitals (HR, RR, SpO2, BP) | High | Always verify directly in the chart before acting |
| Allergy entries | High | Verify against the full allergy section in the chart; the completeness check is a proxy, not a guarantee |
| Code status | High | Verify directly — a blank field may mean "not documented" or may mean "field not loaded" |
| Active medication list | Medium | Spot-check new overnight additions before ordering |
| Encounter summaries and diagnoses | Medium | Verify against the admit note or attending note |
| Pending items (consults, imaging) | Medium | Verify elapsed times — cache TTL may not reflect the most recent status update |
| Handoff narrative | Medium | Dr. Chen reviews and edits before use; the generated text is a draft, not a final record |

### 5.4 Compensating Controls That Operate Independently of LLM Citation

These three hard rules execute in the domain constraint check regardless of what the LLM produced or cited:

1. **NKDA block:** If any allergy field is blank → remove "no known allergies" → insert "Allergy section incomplete."
2. **Blank code status flag:** If code status field is absent or blank → append the "Code status: NOT DOCUMENTED" flag.
3. **Stale critical value flag:** If any critical value's observation timestamp is older than 30 minutes → append the staleness marker with the observation timestamp.

These compensating controls are rule-based and deterministic. They do not depend on LLM output quality. They are the last line of defense for the three highest-risk omission scenarios.

---

## 6. Phase 13 Cutover Gates

The following conditions must ALL be confirmed before the dispatcher replaces the legacy per-use-case endpoints. These are enforced in Phase 12 (parallel run) and Phase 13 (migration).

**Precondition (must be confirmed before Phase 13 begins):**
Phase 5 dispatcher response verification must be shipped and green in CI: `verification/dispatcher_response.py` must be hooked after the dispatcher `end_turn`, and `test_dispatcher_verification.py` must be green. Without this, `POST /agent/query` runs without final-response verification during the cutover window, violating CLAUDE.md hard rule #5.

**Cutover gates (all must pass):**

1. Routing accuracy ≥ 95% on the 20-query eval set (Phase 6 routing eval, `test_agent_routing.py`)
2. Dispatcher p95 ≤ 4s, measured in the Phase 12 production-equivalent load test
3. Error rate ≤ 1% over a 24-hour soak in the parallel-run configuration
4. Tool misroute rate ≤ 2%, measured via Langfuse trace tagging in the parallel run
5. Cache-hit input tokens ≥ 70% of total input tokens for UC-2/3/4 calls within a session (Langfuse token telemetry, Phase 12 parallel run)
6. Phase 5 dispatcher response verification (`verification/dispatcher_response.py`) shipped, hooked after `end_turn`, and green in CI
7. Full 84-test eval suite green in CI (all hard-failure categories at 100%, clinical accuracy at 95%)

**Post-cutover:**
Legacy endpoints (`POST /triage/census`, `POST /briefing/{id}`, `POST /session/{id}/query`, etc.) remain active behind a feature flag for one full release cycle after gate passage. They are removed in the subsequent release after confirmed dispatcher stability.

**Pre-cutover manual Railway verification (Phase 12 spot-checks):**

These steps require Railway access and must be run after `python3 synthetic_data/load.py` completes successfully:

- `GET https://<railway-domain>/apis/default/fhir/Patient` — assert 23 patients returned
- Census endpoint spot-check: patients ranked P1–P10; confirm pt-019 at P9, pt-020 at P10
- Cross-check: `POST <agent-api>/agent/query` with census trigger returns same patient count as FHIR Patient list
- Confirm all 23 patients have isolation Flag resources (spot-check 3 bundles via `GET /fhir/Patient/{id}/$everything`)

---

## 7. Schema Migration Gate

**Status: PARTIALLY BLOCKED**

This section documents the status of the `code_status` and `isolation` field prerequisites defined in `ARCHITECTURE.md §3.1`.

### 7.1 `code_status` — Status: IMPLEMENTED (via FHIR Observation, not Patient extension)

`code_status` is NOT implemented as a Patient resource extension in the synthetic FHIR bundles. It is implemented as a FHIR Observation resource with LOINC code `81638-3`. The extractor in `agent-api/triage/criteria.py` reads it correctly:

```python
code_status_present = any(
    _loinc(entry.get("resource", entry)) == "81638-3"
    for entry in resources.get("Observation", [])
)
if not code_status_present:
    criteria.blank_code_status = True
```

This approach is functionally equivalent to a Patient resource extension for the agent's purposes — the blank-code-status auto-flag fires correctly when no LOINC 81638-3 Observation is present. Verified against `synthetic_data/bundles/pt-001.json` — no Patient extensions are present; code status is expressed via Observation.

**Gate result:** Passed. The blank-code-status auto-flag mechanism works against the synthetic dataset. Tests in `agent-api/tests/test_verification.py` verify this behavior.

### 7.2 `isolation` — Status: NOT IMPLEMENTED — BLOCKING

**ARCHITECTURE.md §3.1** lists `isolation` as a hard prerequisite column for `patient_data`. **USERS.md §4** lists isolation status as a required field in the information needs per patient and as an always-shown field in the UC-2 expanded briefing (section 6: "Code status and isolation — always shown, always explicit").

Current state (verified 2026-04-29):

- No `isolation` field or equivalent FHIR Observation exists in `synthetic_data/bundles/pt-001.json` or any inspected bundle.
- No `isolation` handling exists anywhere in `agent-api/` Python source files (grep confirmed zero matches).
- The `briefing/context_builder.py` and `briefing/generator.py` files reference code status but have no isolation extraction.

**Consequence:** Any test that verifies isolation status surfacing passes for the wrong reason — the verification layer cannot flag a missing isolation field if no isolation field is ever populated. This is the exact failure mode ARCHITECTURE.md §3.1 warns about: "blank code status auto-flag tests will silently pass for the wrong reason."

**BLOCK:** Phase 8 Phase 1 (tool schemas) and all subsequent phases that depend on isolation surfacing must not proceed until:

1. Isolation status is added to the synthetic FHIR bundles — either as a Patient resource extension (e.g., `http://openemr.io/fhir/StructureDefinition/isolation-status`) or as a FHIR Observation with an appropriate SNOMED/LOINC code (e.g., SNOMED 409689004 for contact isolation).
2. The FHIR extractor reads the isolation field correctly in `triage/criteria.py` and/or `briefing/context_builder.py`.
3. At least one test in `agent-api/tests/` verifies that a patient with isolation status set has it surfaced in the briefing and triage output, and that a patient without isolation status results in an explicit "isolation status unknown" flag rather than a silent omission.

**This is a Phase 8 Phase 0 finding. It must be resolved before Phase 1 begins.**
