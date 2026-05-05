# Cost & Latency Report

## 1. Executive summary

This report holds the four components of the W2_ARCHITECTURE §10.4 deliverable:
projected per-encounter cost (§10.3, copied verbatim), actual dev spend over the
build window, p50/p95 latency per pipeline stage, and bottleneck analysis with
predictions to validate. Empty cells mean **"not yet measured"** — they fill as
the deploy accrues traffic. The report is regenerated post-deploy by pulling
histogram quantiles from the agent-api `/metrics` endpoint and vendor-spend
totals from the Anthropic, Cohere, and Voyage consoles.

## 2. Projected per-encounter cost (W2_ARCHITECTURE §10.3)

Per-encounter cost components (synthetic data, Sonnet 4.6 + Haiku 4.5 + Cohere
+ Voyage):

| Component | Per-document ingest | Per-query |
|---|---|---|
| OCR (PyMuPDF / Tesseract) | $0 (CPU-only) | n/a |
| Classifier (Sonnet) | ~$0.005 (1 page + OCR text) | n/a |
| Schema-fill (Sonnet vision) | ~$0.025 (3-page avg, 1 image per page) | n/a |
| Cohere rerank | n/a | ~$0.001 |
| Voyage embeddings (query) | n/a | ~$0.0001 |
| Answer model (Sonnet, with prompt caching) | n/a | ~$0.015 |
| **Total** | **~$0.030 / document** | **~$0.016 / query** |

Eval-gate amortization: ~$0.50 per full PR run × ~5 PRs/day + nightly runs +
meta-eval cadence ≈ **~$100/month budget line**. Acceptable for PR-blocking CI.

## 3. Actual dev spend (build window)

Pulled from `console.anthropic.com → Usage` for the build window, plus Cohere
and Voyage console exports. Reported as one line per vendor, plus a total. The
per-encounter projections in §2 are validated against this actual dev mix.

| Vendor | Spend (build window) | Notes |
|---|---|---|
| Anthropic (Sonnet) | _to fill post-build_ | Vision + dispatch + factuality judge |
| Anthropic (Haiku) | _to fill post-build_ | Boolean rubric judge |
| Cohere | _to fill post-build_ | Rerank only |
| Voyage | _to fill post-build_ | Embeddings, indexing-time only |
| **Total** | _to fill post-build_ | |

## 4. p50/p95 latency (per-stage histograms)

Captured as Prometheus histograms from `https://copilot-agent-api-production.up.railway.app/metrics`.
Sampled from a fixed query workload (the 50 eval cases, plus 20 ingest passes
against the synthetic panel) to make runs comparable across deploy versions.

> **Populate by querying `/metrics` with `histogram_quantile()` in PromQL or by
> parsing the Prometheus exposition format directly** (look for `_bucket` lines
> and compute quantiles client-side). p50/p95 columns are intentionally empty
> until the fixed workload is run against the deployed build.

### 4.1 Ingest path

| Stage | Spec metric (§10.4) | Shipped today? | p50 | p95 |
|---|---|---|---|---|
| OCR / layout | `agent_w2_ingest_ocr_duration_seconds` | yes (under different label set) — covered by `agent_w2_extraction_duration_seconds{doc_type,classifier_confidence_bucket}`, which times OCR + classifier + vision + validation jointly. Phase 8 will add per-stage breakdown. | | |
| Classifier | `agent_w2_ingest_classifier_duration_seconds` | yes (under different label set) — same combined histogram as above. Phase 8 will add per-stage breakdown. | | |
| Schema-fill | `agent_w2_ingest_schemafill_duration_seconds` | yes (under different label set) — same combined histogram as above. Phase 8 will add per-stage breakdown. | | |
| FHIR write | `agent_w2_ingest_fhirwrite_duration_seconds` | Phase 8 | | |
| Critic | `agent_w2_ingest_critic_duration_seconds` | Phase 8 | | |
| **End-to-end** | `agent_w2_ingest_total_duration_seconds` | Phase 8 | | |

Adjacent ingest signals already shipped (see `agent-api/agent/metrics.py`):
`agent_w2_document_ingest_total{path,doc_type,outcome}`,
`agent_w2_classifier_confidence{doc_type}`,
`agent_w2_ocr_confidence{doc_type}`,
`agent_w2_critic_decisions_total{decision,reason}`,
`agent_w2_demographic_checks_total{outcome}`.

### 4.2 Query path

| Stage | Spec metric (§10.4) | Shipped today? | p50 | p95 |
|---|---|---|---|---|
| Retrieval (sparse) | `agent_w2_query_sparse_duration_seconds` | yes (under different label set) — covered by `agent_w2_retrieval_duration_seconds{mode="sparse"}`. Phase 8 will rename to the spec metric. | | |
| Retrieval (dense) | `agent_w2_query_dense_duration_seconds` | yes (under different label set) — `agent_w2_retrieval_duration_seconds{mode="dense"}`. Phase 8 will rename to the spec metric. | | |
| Merge + dedup | `agent_w2_query_merge_duration_seconds` | yes (under different label set) — `agent_w2_retrieval_duration_seconds{mode="merge"}`. Phase 8 will rename to the spec metric. | | |
| Rerank | `agent_w2_query_rerank_duration_seconds` | yes (under different label set) — `agent_w2_retrieval_duration_seconds{mode="rerank"}`. Phase 8 will rename to the spec metric. | | |
| Contradiction pass | `agent_w2_query_contradiction_duration_seconds` | Phase 8 | | |
| Answer generation | `agent_w2_query_answer_duration_seconds` | Phase 8 (today, dispatcher-wide latency is captured by `agent_dispatch_latency_seconds`) | | |
| **End-to-end** | `agent_w2_query_total_duration_seconds` | Phase 8 (today, `agent_dispatch_latency_seconds` is the closest proxy) | | |

Adjacent query signals already shipped: `agent_w2_retrieval_hits_total{mode}`,
`agent_dispatch_latency_seconds`, `agent_prompt_cache_hits_total`,
`agent_prompt_cache_misses_total`, `agent_data_cache_{hits,misses}_total{cache}`.

## 5. Bottleneck analysis predictions

Predictions before measurement:

| Path | Predicted bottleneck | Why |
|---|---|---|
| Ingest (text-PDF) | Schema-fill (Claude vision) | Vision call dominates over local OCR + FHIR write |
| Ingest (scanned PDF) | OCR (Tesseract) | Tesseract on multi-page scans is CPU-bound and serial |
| Query | Answer generation (Sonnet) | Single LLM call with cached prompt; rerank and retrieval are sub-100ms |

Methodology: for each path, the bottleneck is the stage whose p95 is the
largest fraction of the end-to-end p95. If that fraction is below 40%, report
"balanced — no single stage dominates" with the top three contributors listed.
Validation outcome will be added here once latency data accrues.

## 6. How this report is regenerated

- Pull histogram quantiles from `/metrics` on https://copilot-agent-api-production.up.railway.app
  using `histogram_quantile(0.5, ...)` and `histogram_quantile(0.95, ...)` in
  PromQL, or by parsing the raw Prometheus exposition format.
- Vendor spend totals from `console.anthropic.com` (Sonnet + Haiku rows
  separately), `dashboard.cohere.com`, and `dash.voyageai.com`.
- Update the dev-spend table at the cadence chosen by the team (suggested:
  weekly during the build window, monthly post-launch). Re-run the fixed
  workload (50 eval cases + 20 ingest passes) before each latency refresh so
  numbers stay comparable across deploy versions.
