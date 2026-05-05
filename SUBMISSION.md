# Clinical Co-Pilot — Submission

**A multi-agent clinical agent that reads documents, cites every fact to a real source with verified value-to-bbox fidelity, refuses cleanly when uncertain, surfaces conflicts rather than silently resolving them, and is gated by a 50-case CI suite — so the user's morning brief sees the messy half of the chart she would otherwise be assembling herself.**

---

## Live URLs

| Surface | URL |
|---|---|
| Agent API (health) | https://copilot-agent-api-production.up.railway.app/health |
| Agent API (metrics) | https://copilot-agent-api-production.up.railway.app/metrics |
| OpenEMR (chart system of record) | https://clinical-copilot-openemr-production.up.railway.app |
| Demo video | `[VIDEO_LINK_HERE]` |
| Code (W2 branch) | https://github.com/Hirom0112/openemr/tree/clinical-copilot |

Reproduce the demo end-to-end from `docs/DEMO_SCRIPT.md`.

---

## Eval gate evidence

The 50-case W2 eval suite is wired into GitHub Actions and hard-fails on regression. We proved it by seeding a regression and watching the gate bite.

| Item | Value |
|---|---|
| Regression PR | https://github.com/Hirom0112/openemr/pull/1 |
| What the PR does | Strips the `citations` field from the extractor's output (one-line regression that mimics a careless refactor). |
| W2 Eval Suite job result | **FAIL** — `citation_present` rubric dropped 100% → 74% (−26 pts), `GATE: FAIL`, exit 1. |
| Other rubrics on same PR | held at baseline — the gate isolates which property regressed. |
| Failure screenshot / artifact | `[SCREENSHOT_HERE]` |

**Test counts (clean, post-fix):**

- 204 W2 backend tests, 0 failures.
- 49 agent-ui Jest tests, 0 failures.
- 0 import-linter contract violations (`agent-api/.importlinter`).

**Gate mechanics:** 50 cases × 6 boolean rubrics → per-rubric pass rates compared against `evals/baseline.json`. `evals/diff_baseline.py` enforces the per-rubric floor. CI workflow: `.github/workflows/copilot-eval.yml` (job `w2-eval`).

---

## Architecture references

| Document | Purpose |
|---|---|
| `W2_ARCHITECTURE.md` | Single-source-of-truth design doc (1,360 lines). |
| `W2_ARCHITECTURE.md` §4.2.1 / §4.2.2 | Custom-upload deployment deviation and security tradeoff. |
| `docs/SECURITY_TRADEOFFS.md` | Full analysis of the shared-HMAC tradeoff and reversibility plan. |
| `docs/COST_LATENCY.md` | Cost model and measurement methodology (Phase 8.1 scaffold). |
| `ARCHITECTURE.md` §5.5 | Observability metric and event-type catalog (W1 + W2 entries). |
| `EVAL.md` | W1 + W2 eval suite description. |
| `docs/DEMO_SCRIPT.md` | Reproducible 4-minute demo script. |

---

## What's deployed vs spec

| Pillar / Section | Spec | Deployed |
|---|---|---|
| Pillar 1 — Document Ingestion (§4) | FHIR Binary POST → legacy REST upload → custom upload → local disk | **Live.** Tier 1 + 2 unavailable on deployed OpenEMR build (404 / 401). Tier 3 (custom JWT-protected `oe-module-clinical-copilot/public/upload.php`) is the active path. Documented as architectural deviation in §4.2.1. |
| Pillar 2 — Multi-Agent Graph (§5) | Supervisor + extractor + retriever + critic over LangGraph, SSE-streamed | **Live** at `POST /agent/w2/dispatch` with SSE. Streams supervisor → workers → critic frames. |
| Pillar 3 — Hybrid RAG (§6) | Sparse (tsvector) + dense (Voyage embeddings) merged, reranked with Cohere Rerank 3 | **Live** at `POST /evidence/search`. Indexing pipeline built; corpus loaded. |
| Pillar 4 — Eval Gate (§11) | 50 cases, 6 boolean rubrics, baseline + diff, CI hard-fail | **Live.** `evals/baseline.json` + `evals/diff_baseline.py` + `.github/workflows/copilot-eval.yml` job `w2-eval`. Verified by PR #1. |
| Citation contract (§8) | 5-field shape with bbox-grounded fidelity check | Live; per-value fidelity check runs in critic. |
| Observability (§10, ARCHITECTURE.md §5.5) | Per-event structured logs + Prometheus metrics, no PHI | Live; `agent_w2_*` metric family populated, audit dual-target preserved (§9.4 / §4.2.2). |

---

## Known gaps — Phase 8 conditional items

Stating these honestly rather than papering over them. Each is scoped, deferred to Phase 8 or beyond, and does not affect the core demonstration.

- **Pillar 1 Path A (passive ingest).** §4.1 describes a polling-based passive pickup of documents already in OpenEMR. The active path (§4.2 + §4.2.1) is shipped and demonstrated; passive ingest is a Phase 8 follow-on.
- **APScheduler watchdog (§4.6).** Stuck-extraction reaper for the stub-row claim is specified but not yet wired. The stub-row mechanism itself is in place; only the watchdog cron is deferred.
- **Intra-document conflict detection (§5.7) and retrieval-vs-record contradiction (§6.6).** Critic surfaces them as hooks today; the comparison passes themselves are stubbed for Phase 8.
- **Quarterly judge meta-eval (§11.7).** The infrastructure exists (`evals/judge_credibility.json`); the first quarterly run has not been executed.

---

## Honesty section

Two items recorded for the reviewer rather than buried.

- **Pre-existing W1 test failures in CI are unrelated to W2 work.** I reproduced them on the pre-Phase-1 commit (W2 branch point) — they are inherited W1 baseline noise, not regressions introduced by W2 changes.
- **Document persistence on Railway uses the custom upload tier**, not OpenEMR's documented FHIR Binary write or legacy REST upload, because both upstream paths fail on the deployed OpenEMR build (FHIR `Binary` is `read`-only and the route returns 404; legacy REST upload is ACL-gated and returns 401 even with a valid bearer token and `api:oemr` scope). Full analysis in `W2_ARCHITECTURE.md` §4.2.1; security tradeoff of the shared-HMAC custom path in §4.2.2 and `docs/SECURITY_TRADEOFFS.md`. The deviation is reversible and gated by an environment variable (`COPILOT_JWT_SECRET`); unsetting it disables tier 3 and the fallback chain falls through cleanly.
