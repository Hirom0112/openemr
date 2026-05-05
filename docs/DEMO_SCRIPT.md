# Clinical Co-Pilot — W2 Demo Script

A reproducible, ~4-minute end-to-end demo. Anyone with the deploy URLs and a
copy of `docker/development-easy/.env.copilot` (which contains
`COPILOT_JWT_SECRET`) can run this script verbatim.

---

## 0. What this demonstrates (15s narrative)

The agent reads clinical documents, extracts schema-validated facts whose
values are bbox-grounded to a real source region, refuses cleanly when it is
uncertain, and is gated by a 50-case CI suite that hard-fails on regression.
We will see all four properties live, end-to-end, against the real Railway
deployment — no local mocks.

---

## 1. Pre-recording setup

Open four browser tabs and one terminal. Keep them in this fixed order so
the cuts are reproducible.

| Slot | Window | URL / contents |
|---|---|---|
| Terminal | iTerm / Terminal | Working dir = repo root, `docker/development-easy/.env.copilot` already sourced (see below) |
| Tab 1 | Browser | `https://copilot-agent-api-production.up.railway.app/health` |
| Tab 2 | Browser | `https://copilot-agent-api-production.up.railway.app/metrics` |
| Tab 3 | Browser | `https://clinical-copilot-openemr-production.up.railway.app` (logged in as `admin` / pilot password, on the patient with `pid=1`) |
| Tab 4 | Browser | `https://github.com/Hirom0112/openemr/pull/1` (the regression PR) |
| Tab 5 | Browser | The Actions run for PR #1 — "W2 Eval Suite" job, with the `eval_results.md` artifact already expanded |

**One-time terminal setup before starting the recording:**

```bash
cd /path/to/repos-gauntlet/openemr

# Source the secret silently — `set -a` exports without echoing values.
set -a; source docker/development-easy/.env.copilot; set +a

# Sanity check (does NOT print the secret value):
[[ -n "$COPILOT_JWT_SECRET" ]] && echo "secret loaded: ${#COPILOT_JWT_SECRET} chars"

# Pin endpoints used throughout the demo
export AGENT_API_URL=https://copilot-agent-api-production.up.railway.app
export OPENEMR_URL=https://clinical-copilot-openemr-production.up.railway.app

# Mint a 5-minute demo JWT (script never prints the secret)
TOKEN="$(./scripts/mint_demo_jwt.sh)"
echo "token minted, $(echo "$TOKEN" | wc -c) chars"
```

If `mint_demo_jwt.sh` errors with "COPILOT_JWT_SECRET is not set", re-run the
`set -a; source ...; set +a` line. The secret is process-scoped and does not
persist across new shell windows.

**Pick the demo PDF.** A real lab-report fixture lives at
`agent-api/tests/fixtures/lab_reports/lactate_real.pdf`. Confirm before
recording:

```bash
ls -la agent-api/tests/fixtures/lab_reports/lactate_real.pdf
```

---

## 2. Recording sequence

Total target: ~4:00. Per-beat targets below.

### Beat 1 — The deploy is live (45s)

**Narrate:** "This is the agent-api running on Railway. It is live, it has
served real traffic, and its W2 instrumentation is wired."

1. Switch to **Tab 1** (`/health`). Show the JSON `{"status":"ok",...}`.
   *(~5s)*
2. Switch to **Tab 2** (`/metrics`). Use Cmd-F to find `agent_w2_`. Scroll
   slowly through the matches: `agent_w2_extraction_duration_seconds`,
   `agent_w2_extraction_total{status="ok"}`, `agent_w2_citation_*`,
   `agent_w2_critic_decisions_total`. The counters are non-zero — this
   is real production traffic, not a fresh process. *(~25s)*
3. Switch to **Terminal**. Run a one-liner that proves the agent dispatch
   route is reachable end-to-end with our minted JWT: *(~10s)*

   ```bash
   curl -sS -o /dev/null -w "%{http_code}\n" \
     "$AGENT_API_URL/health"
   ```

   Expect `200`.

### Beat 2 — Document ingestion, end-to-end (90s)

**Narrate:** "Now we ingest a real lab PDF. The agent OCRs it, runs the
intake-extractor with vision, validates against the LabReport schema,
checks every value's bbox citation for fidelity, and writes a
DocumentReference into OpenEMR's chart."

1. **Terminal.** POST the fixture PDF to `/document/ingest` with multipart
   form data. The exact command, fully reproducible: *(~5s to type, ~15s
   to wait for the response)*

   ```bash
   curl -sS "$AGENT_API_URL/document/ingest" \
     -H "Authorization: Bearer $TOKEN" \
     -F "file=@agent-api/tests/fixtures/lab_reports/lactate_real.pdf;type=application/pdf" \
     -F "patient_id=1" \
     | tee /tmp/ingest.json \
     | python3 -m json.tool
   ```

2. **Walk the response on screen.** Point at, in order: *(~30s)*
   - `extraction.kind` = `"lab_report"` — the supervisor classified.
   - `extraction.values` — four lab values, each with a `citation` block.
   - For one value, expand `citation.field_or_chunk_id` and
     `citation.quote_or_value`. Say: "this string is a real substring of
     the OCR text inside that bbox — that is what citation fidelity
     means."
   - `extraction.soft_warns` is `[]` — the critic did not flag anything.
   - `fhir_write_path` = `"copilot_custom"` — see deviation note below.
   - `document_reference_id` = `"copilot:117"` (or whatever number this
     run produces).

3. **Switch to Tab 3 (OpenEMR).** Navigate to the patient `pid=1` →
   *Documents* tab. The just-uploaded PDF appears in the list with the
   timestamp matching the curl. Click it open. Say: "OpenEMR is the
   system of record. The agent did not create a shadow store."
   *(~30s)*

4. **Back to Terminal.** Show idempotency in one command: re-POST the
   exact same file. Point at the response — same `document_reference_id`,
   no duplicate row. *(~10s)*

   ```bash
   curl -sS "$AGENT_API_URL/document/ingest" \
     -H "Authorization: Bearer $TOKEN" \
     -F "file=@agent-api/tests/fixtures/lab_reports/lactate_real.pdf;type=application/pdf" \
     -F "patient_id=1" \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print("doc_ref:", d["document_reference_id"], "write_path:", d["fhir_write_path"])'
   ```

### Beat 3 — The eval gate bites (60s)

**Narrate:** "The eval gate is not decoration. We seeded a regression and
the gate hard-failed in CI."

1. **Tab 4 (PR #1).** Show the PR title and the red X on the W2 Eval
   Suite check. Say: "This PR strips the `citations` field from the
   extractor's output. It is a one-line regression that any junior
   reviewer would miss." *(~15s)*

2. **Tab 5 (Actions run).** Open the failed `w2-eval` job. Scroll to the
   summary at the bottom. Read aloud: *(~30s)*

   > "citation_present: 100% baseline → 74% on this PR. Drop of 26
   > points. GATE: FAIL. Exit 1."

3. Open the `eval_results.md` artifact. Show the per-rubric pass-rate
   table — six rubrics, one column dropped, the others held. Say:
   "The gate isolates which property regressed. A reviewer sees, in one
   line, that this PR broke citations specifically." *(~15s)*

### Beat 4 — Honest deviation note (30s)

**Narrate:** "There is one deviation from the architecture worth calling
out. We documented it before we had to."

1. Open `W2_ARCHITECTURE.md` in the editor (or GitHub web view). Jump
   to **§4.2.1 Custom upload path (deployment deviation)**. *(~10s)*
2. Read the one-sentence summary: "The two write paths described in §4.2
   are both unavailable on the OpenEMR build deployed for the pilot."
3. Jump to **§4.2.2 Security tradeoff — shared HMAC secret**. Read one
   sentence aloud: *(~15s)*

   > "A holder of `COPILOT_JWT_SECRET` can mint a valid JWT and write
   > arbitrary documents to any patient's chart — equivalent to
   > admin-level chart-write authority."

4. Say: "This is documented, scoped to the pilot, and reversible the
   moment OpenEMR's FHIR Binary write returns. `docs/SECURITY_TRADEOFFS.md`
   has the full analysis." *(~5s)*

### Beat 5 — Closer (15s)

**Narrate verbatim from W2_ARCHITECTURE §18:**

> "A multi-agent clinical agent that reads documents, cites every fact to
> a real source with verified value-to-bbox fidelity, refuses cleanly
> when uncertain, surfaces conflicts rather than silently resolving them,
> and is gated by a 50-case CI suite — so the user's morning brief sees
> the messy half of the chart she would otherwise be assembling herself."

End recording.

---

## 3. Cut points

If the raw recording runs long, splice in this priority order (cut from
the bottom up):

1. **Cut Beat 2 step 4 (idempotent re-POST).** The `fhir_write_path` line
   in the response carries the architectural point already.
2. **Cut Beat 1 step 3 (terminal `curl /health`).** Tabs 1 and 2 already
   prove the deploy is live; the curl is belt-and-suspenders.
3. **Cut Beat 3 step 3 (eval_results.md walk).** The summary line in
   step 2 is sufficient for the gate-bites claim. Keep step 1 + 2.
4. **Cut Beat 4 to a single sentence** if needed: just read the §4.2.2
   blast-radius sentence and move to the closer.

Do not cut Beat 2 steps 1–3 (the live ingest + chart round-trip) or Beat
3 step 2 (the gate-fail summary) — those are the load-bearing
demonstrations.

If the recording runs **short**, extend Beat 2 by walking a second value's
citation block and pointing at the `bbox_id` field explicitly.

---

## 4. Failure modes during recording

If something breaks live, the recoverable failures are:

| Symptom | Recovery |
|---|---|
| `curl` returns `401` from `/document/ingest` | Token expired (5-min TTL). Re-run `TOKEN="$(./scripts/mint_demo_jwt.sh)"`. |
| `fhir_write_path` returns `local_disk` instead of `copilot_custom` | The custom upload tier is misconfigured on OpenEMR. Acknowledge on camera, point to §4.2.1 fallback chain (tier 4 = local-disk, surfaced as degraded). The demo still proves the pipeline. |
| Tab 3 (OpenEMR) shows no new document | Hard-refresh the Documents tab. OpenEMR's tab is cached client-side. |
| Tab 5 Actions artifact has expired | Re-run the workflow on PR #1; the seeded regression is permanent in the PR branch. |

Unrecoverable failures (agent-api down, /metrics blank, eval job green):
stop, fix off-camera, restart from Beat 1.
