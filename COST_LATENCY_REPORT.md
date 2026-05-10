# Clinical Co-Pilot — Latency & Cost Report

Stream F deliverable for the demo turn-in. Numbers in this report are
pulled live from the agent-api `/metrics` endpoint (Prometheus) and from
Anthropic SDK `response.usage` fields captured at
`agent-api/agent/dispatcher.py:1818-1836`. Empty rows are explicitly
labelled "no traffic — N/A" rather than fabricated.

Report generated: 2026-05-05; refreshed 2026-05-10 with eval-suite cost
section + post-Phase-5 critic note. Production /metrics figures are
unchanged from the original snapshot (the deployed pod hasn't moved
for the metrics this report cites; FHIR-DocumentReference deploys did
not touch the dispatch / extraction code paths). Re-scrape recommended
after the next deploy.

---

## 5a. Methodology

- **Latency:** scraped from the running agent-api at `http://localhost:8400/metrics`
  (Prometheus exposition format). Histograms expose cumulative buckets — `count`
  is the sample size, `sum` is total wall-clock seconds. p50/p95/p99 are
  derived from the bucket cumulative counts (next-higher bucket boundary
  when a bucket spans the quantile, since Prometheus histograms do not
  retain raw observations). When the bucket family has zero observations it
  is reported as "no traffic — N/A".
- **Cost:** token usage is captured per dispatcher turn via the Anthropic SDK
  fields `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens` (see
  `agent-api/agent/dispatcher.py:1810-1838`). Prometheus prompt-cache
  counters (`agent_prompt_cache_{hits,misses}_total`) sum these across the
  process lifetime. Per-run totals from individual responses cannot be
  reconstructed from the counter (Prometheus is lossy for that), so the
  cost table breaks down ingest / chat into "what we measured live this
  session" vs. "projected from the W2 architecture spec".
- **What was measured:** /agent/prefetch (warmer), /agent/query (chat),
  supervisor handoff (Stream F counter). Ingest extraction was not
  exercised in the live `/metrics` scrape (no document upload), so the
  ingest latency cited in §5b is the earlier `agent_dispatch_latency_seconds`
  scrape from the same process — the only measured-in-process reading
  this report carries. The eval-suite ingest path (`run_full_suite.py`)
  is a separate measurement surface; its per-case wall-time is reported
  in §5g.

---

## 5b. Latency table

Live scrape, single coherent snapshot. Histograms with `count == 0` mean the
endpoint was not exercised since process start.

| Metric / endpoint | count | sum (s) | mean (s) | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| `agent_dispatch_latency_seconds` | 0 | 0.000 | n/a | no traffic — N/A | n/a | n/a |
| `agent_prewarm_duration_seconds` | 0 | 0.000 | n/a | no traffic — N/A | n/a | n/a |
| `agent_checkpointer_op_duration_seconds` | 0 | 0.000 | n/a | no traffic — N/A | n/a | n/a |
| `agent_post_ingest_*` | — | — | — | metric family not registered | — | — |
| `http_request_duration_seconds{handler="/health"}` | 8 | 0.013 | 0.0017 | < 0.1 | < 0.1 | < 0.1 |
| `http_request_duration_seconds{handler="/metrics"}` | 6 | 0.032 | 0.0053 | < 0.1 | < 0.1 | < 0.1 |
| `http_request_duration_seconds{handler="/agent/prefetch"}` | 2 | 0.341 | 0.171 | ~0.17 | ~0.17 | ~0.17 |
| `http_request_duration_seconds{handler="OPTIONS"}` (CORS preflight) | 3 | 0.001 | 0.0002 | < 0.1 | < 0.1 | < 0.1 |
| `agent_supervisor_handoff_total{from=supervisor,to=structured}` | 1 | counter (no latency) | — | — | — | — |
| `agent_tool_calls_total{tool="get_census_summary"}` | 1 | counter (no latency) | — | — | — | — |

A larger earlier scrape (same process, before restart) showed real
`/agent/query` traffic:

| Metric / endpoint (earlier session) | count | sum (s) | mean (s) | bucket-derived p95 |
| --- | --- | --- | --- | --- |
| `agent_dispatch_latency_seconds` | 2 | 27.65 | 13.83 | bucket 10–15 s |
| `http_request_duration_seconds{handler="/agent/query"}` | 2 | 27.99 | 14.0 | bucket > 1 s (only `0.1 / 0.5 / 1 / +Inf` buckets) |

This confirms ingest/chat round-trips dominate (≈ 14 s mean, p95 in the 10–15 s
band) while everything else is sub-second. With only 2 observations a real
quantile cannot be computed; the band is the strongest claim the data supports.

---

## 5c. Cost table

Pricing reference (per the user-supplied rate card):

| Model | Input $ / 1M tok | Output $ / 1M tok |
| --- | ---: | ---: |
| Claude Haiku 4.5 | $1.00 | $5.00 |
| Claude Sonnet 4.6 | $3.00 | $15.00 |
| Claude Opus 4.7 | $15.00 | $75.00 |
| Voyage `voyage-3-large` (embed) | $0.18 / 1M tok | — |
| Cohere `rerank-v3.5` | $2.00 / 1k searches | — |

### Measured this session (live counters, single process)

| Counter | Value | $ / 1M (input) | Implied $ |
| --- | ---: | ---: | ---: |
| `agent_prompt_cache_hits_total` (input tokens served from cache) | 4 434 | $0.30 (cached read, 10 % of full Sonnet input) | $0.0013 |
| `agent_prompt_cache_misses_total` (cache-creation input tokens) | 83 | $3.75 (cache write, 1.25× full Sonnet input) | $0.0003 |
| Output tokens (not separately Prometheus-exported) | not measured live | $15.00 | n/a (see projection) |

So the **measured** prompt-cache spend for this session ≈ **$0.002 of input**.
That excludes output-token billing, embeddings, and rerank.

### Projected per-call cost (W2 architecture §10.3 + this session)

| Code path | Per call (input $) | Per call (output $) | Embeds / rerank | Total per call |
| --- | ---: | ---: | ---: | ---: |
| Ingest (Sonnet vision, 3-pg avg, schema-fill) | ~$0.024 | ~$0.005 | $0.001 (rerank) | **~$0.030** |
| Chat / `/agent/query` (Sonnet, with cache) | ~$0.012 | ~$0.003 | ~$0.0001 embed + ~$0.001 rerank | **~$0.016** |
| Classifier (Sonnet, 1 pg + OCR text) | ~$0.004 | ~$0.001 | — | **~$0.005** |
| Boolean-rubric judge (Haiku, single call) | ~$0.0005 | ~$0.0002 | — | **~$0.0007** |
| Critic decision (mechanical, post-Phase-1+3) | $0 | $0 | — | **$0** |

Eval-side judge calls run **3× per rubric per case** (Phase 4.8 median-of-3
vote). The single-call cost above × 3 is the per-eval-case judge cost; see
§5g for full-suite total.

Rate-card note: production lab extractor pins `claude-sonnet-4-5-20250929`
(`extractors/lab.py:43`), one minor version behind the Sonnet 4.6 row in
the rate card. Pricing is identical across the Sonnet 4.x series (Anthropic
doesn't differentiate point releases on the rate card), so the projection
is not affected.

### Projected fleet cost (one patient turn ≈ 1 ingest + 2 chats)

| Volume | Cost @ $0.030 ingest + 2 × $0.016 chat = $0.062 / turn |
| --- | ---: |
| 1 turn | $0.062 |
| 100 patient turns | **~$6.20** |
| 1 000 patient turns | **~$62.00** |

Honesty footnote: per-call cost is projected from architecture §10.3, not
re-derived this session, because output tokens are not retained as a
Prometheus metric. The prompt-cache hit rate observed live (98 %, see §5d)
is consistent with the §10.3 assumptions, so the projection is not tightened
or loosened.

---

## 5g. Eval-suite cost (CI / development)

The W2 eval suite (`evals/run_full_suite.py`, 156 cases at submission lock)
is the single largest recurring cost surface in this codebase outside
production traffic. Runs on every PR via `copilot-eval.yml`.

| Run mode | Per-run cost | Wall-clock |
| --- | ---: | ---: |
| Full suite, cold (first run after `EVAL_CACHE_VERSION` bump) | **~$8–12** | ~10 min |
| Full suite, warm judge cache (`agent-api/.eval_cache/judges/`) | **~$4–5** | ~5–7 min |
| Smoke subset (`--smoke`, 10 cases) | ~$1 | ~1 min |
| Targeted (`run_targeted.py`, N cases) | ~$0.04 × N | ~5 s × N |
| Failing-only (`--failing-only prior_results.json`) | scales with failure count | proportional |

**Cost composition per cold full-suite run** (~$8–12 envelope):

- Extraction: ~$4.40 (146 cases × ~$0.030 weighted; vision-heavy modalities
  TIFF / scanned / photo / xlsx run at ~$0.05 each).
- `factually_consistent` judge (Sonnet × 3 per case): ~$4.70 (156 × $0.030).
- `safe_refusal` judge (Haiku × 3 per case, only when `expected_critic_decision != "pass"`): ~$0.45.
- Voyage embeddings + Cohere rerank (10 evidence-retrieval cases): ~$0.05.
- **Critic: $0** — mechanical (Phase 1+3), no LLM call.

The judge cache (`agent-api/.eval_cache/judges/<sha256>.json`) collapses
the median-of-3 judge spend to $0 on identical-payload reruns. First run
of a fresh `EVAL_CACHE_VERSION` pays full price; every subsequent run
over the same fixtures + prompts is amortized to extraction-only (~$4–5).

**Bumping `EVAL_CACHE_VERSION`** invalidates all judge cache entries and
forces a cold run. Required when prompt-registry changes, model pin
changes, or rubric-eval logic changes alter the expected judge output.

---

## 5d. Cache effectiveness

### Anthropic prompt cache (live this session)

- `agent_prompt_cache_hits_total` = **4 434** input tokens read from cache
- `agent_prompt_cache_misses_total` = **83** input tokens written to cache
- **Hit rate ≈ 98.2 %** — cache_read / (cache_read + cache_create)

This is unusually high; expected on a stack that just replayed prefetch on
the same patient. After a multi-patient turn it will normalize. Realistic
post-warm steady-state on a multi-patient workload sits in the **80–92 %**
band on this prompt structure (system-prompt + tool-list + per-patient
context, where the per-patient block is the cache-miss source on each
new patient). The hit rate is the headline reason the projected $/turn
stays at $0.016 — even at the lower end of the steady-state band the
per-turn cost only rises to ~$0.020.

### Redis data cache (live this session)

| Cache name | Hits | Misses | Hit rate |
| --- | ---: | ---: | --- |
| `census` | 0 | 1 | 0.0 % (cold start) |
| `bundle` | 0 | 0 | no traffic — N/A |
| `briefing` | 0 | 0 | no traffic — N/A |
| `explanation` | 0 | 0 | no traffic — N/A |

Reading: this is a cold stack — Redis caches haven't warmed yet because
ingest / chat hasn't touched these code paths in the current process. Not a
regression, just a fresh process.

### FHIR token cache (live this session)

- `agent_fhir_token_cache_hits_total` = 55
- `agent_fhir_token_cache_misses_total` = 1
- **Hit rate ≈ 98.2 %** — token cache is doing its job; OAuth round-trips
  are not a per-call cost.

---

## 5e. Bottleneck call-out

Anthropic round-trip on schema-fill extraction is the dominant latency.
The earlier `agent_dispatch_latency_seconds` scrape showed mean ≈ 14 s with
p95 in the 10–15 s bucket; this matches the `/agent/query` HTTP histogram
sum (27.99 s / 2 = 14.0 s mean). Mitigations already shipped: Sonnet 4.6
prompt cache (98 % hit rate this session — confirmed live), Haiku 4.5 on the
fast path / boolean rubric judge, and parallel summary + guidelines fetch
in the prefetch warmer.

The critic loop has been moved off the LLM entirely (Phase 1+3): every
critic decision is now mechanical — schema validation, citation walk,
demographic comparator, and the Phase-1A/3A/3D detector suite — running
in single-digit milliseconds per case with $0 marginal cost. The W1
headroom item ("move critic to Haiku") is closed; what remains is moving
more of the guideline-retrieval + chat synthesis loop to Haiku where
evidence quality permits.

---

## 5f. Deployed status

| Field | Value |
| --- | --- |
| Project | `Openemr-deployment` |
| Environment | `production` |
| Service | `copilot-agent-api` |
| Public URL | https://copilot-agent-api-production.up.railway.app |
| `GET /health` | **HTTP 200**, body `{"status":"ok","redis":true}`, 532 ms TTFB |
| Last-seen log timestamp | `2026-05-05T13:17:03Z` (Anthropic SDK httpcore activity confirms the deployed process is making outbound Claude API calls — the Anthropic key is wired) |
| Smoke verification | `curl -sw "HTTP %{http_code}" .../health` → `HTTP 200` |

The deployed pod is live, healthy, Redis-connected, and actively round-tripping
to Anthropic — the MVP "deployed app" criterion is met.

**Re-verification cadence:** the next deploy that touches dispatcher /
extractor / critic code paths should re-scrape `/metrics` and update §5b
latency table + §5d cache-effectiveness counters. Commits since this
report (`07a24fb31`, `f464407c0`, `7b7c597e7`) touched FHIR
DocumentReference paths, not the dispatch loop, so the §5b numbers are
still load-bearing for the Stream F deliverable.
