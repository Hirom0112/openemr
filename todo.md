# Week 2 Execution Plan — PHASES → SLICES

**Operating principle:** every slice ends in something runnable. Phases are hard-tied to deadlines. Anything in W2_ARCHITECTURE.md not in this plan is Phase 8 / cut.

**Test path convention:** all Week 2 Python tests live under `agent-api/tests/` (the existing Week 1 convention). The repo-root `tests/` directory is reserved for OpenEMR's PHP/PHPStan suites and is not used by the agent-api.

## Live status (last updated mid-build)

Legend: ✅ done & committed · ⏳ in flight · ⬜ not started · ⚪ optional / Phase-8 conditional

| Phase | Status | Notes |
|---|---|---|
| **Phase 1** — ingest end-to-end | ✅ | spike + 7 slices, 6 commits, end-to-end on local |
| **Phase 2** — Railway deploy | ⏳ | code deployed; W2 routes live; final `/document/ingest` smoke waiting on OpenEMR PHP rebuild |
| **Phase 3** — LangGraph orchestration | ✅ | 9 slices, supervisor + 4 workers + critic + finalize |
| **Phase 4** — Hybrid RAG + intake + UI | ✅ | 6 slices; pgvector + Cohere fallback + pdf.js viewer |
| **Phase 5** — Eval gate | ✅ | 50 cases · 6 rubrics · baseline.json · diff_baseline · GH workflow · advisory pre-push · **5.8 seeded regression VERIFIED on PR #1 (citation_present 100%→74% → GATE: FAIL)** |
| **Phase 6.1 + 6.2** — W2 metrics + audit dual-target | ✅ | 9 metrics + 6 event types live |
| **Phase 6.3** — re-deploy + smoke against deployed URL | ⏳ | blocked on Phase 2.2 |
| **Phase 7** — demo video + Early Submission | ⬜ | needs green deploy + manual recording (Thu) |
| **Phase 8.1** — cost/latency report | ✅ | empty-cell scaffold; fills from /metrics + vendor consoles |
| **Phase 8.2** — README W1/W2 split + ARCH §5.5 | ✅ | catalog matches code, not spec wishlist |
| **Phase 8.3** — Path A passive ingest | ⚪ | conditional; requires Phases 1–7 green |
| **Phase 8.4** — APScheduler watchdog | ⚪ | conditional |
| **Phase 8.5** — intra-doc + retrieval-vs-record conflict passes | ⚪ | conditional |
| **Phase 8.6** — judge meta-eval baseline | ⚪ | conditional |

## Submission readiness

- [x] Public deploy URL exists (https://copilot-agent-api-production.up.railway.app)
- [ ] `/document/ingest` returns a real LabReport end-to-end (waiting on OpenEMR rebuild)
- [x] Seeded regression hard-fails CI on a feature branch (PR #1)
- [ ] Demo video recorded
- [ ] SUBMISSION.md with deploy URL + video + CI red-screenshot

---

## PHASE 1 — Ingest path, one document type, end-to-end
**Goal:** `POST /document/ingest` accepts a lab PDF, runs OCR + Claude-vision schema-fill, writes a FHIR `DocumentReference`, persists extraction with bbox citations to Postgres, returns typed JSON. Includes `unknown`-class fallback and OCR-confidence soft-warn (demo survivability).
**Target completion:** Tuesday 9pm Central
**Submission checkpoint:** MVP

### SPIKE (FIRST 2 HOURS — riskiest slice de-risked)
**Riskiest slice:** **Schema-fill via Claude vision with bbox grounding** — if Claude can't reliably consume PyMuPDF layout JSON + page image and emit `{value, citations:[{bbox_id, quote_or_value}]}` against a Pydantic v2 schema, the entire architectural anti-hallucination story collapses and Phase 1's deliverable is impossible.

- **Spike name:** Prove the "OCR-region constraint" round-trips end-to-end on one fixture
- **Deliverable:** `agent-api/extractors/spike_lab.py` — single function: `(pdf_bytes) → LabReport`. PyMuPDF layout, hardcoded fixture, Claude vision call, Pydantic strict-mode validate.
- **Verification:** `python -m extractors.spike_lab tests/fixtures/lab_osh_lactate.pdf` prints a valid `LabReport` JSON whose first `LabValue.citations[0].bbox_id` resolves into the layout dict and whose `quote_or_value` is a substring of that bbox's OCR text.
- **Hours:** 2
- **Cut line:** **If the spike isn't green by Monday 1pm**, drop the bbox/citation contract from MVP. Path B still ships, but extractor returns flat JSON without bbox citations and the critic skips fidelity check — soft-warn banner reads "bbox citations unavailable in MVP build". Pick it back up Wednesday morning before LangGraph wrapping.
- **Dependencies:** none

### Slice 1.1 — Wire OCR layer to PyMuPDF
- **Deliverable:** `agent-api/documents/ocr.py` — `extract_layout(pdf_bytes) -> list[LayoutBlock]` with `bbox_id`, `page`, `bbox`, `text`, `ocr_confidence`.
- **Verification:** `pytest agent-api/tests/test_ocr.py::test_layout_extracts_lactate_bbox` — asserts a known fixture page contains a block with `text` containing "4.2" and stable `bbox_id="p1-bNNN"`.
- **Hours:** 1.5
- **Cut line:** none — required path
- **Dependencies:** spike

### Slice 1.2 — Pydantic schemas (LabReport, UnknownDocument, Citation)
- **Deliverable:** `agent-api/extractors/schemas.py` with `LabReport`, `UnknownDocument`, `Citation`, `ExtractionResult` discriminated union (skip `IntakeForm` until Phase 4).
- **Verification:** `pytest agent-api/tests/test_schemas.py` — round-trips fixture JSON; rejects missing-`citations` field with strict-mode error.
- **Hours:** 1
- **Dependencies:** none (parallelizable with 1.1)

### Slice 1.3 — Lab extractor with vision schema-fill + unknown fallback
- **Deliverable:** `agent-api/extractors/lab.py::extract_lab(layout, pdf_pages) -> LabReport | UnknownDocument`. Promotes spike code to a real module. Keyword fast-path classifier inline (LOINC/"LABORATORY"); on fast-path miss, return `UnknownDocument` with the page summary — defer the LLM classifier to Phase 3.
- **Verification:** `pytest agent-api/tests/test_extractor_lab.py` — happy path produces `LabReport` with ≥1 cited value; non-lab fixture (a discharge summary) falls back to `UnknownDocument`.
- **Hours:** 3
- **Cut line:** If by **Tuesday 12pm** vision schema-fill is unstable, cut citations and emit values without bboxes; soft-warn banner says "bbox citations disabled". MVP still ships.
- **Dependencies:** 1.1, 1.2

### Slice 1.4 — Postgres `copilot_doc_extractions` table + writer
- **Deliverable:** `agent-api/documents/store.py` + migration in `agent-api/audit/schema.sql` (or sibling) creating the table with unique constraint on `document_reference_id`. Stub-row INSERT (§4.3) implemented.

  **Idempotency rule:** keyed on `(document_reference_id, content_sha256)`. On re-ingest with matching key, return the cached extraction row without re-running OCR or the vision call. UPSERT on extraction; derived FHIR Observations carrying `derivedFrom` references back to the source `DocumentReference` are updated in place rather than duplicated. The stub-row INSERT (§4.3) acquires the claim before any expensive work runs.
- **Verification:**
  - `pytest agent-api/tests/test_documents_store.py::test_concurrent_claim_returns_one_winner` — 5 concurrent inserts, exactly one returns a row.
  - `pytest agent-api/tests/test_documents_store.py::test_reingest_returns_cached` — ingest the same fixture twice; second call returns identical `extraction_id`, makes zero Anthropic API calls (assert via mock), and produces no duplicate FHIR Observations.
- **Hours:** 2
- **Cut line:** none — concurrency story matters even at MVP because Path A reuses this primitive.
- **Dependencies:** none (parallelizable)

### Slice 1.5 — FHIR `DocumentReference` + `Binary` write
- **Deliverable:** `agent-api/documents/fhir_writer.py::write_document(patient_id, pdf_bytes, mime) -> document_reference_id`. Uses existing `auth.fhir_client`.
- **Verification:** `curl -F file=@tests/fixtures/lab_osh_lactate.pdf -F patient_id=1 ... /document/ingest` and follow up with `GET /apis/default/fhir/DocumentReference?patient=1` — one new entry exists; PDF retrievable via `Binary/{id}`.
- **Hours:** 2
- **Cut line:** **If FHIR Binary write isn't green by Tuesday 4pm**, fall back to local-disk persistence under `/tmp/copilot-docs/{uuid}.pdf` and write a fake `DocumentReference` row to Postgres with the same UUID. This is risk #1 in the architecture's risk register and the documented mitigation is the REST `/api/patient/.../document` fallback — try it second; local disk is the third fallback.
- **Dependencies:** 1.4

### Slice 1.6 — Wire `POST /document/ingest`
- **Deliverable:** route in `agent-api/main.py` (multipart upload, 25MB / 50-page guard, JWT-protected via existing middleware), orchestrating 1.1 → 1.5 sequentially. Returns `{document_reference_id, extraction, citations[], soft_warns[]}`.
- **Verification:** `curl -X POST -F file=@lab.pdf -F patient_id=1 http://localhost:8088/document/ingest` returns 200 with a `LabReport` body; oversized PDF returns 413 with the routing-to-Path-A message.
- **Hours:** 2
- **Dependencies:** 1.3, 1.4, 1.5

### Slice 1.7 — Inline soft-warn for low OCR confidence + unknown fallback
- **Deliverable:** in 1.6's response, surface `soft_warns: ["scan quality low"]` when document-level mean OCR confidence < 0.6; surface `"unknown_document"` when fast-path classifier missed. No critic node yet — these flags ride on the response envelope.
- **Verification:** `pytest agent-api/tests/test_ingest_softwarns.py` — fixture: blurry scan → `soft_warns` includes "scan quality low"; consultant note → response is `kind:"unknown"` with summary present.
- **Hours:** 1
- **Dependencies:** 1.6

**Phase 1 honest sizing:** 14.5h of work to be completed in ~28 working hours Mon–Tue. Tight but feasible **if the spike lands by Monday 1pm**. If it doesn't, take the cut line on 1.3 immediately.

---

## PHASE 2 — First deploy
**Goal:** Phase 1 running on Railway with a public URL. CORS, env vars, upload limits configured. Smoke test from external network.
**Target completion:** Tuesday 11pm Central
**Submission checkpoint:** MVP

### Slice 2.1 — Railway service + `.railwayignore` + Dockerfile sanity
- **Deliverable:** Railway project with `agent-api` service deployed; `OPENEMR_ORIGIN`, FHIR client creds, Anthropic key, `REDIS_URL`, `AUDIT_DB_URL` set; Postgres + Redis add-ons attached.
- **Verification:** `curl https://<railway-url>/health` returns `{"status":"ok","redis":true}`.
- **Hours:** 1.5
- **Cut line:** **If Railway isn't healthy by Tuesday 10:30pm**, deploy via `fly.io launch` instead — same Dockerfile, same env vars. The MVP gate is "publicly reachable URL", not "specifically Railway".
- **Dependencies:** Phase 1 complete

### Slice 2.2 — Smoke `POST /document/ingest` from external network
- **Deliverable:** `scripts/smoke_ingest.sh` that POSTs `tests/fixtures/lab_osh_lactate.pdf` against the public URL using a JWT minted by `JwtMinter.php`.
- **Verification:** `bash scripts/smoke_ingest.sh` returns HTTP 200 with a valid `LabReport`; second run is idempotent (same `document_reference_id`).
- **Hours:** 0.5
- **Dependencies:** 2.1

**MVP gate met when Slice 2.2 is green from a network outside Railway's VPC.**

---

## PHASE 3 — LangGraph orchestration
**Goal:** Supervisor + intake-extractor worker + evidence-retriever worker + structured-data worker + critic + finalize. Dispatcher exposed as the structured-data worker. Logged handoffs via existing observability.
**Target completion:** Wednesday 4pm Central
**Submission checkpoint:** Early

> **Honest pricing:** this phase is **four workers + critic + supervisor + finalize = 7 nodes**, not 2. Budgeting ~15h.

### Slice 3.1 — LangGraph skeleton + state object
- **Deliverable:** `agent-api/graph/state.py` defining `W2State` per §5.10; `agent-api/graph/build.py` building an empty graph: supervisor → noop worker → critic-stub → finalize. Wired to existing Redis checkpointer.
- **Verification:** `pytest agent-api/tests/test_supervisor.py::test_empty_graph_runs` — graph compiles, executes, persists state.
- **Hours:** 2
- **Cut line:** **If LangGraph fights us on streaming/ContextVars by Wed 11am**, fall back to hand-rolled supervisor (~150 lines, risk #5's documented mitigation). All later slices unchanged because they're node-shaped.
- **Dependencies:** Phase 1 complete

### Slice 3.2 — Supervisor routing
- **Deliverable:** `agent-api/graph/supervisor.py` with deterministic routing per §5.2. Emits `node_handoff` audit row + Langfuse span on each decision.
- **Verification:** `pytest agent-api/tests/test_supervisor.py::test_routes_file_to_extractor`, `::test_routes_question_with_facts_to_retriever`, `::test_routes_structured_query_to_dispatcher`.
- **Hours:** 2
- **Dependencies:** 3.1

### Slice 3.3 — Wrap intake-extractor as a worker
- **Deliverable:** `agent-api/graph/nodes/extractor.py` — adapts Phase 1's `extract_lab` into a graph node consuming/producing `W2State`.
- **Verification:** `pytest agent-api/tests/test_extractor_node.py` — given file_bytes_ref + patient_id, populates `state.extraction` and `state.demographic_check`.
- **Hours:** 1
- **Dependencies:** 3.1, Phase 1

### Slice 3.4 — Wrap dispatcher as structured-data worker
- **Deliverable:** `agent-api/graph/nodes/structured.py` — calls existing `dispatch()` and packs result into `W2State`. **Dispatcher source unchanged** (constraint #3).
- **Verification:** `pytest agent-api/tests/test_structured_node.py` — "show me his vitals trend" routes through supervisor → structured node → finalize and returns the same shape `POST /agent/query` does today.
- **Hours:** 1.5
- **Dependencies:** 3.2

### Slice 3.5 — Evidence-retriever worker stub (returns empty)
- **Deliverable:** `agent-api/graph/nodes/retriever.py` returns `state.retrieval = {"snippets": []}`. Real retrieval lands in Phase 4; this slice exists so the supervisor can route to it now and so the critic can be tested with retrieval-shaped state.
- **Verification:** `pytest agent-api/tests/test_retriever_node.py::test_stub_returns_empty_snippets`.
- **Hours:** 0.5
- **Dependencies:** 3.1

### Slice 3.6 — Critic node (hard-block + soft-warn)
- **Deliverable:** `agent-api/graph/nodes/critic.py` implementing §5.8. **Reuses existing `verification/dispatcher_response.py` for the structured-data path** (constraint #3 — preserve W1 verification). For document path: schema-valid, citation-present, citation-resolvable, citation-fidelity (per §10.4 with OCR-confidence gate), demographic-mismatch hard-block stub (real demographic check in 3.7).
- **Verification:** `pytest agent-api/tests/test_critic.py` — fabricated `quote_or_value` not in cited bbox → hard_block; low OCR confidence → soft_warn with fidelity check skipped; clean lab report → pass.
- **Hours:** 3
- **Dependencies:** 3.3, 3.5

### Slice 3.7 — Wrong-patient detection (MRN-dominant rule)
- **Deliverable:** `agent-api/demographics/check.py` implementing the §5.6 table. Plugged in between worker output and critic.
- **Verification:** `pytest agent-api/tests/test_demographics.py` — one parametrized test per row of the §5.6 table.
- **Hours:** 2
- **Dependencies:** 3.6

### Slice 3.8 — Finalize node + SSE streaming
- **Deliverable:** `agent-api/graph/nodes/finalize.py` emits `{event:"node_complete", node, partial}` SSE frames in the format §5.9 specifies. Reuses `_sse_format()` from `main.py`.
- **Verification:** `curl -N -X POST .../agent/w2/dispatch -d '{...}'` shows incremental SSE frames; final frame contains the assembled response.
- **Hours:** 1.5
- **Dependencies:** 3.6

### Slice 3.9 — Wire `POST /agent/w2/dispatch`
- **Deliverable:** route in `main.py`; ingest path also flows through it (Path B request shape).
- **Verification:** end-to-end: `POST /agent/w2/dispatch` with a file → extractor → demographic check → critic → finalize → response with critic_decision and node_handoff audit rows queryable in Postgres.
- **Hours:** 1
- **Dependencies:** 3.7, 3.8

**Phase 3 sizing:** 14.5h, fits Wed 8am–4pm if started fresh. **If by Wed 1pm only the skeleton + supervisor + critic exist**, drop the SSE streaming (3.8 ships as a non-streaming finalize) and merge 3.7 into the critic itself.

---

## PHASE 4 — Hybrid RAG + intake form + UI
**Goal:** pgvector + tsvector indexed corpus, Cohere rerank wired, `POST /evidence/search` working. Intake-form extractor. Bbox overlay on pdf.js with click-to-source.
**Target completion:** Wednesday 6pm (freeze)
**Submission checkpoint:** Early

### Slice 4.1 — `pgvector` enable + `copilot_guideline_chunks` schema
- **Deliverable:** migration adds extension and table per §6.1 with GIN(content_tsv) + IVFFLAT(embedding).
- **Verification:** `psql -c "\d copilot_guideline_chunks"` shows both indexes; `SELECT extname FROM pg_extension WHERE extname='vector'` returns one row.
- **Hours:** 1
- **Dependencies:** none

### Slice 4.2 — Indexing pipeline (offline, one-shot)
- **Deliverable:** `agent-api/rag/index.py` — chunks 5–8 representative guideline PDFs (sepsis, AKI, hypertension subset of §6.4 — **not all 30–60**), embeds via Voyage-3, inserts into table.
- **Verification:** `python -m rag.index agent-api/corpus/` finishes; `SELECT count(*) FROM copilot_guideline_chunks` ≥ 200.
- **Hours:** 2
- **Cut line:** **If indexing isn't green by Wed 3pm**, ship retrieval against just sepsis+AKI (~80 chunks). Demo cases all use sepsis anyway.
- **Dependencies:** 4.1

### Slice 4.3 — Hybrid retrieval + Cohere rerank
- **Deliverable:** `agent-api/rag/retrieve.py::search(query, k=5)` — parallel sparse+dense top-20, merge dedupe, Cohere rerank top-30→top-5; on Cohere failure, fall back to merged-top-8 (§6.2).
- **Verification:** `pytest agent-api/tests/test_rag_retrieve.py` — "lactate sepsis bundle" surfaces an SSC chunk in top-5; Cohere mocked-503 falls back without raising.
- **Hours:** 2
- **Dependencies:** 4.2

### Slice 4.4 — `POST /evidence/search` route + retriever node now real
- **Deliverable:** route in `main.py`; `graph/nodes/retriever.py` now calls `rag.retrieve.search()`.
- **Verification:** `curl -X POST .../evidence/search -d '{"query":"hour-1 sepsis bundle"}'` returns top-5 with `source_id`, `chunk_id`, `relevance_score`.
- **Hours:** 1
- **Dependencies:** 4.3

### Slice 4.5 — Intake-form extractor
- **Deliverable:** `agent-api/extractors/intake.py` + `IntakeForm` in `schemas.py`. Same vision-schema-fill pattern as lab.
- **Verification:** `pytest agent-api/tests/test_extractor_intake.py` — fixture admission form yields `IntakeForm` with code_status + ≥1 cited allergy.
- **Hours:** 2
- **Cut line:** **If by Wed 5pm only lab works**, ship lab + unknown only. Phase 5 eval-mix shifts: cut intake bucket from 10 → 0 cases, redistribute to lab + unknown. Submission still passes.
- **Dependencies:** 1.3

### Slice 4.6 — pdf.js viewer + bbox overlay + citation chips
- **Deliverable:** `agent-ui/src/components/DocumentViewer.tsx` rendering PDF with `pdfjs-dist`; `BboxOverlay.tsx` drawing a canvas overlay; `CitationChip.tsx` cycling through citations on click.
- **Verification:** Browser: open chat, ingest a lab PDF, click the lactate citation chip → PDF opens to page 2 with the lactate bbox highlighted.
- **Hours:** 4
- **Cut line:** **If by Wed 5:30pm the overlay isn't pixel-correct**, ship a static link instead — citation chips open the PDF at the correct page (no bbox highlight). The bbox JSON still ships in the API response; the demo video can show one hand-staged screenshot.
- **Dependencies:** Phase 1 complete

**Phase 4 sizing:** 12h, parallelizable. RAG track (4.1–4.4) and intake (4.5) can run side by side with the UI track (4.6). Tight against the Wed 6pm freeze.

---

## PHASE 5 — Eval gate (THIS IS A TIMED TASK, not an afterthought)
**Goal:** 50 cases in `tests/fixtures/w2_eval_cases.py`, 6 boolean rubrics, `evals/baseline.json` committed, `evals/diff_baseline.py`, pre-push hook, GH Actions extended. **Phase ends with a seeded regression hard-failing CI on a feature branch.**
**Target completion:** Thursday 10am Central
**Submission checkpoint:** Early

### Slice 5.1 — `agent-api/tests/fixtures/w2_eval_cases.py` — first 20 cases
- **Deliverable:** dataclasses with `(case_id, document_path, expected_kind, expected_critic_decision, expected_fields[])`. Bucket: 6 nominal lab, 4 nominal intake, 3 nominal unknown, 2 wrong-patient, 2 blank/noise, 2 low-quality, 1 intra-doc-conflict.
- **Verification:** `pytest agent-api/tests/test_w2_eval.py --collect-only` lists 20 parametrized cases.
- **Hours:** 3
- **Dependencies:** Phase 4 (extractors must produce real outputs to label)

### Slice 5.2 — Mechanical rubrics (`schema_valid`, `citation_present`, `correct_critic_decision`, `no_phi_in_logs`)
- **Deliverable:** `agent-api/evals/rubrics_mechanical.py` — four pure-python checks producing booleans.
- **Verification:** `pytest agent-api/tests/test_rubrics_mechanical.py`.
- **Hours:** 2
- **Dependencies:** 5.1

### Slice 5.3 — LLM rubrics (`factually_consistent` Sonnet, `safe_refusal` Haiku) with auto-rerun
- **Deliverable:** `agent-api/evals/rubrics_llm.py` — strict yes/no prompts; on `factually_consistent` fail, auto-rerun once (§11.6); persist judge response for debug.
- **Verification:** `pytest agent-api/tests/test_rubrics_llm.py -m clinical_accuracy` — fixture with hallucinated lactate value → `factually_consistent=False` on both runs.
- **Hours:** 3
- **Dependencies:** 5.2

### Slice 5.4 — Cases 21–50
- **Deliverable:** remaining 30 cases per §11.1 mix.
- **Verification:** `pytest agent-api/tests/test_w2_eval.py --collect-only | wc -l` ≥ 50.
- **Hours:** 3
- **Cut line:** **If by Thursday 6am only 35 cases exist**, ship 35 with proportionally adjusted bucket counts, document the deviation in `EVAL.md`. The gate's *mechanism* is what's graded, not the count, but 35 is the bare minimum I'd defend in a video.
- **Dependencies:** 5.1

### Slice 5.5 — `evals/baseline.json` + `evals/diff_baseline.py`
- **Deliverable:** baseline pinned per §11.5; script computes per-rubric pass-rate, compares to baseline, exits 1 if any rubric drops >5pp or falls below floor; `no_phi_in_logs` failure exits 1 absolutely.
- **Verification:** `python evals/diff_baseline.py --results eval-results.json --baseline evals/baseline.json` exits 0 on baseline-equal results, 1 on a synthetic 6pp drop.
- **Hours:** 1.5
- **Dependencies:** 5.3, 5.4

### Slice 5.6 — Extend `.github/workflows/copilot-eval.yml`
- **Deliverable:** add `w2-eval` job: runs full 50-case suite, posts `eval-results.json`, runs `diff_baseline.py`, posts a PR comment with the table.
- **Verification:** push a no-op branch — workflow runs, diff job exits 0, PR comment renders.
- **Hours:** 1.5
- **Dependencies:** 5.5

### Slice 5.7 — Pre-push hook (10-case smoke)
- **Deliverable:** `.git/hooks/pre-push` template + `scripts/install_pre_push.sh`; smoke selects one of each bucket via marker `@pytest.mark.smoke`.

  **Pre-push hook is ADVISORY ONLY.** A failing pre-push warns the developer but does not block the push. CI on GitHub Actions is the CANONICAL gate. Only red CI on the PR blocks submission. The hook exists to catch obvious regressions before the 5–10 minute CI wait, not to be the gate itself. The install script prints this distinction on first install.
- **Verification:** `git push --dry-run` triggers the smoke; on a deliberate failure the hook prints a warning but the push still proceeds (hook exits 0 regardless of test outcome).
- **Hours:** 0.5
- **Dependencies:** 5.6

### Slice 5.8 — **SEEDED-REGRESSION VERIFICATION (the actual graded check)**
- **Deliverable:** branch `regression/seed-strip-citations` that mutates `extractors/lab.py` to drop `citations` from one in three `LabValue`s.
- **Verification:** `git push origin regression/seed-strip-citations` — the GH Actions job **must hard-fail** with `citation_present` rubric below floor. If it doesn't fail, the gate is broken; **revert and fix before submission**.
- **Hours:** 1
- **Cut line:** **non-cuttable.** This is the Thursday-morning pass/fail gate. If it doesn't bite by Thursday 10am, freeze everything else and fix it.
- **Dependencies:** 5.6

**Phase 5 sizing:** 15.5h; fits a long Wed-night-into-Thursday-morning push. The cases (5.1, 5.4) are the time-sink — start them in parallel with Phase 3/4 work as soon as fixtures exist.

---

## PHASE 6 — Wednesday 6pm freeze + deployment hardening
**Goal:** No new features after 6pm Wed. Only deploy hardening, bug fixes, observability tightening.
**Target completion:** Wednesday 11pm
**Submission checkpoint:** Early

### Slice 6.1 — Add W2 Prometheus metrics from §10.2
- **Deliverable:** counters/histograms registered in `agent/metrics.py`: `agent_w2_document_ingest_total`, `agent_w2_extraction_duration_seconds`, `agent_w2_critic_decisions_total`, `agent_w2_eval_pass_rate`. Wire emission at the right boundaries.
- **Verification:** `curl http://localhost:8088/metrics | grep agent_w2_` shows the new metrics with non-zero counts after a smoke ingest.
- **Hours:** 1.5
- **Dependencies:** Phase 3

### Slice 6.2 — Audit dual-target for new event types
- **Deliverable:** `node_handoff`, `classifier_verdict`, `demographic_check`, `critic_decision`, `document_extracted` rows written via `audit/writer.py` at the existing emission points in graph nodes.
- **Verification:** `psql -c "SELECT event_type, count(*) FROM copilot_audit_events WHERE event_type LIKE 'document_%' OR event_type IN ('node_handoff','critic_decision') GROUP BY 1"` after a smoke ingest — all five rows present.
- **Hours:** 1
- **Dependencies:** 6.1

### Slice 6.3 — Re-deploy + smoke against deployed URL
- **Deliverable:** Railway redeploy with frozen build; tag git commit `early-submission-frozen`.
- **Verification:** `bash scripts/smoke_ingest.sh` against the public URL still green; `/metrics` reachable; one full `POST /agent/w2/dispatch` from outside the VPC succeeds.
- **Hours:** 1
- **Cut line:** **If smoke fails on the frozen build**, the freeze is lifted only for the bug at hand; a new tag is cut after the fix.
- **Dependencies:** 6.1, 6.2

**Freeze rule:** after Wed 6pm, only Phase 5 + Phase 6 work runs. Anyone touching extractor / supervisor / critic logic is breaking the freeze.

---

## PHASE 7 — Demo video + Early Submission
**Goal:** Demo video recorded against Wed-night frozen build. Submission package complete.
**Target completion:** Thursday 10pm Central (early-submission deadline 11:59pm)
**Submission checkpoint:** Early

### Slice 7.1 — Demo script + 3-take recording
- **Deliverable:** `docs/demo_script.md` covering: login → census brief shows extracted lactate from OSH fax → click citation → PDF + bbox highlight → ask "is this consistent with sepsis criteria?" → retrieved SSC chunk surfaces → seeded regression PR shows red CI.
- **Verification:** video file renders end-to-end <4 minutes; every claim made in the video is reproducible from the deployed URL.
- **Hours:** 2
- **Cut line:** **If the bbox overlay looks bad on screen recording**, narrate over a still frame for that beat — the rest of the demo doesn't depend on overlay polish.
- **Dependencies:** Phase 6 frozen build

### Slice 7.2 — Submission package
- **Deliverable:** `SUBMISSION.md` linking deployed URL, video, GH Actions CI screenshot of the seeded-regression failure, EVAL.md update.
- **Verification:** every link clicked from a fresh tab works.
- **Hours:** 1
- **Dependencies:** 5.8, 7.1

---

## PHASE 8 — Final hardening
**Goal:** Cost/latency report, README W1/W2 split, audit catalog updates, judge meta-eval baseline, interview readiness.
**Target completion:** Sunday 10am Central
**Submission checkpoint:** Final

Anything in W2_ARCHITECTURE.md not yet shipped — Path A passive ingest, watchdog (APScheduler), retrieval-vs-record contradiction pass, intra-doc conflict pass, third doc type, honest-degradation halts, manual reclassify, document-processing UX timeline — is evaluated for inclusion **only if Phases 1–7 are green**.

### Slice 8.1 — Cost/latency report from deployed metrics
- Deliverable: `docs/COST_LATENCY.md` populated from §10.4 tables.
- Verification: every cell has a number sourced from `/metrics` or vendor console.
- Hours: 2

### Slice 8.2 — README W1/W2 split + ARCHITECTURE.md §5.5 metric updates
- Deliverable: README split, metric/event tables updated.
- Verification: `grep -r agent_w2_ ARCHITECTURE.md` matches Slice 6.1's actual metrics.
- Hours: 2

### Slice 8.3 — Path A (passive scan_unprocessed) — **conditional**
- Run only if 8.1 + 8.2 green by Friday noon. Brings the "morning brief sees the messy half of the chart" claim from the one-sentence defense to life.
- Hours: 4

### Slice 8.4 — Watchdog + APScheduler — **conditional**
- Run only if 8.3 green by Saturday noon. Otherwise the failure mode (stuck rows) is rare enough on synthetic data that the gap is defensible.
- Hours: 3

### Slice 8.5 — Intra-doc + retrieval-vs-record conflict passes — **conditional**
- Already covered as soft-warns in the eval mix (case bucket 8 — intra-doc-conflict). If the rubric `correct_critic_decision` is passing without the dedicated detector, ship the detector to make the *implementation* match the architecture; if it's failing, this is a Phase 8 must-have.
- Hours: 3

### Slice 8.6 — Adversarial sweep + judge meta-eval baseline
- Hours: 3

---

## HONESTY CHECK

The three slices most likely to slip, why, and what gets cut if they do:

### 1. **Slice 1.3 (lab extractor with vision schema-fill)** — likeliest single slip
**Why:** Claude vision producing schema-conformant JSON with mechanically-resolvable bbox citations is the spike's hard part. PyMuPDF emits dozens to hundreds of small text regions per page; convincing the model to (a) pick the right one and (b) faithfully copy the literal text into `quote_or_value` rather than paraphrasing is prompt-engineering work that doesn't always land in one sitting. There's also a real chance the model returns `bbox_id`s it didn't see — at which point the critic blocks every response and Phase 1's deliverable is unviable.
**If it slips:** take the 1.3 cut line — ship MVP with values but no bboxes (a flat `LabReport` with citations field empty), banner says "bbox citations disabled in MVP build". Phase 4.6 (UI overlay) becomes a static page-jump-only build instead of canvas overlay. The seeded regression in 5.8 then targets `value`-fidelity instead of `citation_present`, which is fine — `factually_consistent` still bites.

### 2. **Phase 3 in one day** — the LangGraph wrap-around is undersized
**Why:** Seven nodes, real audit emission at every edge, real ContextVar propagation through async tasks, and SSE streaming integrated with FastAPI is closer to two days of careful work than one. LangGraph + Anthropic + Redis-checkpointer + audit-middleware is a four-way integration risk, exactly risk #5 in the architecture. The hand-rolled supervisor fallback exists for a reason and I don't trust the one-day estimate.
**If it slips:** by Wed 1pm, drop LangGraph and ship the hand-rolled supervisor (~150 lines: a function that switch-cases on request shape and calls workers as plain async functions). All node interfaces stay the same; the critic, demographics, finalize, and SSE all still work. **The architecture's user-visible behavior doesn't change** — only the internal implementation differs from the doc, which is acceptable because the doc names this as the documented contingency. Cut intra-doc-conflict and retrieval-vs-record contradiction passes (they were Phase 8 anyway).

### 3. **Slice 5.8 (seeded-regression fails CI hard)** — the gate-of-the-gate
**Why:** This is the only Thursday-morning pass/fail item that depends on the union of every Phase 5 artifact working together: cases collected, rubrics computed, baseline diffed, GH Actions wired, and the regression actually moving a rubric below threshold. Any one of those misconfigured and the seeded regression sails through green — at which point the whole "eval-gated" architectural claim is performative rather than real. CI debug loops are slow because each iteration is a `git push` + 5–10 minute Actions wait.
**If it slips:** **non-negotiable cut order.** First, narrow the regression to be more aggressive (drop citations from *all* `LabValue`s instead of one-third) so even a sloppy rubric will catch it. If the gate still doesn't fail by Thursday 10am, the cut is to **demote `factually_consistent` and `safe_refusal` to advisory-only** (per §14's degradation principle, this is a documented behavior not a hack) and rely on the four mechanical rubrics (`schema_valid`, `citation_present`, `correct_critic_decision`, `no_phi_in_logs`) — all four of which can hard-fail mechanically without LLM-judge variance. The video then narrates "LLM rubrics ride alongside the gate at advisory-only until meta-eval credibility is established", which matches §11.7 / §14 behavior verbatim.

---

**Bottom line:** the plan ships MVP Tuesday with a real `POST /document/ingest` against one fixture, rides Phase 3+4 hard on Wednesday into the 6pm freeze, owns Phase 5 as a Thursday-morning timed deliverable with a seeded-regression sign-off, then uses Friday–Sunday only to pay back architectural debt. Every cut line has a clock and a fallback; nothing in Phases 1–7 silently overpacks.
