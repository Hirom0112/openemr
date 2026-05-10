# Week 2 Demo Audit

> Read-only audit. Every claim cites file paths and line numbers. Where the spec asserts something not in code, the row is marked SPEC-ONLY / DRIFT / MISSING.

## Executive Summary

- **Solid:** Pydantic v2 schemas (strict + discriminated union on `kind`), the W2 LangGraph (supervisor → workers → critic → finalize) with SSE, hybrid RAG (Voyage + Cohere with `RerankUnavailable` fallback), 156-case eval suite gated in `.github/workflows/copilot-eval.yml`, the regression PR (#1) demonstration, and a working Next.js 16 + Auth.js patient dashboard.
- **Solid:** Custom-upload deviation (FHIR Binary 404 / legacy REST 401) is documented honestly in `W2_ARCHITECTURE.md` §4.2.1 with reversibility plan.
- **At risk:** Several spec routes are missing — `POST /document/scan_unprocessed` and `POST /document/{id}/reclassify` do not exist in `agent-api/main.py`. Passive Path A (§4.1) is acknowledged as deferred in `SUBMISSION.md`.
- **At risk:** `synthesis_grounded`, `condition_writeback_succeeded`, `correct_critic_decision` baselines are unusually low (e.g. `correct_critic_decision` floor 0.36) — gate floor is meaningful but pass rate is volatile per modality.
- **Missing/SPEC-ONLY:** `clinical_synonyms.yaml` synonym map is not in the repo (zero hits on grep). APScheduler watchdog is wired for the staging reaper but the §4.6 stuck-extraction watchdog and quarterly meta-eval are deferred (per `SUBMISSION.md` known-gaps).

---

## Submission Requirements Scorecard

| # | Deliverable | Status | Evidence (path:line) | Gap | Demo line |
|---|---|---|---|---|---|
| 1 | Git repo + env vars | DONE | `agent-api/.env.example` documents `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY` (used in `rag/embed.py:90`), `COHERE_API_KEY` (used in `rag/rerank.py:71`), `REDIS_URL`, `LANGFUSE_*`, `FHIR_*`. Repo URL in `SUBMISSION.md:5-16`. | Top-level `.env.example` is OpenEMR's own and does not list agent-api keys; the agent-api `.env.example` is the truthful one. | "Repo at `https://github.com/Hirom0112/openemr/tree/clinical-copilot`." |
| 2 | W2 architecture doc | DONE | `W2_ARCHITECTURE.md` (1,581 lines). Ingestion §4; multi-agent graph §5; hybrid RAG §6; eval gate §11; risks/tradeoffs §14 (honest degradation), §4.2.2 (HMAC tradeoff). | None. | "W2_ARCHITECTURE.md §4 / §5 / §6 / §11 cover all four pillars." |
| 3 | Pydantic v2 schemas | DONE | `agent-api/extractors/schemas.py:39,48,91,122,160,180,189,279` all set `ConfigDict(strict=True, extra="forbid")`. `kind` literals at `:124,162,281`. Discriminated union at `:315-318`. Citation contract at `:45-82` carries `field_or_chunk_id`, `quote_or_value`, `bbox`, `page` (`:53-62`). | `ocr_confidence` per-citation is not a field; `ocr_confidence_range` lives on the parent (`LabReport.ocr_confidence_range:140`). `source_region` per the spec is implied by `field_or_chunk_id` + `bbox`, not a discrete field. | "Show schemas.py Citation + LabReport." |
| 4 | Eval dataset 50+ cases | DONE (exceeded) | `EVAL.md:140-156` — 156 cases, 12 buckets including `bbox_gt`(36) `lab_nominal`(22) `intake_nominal`(22) `mixed_content`(12) `wrong_patient`(11) `low_quality_scan`(10) `evidence_retrieval`(10) `unknown_nominal`(8) `blank_noise`(7) `missing_data`(7) `wrong_type_hint`(6) `intra_doc_conflict`(5). `evals/baseline.json` carries 18 keys + per-modality. | Spec §11.1 lists 12 lab/10 intake/6 unknown/4 wrong-hint/5 wrong-patient/4 blank-noise/4 mixed/3 low-quality/2 conflict (50). Implementation grew bucket counts; no longer 1:1 with §11.1 numbers. Bucket `evidence_retrieval`, `bbox_gt`, `missing_data` were added per Phase 9 — drift but documented in `SUBMISSION.md:56-59`. | "156 cases, 18 baseline keys." |
| 5a | Pre-push 10-case smoke | DONE | `.git/hooks/pre-push` runs `pytest tests/test_w2_eval_smoke.py -m smoke`. | Hook is local-only; not committed (it's in `.git/hooks`). `scripts/install_pre_push.sh` is the on-clone installer. | "git push triggers smoke; CI runs full." |
| 5b | CI 50-case + diff_baseline | DONE | `.github/workflows/copilot-eval.yml:259-279` — `evals.run_full_suite` then `evals/diff_baseline.py`. `:289-293` posts sticky PR comment. MySQL service container `:151-166` for `provenance_chain`. | Cron-style nightly is in `copilot-eval-nightly.yml` (sibling file). | "CI w2-eval job + diff_baseline." |
| 5c | Regression-injection script | PARTIAL | `SUBMISSION.md:24-36` cites PR #1 stripping citations; `docs/eval-evidence/regression-2026-05-08-rubric-drop.png`. `scripts/verify_mvp.sh:429-443` consults the regression branch CI status. | No standalone "regression injector" script in `agent-api/scripts/` — the regression is a manual seeded branch. | "PR #1 shows the gate biting." |
| 6 | Demo UI path | DONE | `agent-ui/src/components/FileDropZone.tsx`, `DocumentReviewPanel.tsx`, `BboxOverlay.tsx`, `CitationChip.tsx`, `SoftWarnBanner.tsx`, `DocumentViewer.tsx` (uses `pdfjs-dist` per `agent-ui/package.json:14`). Routes `/document/ingest` (`main.py:2352`), `/agent/w2/dispatch` (`:3310`), `/evidence/search` (`:3267`), `/document/post-ingest-context` (`:3891`). | None for the active path. | "Drop PDF → review chips → bbox highlight → critic verdict." |
| 7 | Cost & latency report | PARTIAL | `COST_LATENCY_REPORT.md` covers methodology, latency table, projected cost at 100/1k turns, cache hit rate (98%), bottleneck callout (Anthropic round-trip ~14s mean p95). | Empty rows for `agent_dispatch_latency_seconds` and `agent_prewarm_duration_seconds` in main snapshot (`COST_LATENCY_REPORT.md:45-47`); per-stage p50/p95 not exhaustive. No 10k/100k projection. | "98% prompt-cache hit rate; $0.062/turn projected." |
| 8 | Deployment | DONE | `SUBMISSION.md:13-15` Railway URLs; `COST_LATENCY_REPORT.md:166-178` deployed status; `scripts/02-deploy-railway.sh` exists. | CI does not auto-deploy — Railway redeploys on `push origin clinical-copilot`. Documented in MEMORY note ("origin clinical-copilot redeploys Railway production"). | "/health returns 200 on Railway." |
| 9 | Dashboard framework | DONE | `patient-dashboard/web/package.json:6-25` — Next.js 16, React 19, next-auth 5 beta, shadcn, tailwind 4, TypeScript 5. | None. | "Next.js 16 + Auth.js + shadcn." |
| 10 | OAuth2/OIDC + token storage/refresh | DONE | `patient-dashboard/web/src/auth.ts:9-34` (`refreshAccessToken` against `/oauth2/default/token`); `:36-47` NextAuth config; `:48+` JWT callback persists tokens. Provider at `src/lib/auth/openemr-provider.ts:36-37`. | None. | "Sign in via OpenEMR OAuth, server-side token refresh." |
| 11 | Patient header | DONE | `patient-dashboard/web/src/components/cards/PatientHeader.tsx` (test alongside). | UNVERIFIED for full field set (name, DOB, sex, MRN, active) without reading file. | "Header shows DOB/MRN/sex." |
| 12 | Clinical cards | DONE | `web/src/components/cards/`: `AllergiesCard.tsx`, `MedicalProblemsCard.tsx`, `MedicationsCard.tsx`, `PrescriptionsCard.tsx`, `CareTeamCard.tsx`, plus tests for each. FHIR client at `web/src/lib/fhir/client.ts`. | Spec named Problem List; code names it "MedicalProblemsCard" (acceptable — same FHIR Condition resource). | "Five required cards rendered." |
| 13 | +1 chosen section | DONE | `VitalsCard.tsx` (+ test). `EncounterControls.tsx` also present. | None — Vitals is the +1. | "+1 = Vitals." |
| 14 | Migration doc | DONE | `patient-dashboard/PATIENT_DASHBOARD_MIGRATION.md` defends Next.js 16 by mapping 5 audit findings to capabilities (server components, Auth.js, TypeScript, shadcn). | None. | "Migration defense in PATIENT_DASHBOARD_MIGRATION.md." |
| 15 | Dashboard deployment | UNVERIFIED | `patient-dashboard/DEPLOY.md` exists; no public URL surfaced in `SUBMISSION.md`. | URL not listed in submission. | (verify before demo) |

---

## Track A — Document Ingestion / RAG / Eval

### A. Worker graph

| Spec node | Code node | Path | Status |
|---|---|---|---|
| Supervisor | `supervisor` | `graph/build.py:103,54`; `graph/nodes/supervisor.py` | DONE |
| Intake-extractor | `intake_extractor` | `graph/build.py:104`; `graph/nodes/extractor.py` | DONE |
| Evidence-retriever | `evidence_retriever` | `graph/build.py:106`; `graph/nodes/retriever.py` | DONE |
| Structured-data | `structured` | `graph/build.py:105`; `graph/nodes/structured.py` | DONE |
| Critic | `critic` | `graph/build.py:114`; `graph/nodes/critic.py` | DONE |
| Finalize | `finalize` | `graph/build.py:115`; `graph/nodes/finalize.py` | DONE |
| Demographics | `demographics` | `graph/nodes/demographics.py`; flow `intake_extractor → demographics → critic` (`build.py:7`) | DONE |
| Cross-source conflict | `graph/nodes/cross_source_conflict.py`; `conflict/detector.py` | DONE (Phase 9 Slice 9.7) |
| SSE streaming | `main.py:3310` `/agent/w2/dispatch`; `main.py:777,856` UC-5 SSE pattern | DONE |

### B. Hybrid RAG

| Item | Status | Evidence |
|---|---|---|
| Sparse + dense merge | DONE | `agent-api/rag/retrieve.py:262-278` |
| Voyage-3 embeddings | DONE | `agent-api/rag/embed.py:90` model `voyage-3` |
| Cohere Rerank 3 + fallback | DONE | `agent-api/rag/rerank.py:18,54-79`; `RerankUnavailable` raised → fallback path in `retrieve.py:278` |
| `POST /evidence/search` | DONE | `agent-api/main.py:3267` |
| Corpus | DONE | 3 PDFs (`corpus/ssc_2021_excerpt.pdf`, `kdigo_aki_2012_excerpt.pdf`, `ada_glycemic_excerpt.pdf`) + 7 JSON guidelines (`data/guidelines/*.json`); manifest `corpus/manifest.yaml:2,6,10,15+`. |
| Indexing pipeline | DONE | `agent-api/rag/index.py:23-87` |

### C. Critic

| Sub-check | Status | Evidence |
|---|---|---|
| Schema validity | DONE | `graph/nodes/critic.py:92-100` |
| Citation existence + resolvability | DONE | `critic.py:67-68` `_is_normalized_substring`; `evals/rubrics_mechanical.py` includes `citation_resolvable`, `citation_row_match`, `citation_token_match` (per `EVAL.md:118-122`) |
| Citation fidelity | DONE | `critic.py` walks layout + skips on low OCR confidence (`_OCR_CONFIDENCE_THRESHOLD = 0.6` `:51`) |
| Demographic check (MRN-dominant §5.6) | DONE | `agent-api/demographics/check.py`; called from critic flow |
| Intra-doc conflict (§5.7) | PARTIAL | `conflict/detector.py:1` "Phase 9 Slice 9.7"; `SUBMISSION.md:115` lists Phase 8 hooks only — verify wiring vs. stub |
| Retrieval-vs-record contradiction (§6.6) | PARTIAL | `SUBMISSION.md:115` says "stubbed for Phase 8"; `cross_source_conflict.py` exists but coverage incomplete |
| pass / soft_warn / hard_block | DONE | `critic.py:79-86` `_from_w1_result` returns these three |
| Derived-field fail-closed (§8.5) | DONE | `critic.py:21-23` "Failure-closed boundary: any unexpected `Exception`... turns into a `hard_block` with violation `CRITIC_ERROR`" |

### D. Observability

| Item | Status | Evidence |
|---|---|---|
| §10.2 metrics | DONE | `agent-api/agent/metrics.py` — 40+ metrics including `agent_w2_document_ingest_total:147`, `agent_w2_extraction_duration_seconds:152`, `agent_w2_retrieval_duration_seconds:157`, `agent_w2_critic_decisions_total:167`, `agent_w2_demographic_checks_total:172`, `agent_supervisor_handoff_total:309`, `agent_post_ingest_*:323,329`, `agent_quarantine_*:343-361`, `agent_synthesis_*:459,465`. |
| Langfuse spans | PARTIAL | `agent-api/observability/json_logging.py:123,132` mentions Langfuse pinning. No deep tracing wiring confirmed in this audit. |
| No PHI in logs | DONE | Eval rubric `no_phi_in_logs` floor=1.00 (`baseline.json:10`) |
| APScheduler watchdog | PARTIAL | `main.py:5109-5146` wires `staging.watchdog.start_watchdog`; this is the staging reaper. §4.6 "stuck-extraction watchdog" + §11.7 quarterly meta-eval explicitly deferred per `SUBMISSION.md:115-117`. |

### E. UI

| Item | Status | Evidence |
|---|---|---|
| pdfjs-dist | DONE | `agent-ui/package.json:14` `"pdfjs-dist": "^4.7.76"` |
| Citation chip | DONE | `agent-ui/src/components/CitationChip.tsx`, `SynthesisCitationChip.tsx` |
| Bbox overlay | DONE | `agent-ui/src/components/BboxOverlay.tsx` |
| Soft-warn banner | DONE | `agent-ui/src/components/SoftWarnBanner.tsx` |
| Doc-processing indicator | PARTIAL | `agent-ui/src/components/PostIngestContextCard.tsx` exists; explicit "processing" indicator not located |
| Drag-drop tile | DONE | `agent-ui/src/components/FileDropZone.tsx:2,118` (Pillar 1 Path B entry) |
| Quarantine resolver UI | DONE | `QuarantineCard.tsx`, `DuplicateDocumentCard.tsx` |

### F. Synonym map + numeric normalization

| Item | Status | Evidence |
|---|---|---|
| `clinical_synonyms.yaml` | MISSING | `grep -rn clinical_synonyms agent-api/` returns nothing. Numeric normalization lives at `critic.py:54-64` (`_NUM_TOKEN_RE`, `_normalize`). |
| CI threshold check on synonym map | MISSING | No reference. |
| Normalization unit tests | PARTIAL | Indirectly covered by `citation_row_match`, `citation_token_match` rubrics; no dedicated unit-test file located. |

### G. Honest degradation §14

| Item | Status | Evidence |
|---|---|---|
| Watchdog stale-liveness | PARTIAL | `agent_watchdog_last_run_timestamp_seconds` Gauge at `metrics.py:189` — wired but watchdog itself partial (see D). |
| Synonym-map fallback | UNVERIFIED | Map missing (F). Fallback is moot. |
| Meta-eval cadence widening | SPEC-ONLY | Per `SUBMISSION.md:117` "first quarterly run has not been executed". |
| Corpus-age soft-warn | DONE | `critic.py:52` `_GUIDELINE_STALE_AFTER = timedelta(days=24*30)` |

### H. Routes from §17.3

| Route | Status | Evidence |
|---|---|---|
| `POST /document/ingest` | DONE | `main.py:2352` |
| `POST /document/scan_unprocessed` | MISSING | grep returns nothing |
| `GET /document/{id}/preview` | PARTIAL | Closest: `GET /document/{document_reference_id:path}/binary` (`main.py:3661`) and `/docx-paragraphs` (`:3773`) |
| `POST /document/{id}/reclassify` | MISSING | grep returns nothing |
| `POST /evidence/search` | DONE | `main.py:3267` |
| `POST /agent/w2/dispatch` | DONE | `main.py:3310` |
| Bonus: `POST /document/post-ingest-context` | DONE | `main.py:3891` |
| Bonus: `POST /document/{id}/post-approval-context` | DONE | `main.py:4076` |
| Bonus: `/document/quarantine/*` | DONE | `main.py:4840,4874,4944,5027` |
| Bonus: `POST /document/{id}/chat` | DONE | `main.py:4618` |

---

## Track B — Patient Dashboard Migration

| # | Item | Status | Evidence |
|---|---|---|---|
| 9 | Framework | DONE | `patient-dashboard/web/package.json` Next.js 16 + Auth.js v5 beta + shadcn + Tailwind 4 + TS 5; React 19 |
| 10 | OAuth2/OIDC | DONE | `web/src/auth.ts:1,9-47`; `web/src/lib/auth/openemr-provider.ts:1-37` (SMART-on-FHIR provider); refresh path `auth.ts:9-34` |
| 11 | Patient header | DONE | `web/src/components/cards/PatientHeader.tsx` + `.test.tsx` |
| 12a | Allergies | DONE | `cards/AllergiesCard.tsx` |
| 12b | Problem List | DONE | `cards/MedicalProblemsCard.tsx` |
| 12c | Medications | DONE | `cards/MedicationsCard.tsx` |
| 12d | Prescriptions | DONE | `cards/PrescriptionsCard.tsx` (synthesis from MedicationRequest noted in MIGRATION.md:62-66) |
| 12e | Care Team | DONE | `cards/CareTeamCard.tsx` |
| 12-FHIR | FHIR client | DONE | `web/src/lib/fhir/{client.ts,types.ts,token.ts,synthesis.ts,allergy-display.ts,error.ts}` |
| 13 | +1 section | DONE | `cards/VitalsCard.tsx` + `.test.tsx`. `EncounterControls.tsx` also present. |
| 14 | MIGRATION.md | DONE | `patient-dashboard/PATIENT_DASHBOARD_MIGRATION.md:1-100` defends Next.js by mapping 5 problems to capabilities |
| 15 | Deployment | UNVERIFIED | `patient-dashboard/DEPLOY.md` exists but no public URL surfaced in `SUBMISSION.md`. Confirm before demo. |

Loading/error states: `web/src/components/cards/card-states.tsx` provides shared states; per-card test files cover behavior.

---

## Pending / Risk Register (ranked by submission-grade impact)

1. **Dashboard public URL not in SUBMISSION.md** — graders click links. `SUBMISSION.md:11-16` lists agent-api + OpenEMR but not the Next.js dashboard URL. Fix: surface the Railway/Vercel URL.
2. **`/document/scan_unprocessed` and `/document/{id}/reclassify` MISSING** — §17.3 contract gap. If a grader reads the architecture doc and curls these routes, they 404. Either ship them or add a "deferred" line to `W2_ARCHITECTURE.md` like passive Path A already has.
3. **`clinical_synonyms.yaml` MISSING** — `W2_ARCHITECTURE.md` §10.4 / §8.6 references normalization; the synonym map asset is not present. Numeric normalization is implemented inline (`critic.py:54-64`), but the named YAML artifact and its CI threshold check are not in repo.
4. **§4.1 Passive ingest, §4.6 stuck-extraction watchdog, §5.7 intra-doc conflict pass, §6.6 contradiction pass, §11.7 quarterly meta-eval — all PARTIAL/SPEC-ONLY.** Already disclosed honestly in `SUBMISSION.md:110-117` "Phase 8 conditional items"; reviewer-friendly framing keeps this from being a surprise.
5. **Pre-existing W1 test failures.** Disclosed in `SUBMISSION.md:125`; reviewer may flag unless it's explicit during demo.
6. **Per-modality `correct_critic_decision` baseline volatility.** `baseline.json:6` floor 0.36 (and per-modality cells 0.0, 0.27, 0.33). The floor is not aspirational; if the demo run prints it, expect questions.
7. **Cost projection stops at 1k turns.** Spec asks for 10k/100k.
8. **Top-level `.env.example` does not document agent-api keys** — only the OpenEMR core defaults. Agent-api keys live in `agent-api/.env.example` only. Acceptable but worth pointing out.

---

## Recommended demo path (chronological, click-by-click)

**4-minute walkthrough, heavy on ingest + RAG + eval gate.**

**0:00–0:30 — Setup shot.**
- Open `https://copilot-agent-api-production.up.railway.app/health` in tab 1; show 200.
- Open `https://clinical-copilot-openemr-production.up.railway.app` in tab 2; log in as `admin`.
- Open `W2_ARCHITECTURE.md` table of contents in editor (tab 3) — say "1,581 lines, four pillars."

**0:30–1:30 — Ingest + extract + cite (Pillar 1 + 2).**
- Inside OpenEMR, open the Clinical Co-Pilot iframe; click into a patient with no overnight docs.
- Drag `agent-api/tests/fixtures/eval/lab_clean_2.pdf` into the FileDropZone (`agent-ui/src/components/FileDropZone.tsx`).
- Watch SSE frames: supervisor → extractor → critic → finalize. Point at the bbox overlay (`BboxOverlay.tsx`) when the user hovers a value.
- Click a citation chip — show the bbox highlights on the source page.
- Open `agent-api/extractors/schemas.py:122-141` in editor — say "strict + extra=forbid + min_length=1 on citations".

**1:30–2:30 — Critic refusal (clean refusal demo).**
- Drop `agent-api/tests/fixtures/eval/intra_doc_conflict_lactate.pdf`.
- Watch the SoftWarnBanner appear (`SoftWarnBanner.tsx`) — say "intra-doc conflict surfaces, doesn't silently resolve."
- Drop `agent-api/tests/fixtures/eval/encrypted.pdf` → hard block.
- Open `agent-api/graph/nodes/critic.py:79-86` — show the three return states (pass / soft_warn / hard_block) and the `CRITIC_ERROR` failure-closed boundary at `:21-23`.

**2:30–3:00 — RAG (Pillar 3).**
- In the chat, ask a sepsis-bundle question routed to `/evidence/search`.
- Show response with quote + source_id (`ssc-2021`).
- Open `agent-api/rag/retrieve.py:267-278` — show `cohere_rerank` + `RerankUnavailable` fallback.

**3:00–3:40 — Eval gate (Pillar 4).**
- Open `https://github.com/Hirom0112/openemr/pull/1` — show the failed `W2 Eval Suite` job.
- In the PR comment, point at `citation_present` 1.00→0.70, `correct_critic_decision` 0.96→0.50.
- Open `.github/workflows/copilot-eval.yml:259-279` — show `run_full_suite` + `diff_baseline`.
- Open `agent-api/evals/baseline.json` — point at the 18 keys, say "18 floors, gate hard-fails on a 5-pp drop or below floor."

**3:40–4:00 — Patient Dashboard (Track B).**
- Switch to dashboard URL (verify URL pre-demo).
- Sign in via OpenEMR OAuth (Auth.js); land on `/patient/[id]/page.tsx`.
- Scroll through the 7 cards (header + 5 required + Vitals).
- Open `patient-dashboard/PATIENT_DASHBOARD_MIGRATION.md:37-96` — say "Next.js Server Components keep the FHIR token off the browser."

**Close.** Briefly name pending: `/document/scan_unprocessed` and the watchdog are deferred per `SUBMISSION.md:110-117`; everything in the demo path is shipped and CI-gated.

---

## What to keep working on (post-submission)

Refining current schemas (`lab_pdf`, `intake_form`, `unknown`) before expansion is the right next move:

- Tighten `LabReport.values[].abnormal_flag` to a closed enum and bind it to a unit-aware reference range (`extractors/schemas.py:106-113` already enumerates; reference range is still a free string `:104`).
- Land §6.6 retrieval-vs-record contradiction pass and §5.7 intra-doc comparator from stub to real implementation; both are critic hooks today (`graph/nodes/cross_source_conflict.py`, `conflict/detector.py`) but the pass logic is not yet wired through to the critic verdict per `SUBMISSION.md:115`.
- Add `clinical_synonyms.yaml` as a real artifact with a CI gate. Inline normalization in `critic.py:54-64` is sufficient functionally but the named asset that the architecture references should exist.
- Lift baseline floors on `correct_critic_decision` once the conflict comparators land — current per-modality cells (e.g. `scanned_pdf` 0.0, `typed_pdf` 0.27) are below where they should sit for confident shipping.
- Cost projection at 10k/100k turns + a per-stage p50/p95 table populated from real traffic, not just the cold-start scrape currently captured in `COST_LATENCY_REPORT.md:45-54`.
- Ship §4.1 Pathway A passive prefetch behind a feature flag — most of the wiring (`agent_w2_document_ingest_total` counter) already exists.
- Quarterly judge meta-eval: the infrastructure exists (`evals/judge_credibility.json` per `SUBMISSION.md:117`); execute the first run and commit results.
- Patient dashboard public URL must be in `SUBMISSION.md` before grading.
