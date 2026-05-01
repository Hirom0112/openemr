# Clinical Co-Pilot — Smoke Test Matrix

**Cross-reference:** `scripts/03-smoke-test.sh`, `scripts/04-verify-cutover-gates.sh`, `docs/AGENT_CONTRACT.md`, `docs/UX_SPEC.md`

One-line mapping of each use case to its expected tool invocation, endpoint, smoke assertion, and required test data.

---

## Required env vars

| Variable | Purpose | Example |
|---|---|---|
| `AGENT_API_URL` | Base URL for the agent-api service | `https://copilot-agent-api-production.up.railway.app` |
| `OPENEMR_BASE_URL` | Base URL for the OpenEMR FHIR endpoint | `https://your-openemr.up.railway.app` |

Both scripts read these from the environment. Neither has a safe default for production — set them explicitly.

---

## Required test data

All smoke assertions target the synthetic dataset loaded by `python3 synthetic_data/load.py`.

| Patient ID | Name | Relevant signals |
|---|---|---|
| `pt-001` | Marcus Webb | qSOFA ≥ 2, critical lactate → triage level 1 (URGENT) |
| `pt-002` | Delia Fontaine | Second census patient for multi-patient assertions |
| `pt-003` | Third patient | Third census patient for handoff/census count assertions |

If `pt-001` is not present in the FHIR server, all UC-1/UC-2/UC-4 assertions will fail. Run `python3 synthetic_data/load.py` to restore the dataset.

---

## Smoke assertion matrix

| Check | Use case | Endpoint | Synthetic trigger / prompt | Expected response field | Pass condition |
|---|---|---|---|---|---|
| 1 | Health | `GET /health` | — | `status` | `"ok"` |
| 2 | FHIR reachable | `GET /apis/default/fhir/metadata` | — | HTTP status | 200 |
| 3 | FHIR auth | `GET /fhir/patient/pt-001` (proxy) | — | `resourceType` | present |
| 4 | **UC-1** triage | `POST /agent/query` | `__census_summary__` | `type` | `"census"`, data.census non-empty |
| 5 | **UC-2** briefing | `POST /agent/query` | `"Brief me on patient pt-001."` | `type` | `"briefing"`, narrative non-empty |
| 6 | **UC-3** record query | `POST /agent/query` | `"What was the last potassium for pt-001?"` | `type` | `"query_answer"` (or `"text"` for not-found) |
| 7 | **UC-4** medication safety | `POST /agent/query` | `"Any medication safety concerns for pt-001?"` | `type` | `"medication_safety"` |
| 8 | **UC-5** handoff | `POST /agent/query` | `"Generate handoff notes for all my patients."` | `type` | `"handoff"` |
| 9 | Rationale (direct) | `POST /agent/triage_rationale/pt-001` | body: `{patient_id, session_id}` | any rationale field | `triage_level`, `label`, `rationale`, or `criteria` present |
| 10 | UC-1 ordering | reuses UC-1 response | — | `data.census[?].triage_level` | pt-001 at level 1 |

---

## Synthetic census trigger

The UI auto-dispatches `__census_summary__` to `POST /agent/query` on session open (UX_SPEC.md §2.2). This is the canonical token — defined in `UX_SPEC.md` and sent by `agent-ui/src/components/ChatSurface.tsx` as `CENSUS_INIT_MESSAGE`. The dispatcher routes this to `get_census_summary`.

Do not use `__census_init__` — that was a drift that has been corrected.

---

## Response type → tool mapping

The dispatcher sets the `type` field in every response. When `metadata.tool_called` is absent (e.g., in production deployments without trace metadata), routing accuracy can be inferred from `type`:

| `type` value | Tool invoked |
|---|---|
| `census` | `get_census_summary` |
| `briefing` | `get_patient_briefing` |
| `query_answer` | `query_patient_records` |
| `medication_safety` | `get_medication_safety` |
| `handoff` | `generate_handoff` |
| `text` | Dispatcher fallback / clarification (not a tool route) |
| `rationale` | `get_triage_rationale` (direct call only, not via dispatcher) |

This mapping is implemented in `04-verify-cutover-gates.sh`'s `resolve_tool()` helper.

---

## Rationale endpoint contract

`POST /agent/triage_rationale/{patient_id}` — direct call, not routed through the dispatcher.

Request body (per `UX_SPEC.md §2.3`):

```json
{
  "patient_id": "<patient_id>",
  "session_id": "<session_id>"
}
```

Response envelope type: `"rationale"` — handled by the `PatientCard` expand renderer, not the main response pipeline.

---

## Running the full suite

```bash
export AGENT_API_URL=https://copilot-agent-api-production.up.railway.app
export OPENEMR_BASE_URL=https://your-openemr.up.railway.app

# Step 1: smoke checks (all 10 assertions)
./scripts/03-smoke-test.sh

# Step 2: cutover gates (routing accuracy, latency, rationale speed, + manual gates)
./scripts/04-verify-cutover-gates.sh
```

Both scripts exit 0 on success. Run step 1 before step 2 — if smoke checks fail, gate verification results are unreliable.
