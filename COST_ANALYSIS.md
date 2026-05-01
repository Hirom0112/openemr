# Clinical Co-Pilot — AI Cost Analysis

## Model

**Claude Sonnet 4.6** (`claude-sonnet-4-6`)

| Token type | Price (per 1M tokens) |
|---|---|
| Input | $3.00 |
| Output | $15.00 |
| Cache write (ephemeral) | $3.75 |
| Cache read (ephemeral) | $0.30 |

Prompt caching is active on every dispatch call. The system prompt and per-session census context are marked `cache_control: ephemeral`. Cache reads cost 10× less than regular input — in a 16-patient session, 88% of input tokens are served from cache.

---

## Actual Development Spend

The test suite has 239 cases but only ~37 make live API calls (routing and dispatcher verification tests). The rest are deterministic — triage rules, verification, citation extraction, FHIR auth — and never call the Anthropic API.

| Item | Estimate |
|---|---|
| Per test run (37 live calls × ~6k input + ~225 output) | ~$0.79 |
| Full test runs during development (~30–50) | ~$24–40 |
| Ad-hoc debugging, prompt iteration, integration testing | ~$15–25 |
| **Total dev API spend** | **~$35–65** |

Check the exact figure at console.anthropic.com → Usage.

The number is low because almost all clinical-safety-critical logic (triage rules, source attribution, domain constraints) is deterministic code. The LLM is called for narrative generation and tool routing — not for clinical decision-making.

Infrastructure during development: Railway single-service plan (~$30/month) + Redis (~$0, bundled in compose stack) = **~$50–100 total infra spend** over the development period.

---

## Per-Session Cost Model

A "session" = one physician opening Co-Pilot and completing morning rounds on 16 patients, using all five use cases.

### Token shape per session

| Step | Tool | Cache writes | Cache reads | Uncached input | Output |
|---|---|---|---|---|---|
| Session open (cache creation) | — | 33,500 | 0 | 0 | 0 |
| UC-1 Census summary | `get_census_summary` | 0 | 33,500 | 2,000 | 600 |
| UC-2 Briefings × 16 | `get_patient_briefing` | 0 | 56,000 | 8,000 | 3,200 |
| Rationale click × 8 | `get_triage_rationale` | 0 | 20,000 | 2,400 | 640 |
| UC-3 Follow-up queries × 5 | `query_patient_records` | 0 | 20,000 | 4,000 | 750 |
| UC-4 Medication checks × 3 | `get_medication_safety` | 0 | 9,000 | 1,500 | 600 |
| UC-5 Handoff | `generate_handoff` | 0 | 16,000 | 2,500 | 1,800 |
| **Total** | | **33,500** | **154,500** | **20,400** | **7,590** |

### Per-session cost breakdown

| Line item | Tokens | Rate | Cost |
|---|---|---|---|
| Cache writes | 33,500 | $3.75/MTok | $0.1256 |
| Cache reads | 154,500 | $0.30/MTok | $0.0464 |
| Uncached input | 20,400 | $3.00/MTok | $0.0612 |
| Output | 7,590 | $15.00/MTok | $0.1139 |
| **Session total** | | | **$0.35** |

Without caching, the 154,500 cache-read tokens would cost $0.464 at the input rate. **Caching saves $0.42 per session — a 54% reduction.**

---

## Projected Production Costs

"User" = one active physician making one rounding session per working day (~22 days/month). All projections assume 60% daily active rate (pilots and enterprise rollouts always have stragglers and light users). These costs are *not* derived by multiplying per-session cost by user count — each tier requires architectural changes that alter the cost curve.

---

### 100 Users — ~$700/month

**API:** ~1,800 sessions/month × $0.35 = **$630**

**Infrastructure:** Single Railway deployment + Redis = **$70/month**

**Total: ~$700/month | Per user: $7.00/month**

Nothing changes architecturally from the dev setup. The single constraint is Redis durability — configure AOF persistence so a container restart doesn't wipe in-progress sessions.

**The real cost at this tier isn't money, it's engineering time.** A pilot requires someone handling physician feedback, rotating credentials, and triaging errors. Budget 2–4 hours/week. That labor cost exceeds the API bill.

---

### 1,000 Users — ~$5,100/month

**API:** ~13,200 sessions/month × $0.35 = **$4,620**

**Infrastructure:**
| Item | Cost |
|---|---|
| 3 stateless `agent-api` replicas + load balancer | $140/month |
| Redis with AOF persistence | $50/month |
| Monitoring upgrade | $30/month |
| **Total infra** | **$220/month** |

**Total: ~$4,840/month | Per user: $4.84/month**

**What actually breaks — the FHIR tier, not the agent tier.** 600 active physicians hitting census at 7 AM = 600 × 16 patients = 9,600 FHIR requests in 30 minutes. OpenEMR's PHP-FPM pool handles ~50–100 concurrent requests. The queue forms at the FHIR layer, not the AI layer.

**Architectural changes required:**
- `agent-api` replicas are already stateless by design — the checkpointer writes all session state to Redis. Add a load balancer and replicate.
- **FHIR bundle cache keyed by patient ID** in Redis with 60-minute TTL. Two physicians covering overlapping patients don't re-fetch the same bundle. Reduces FHIR load ~30% and cuts agent-side cold-cache API calls.
- **Pre-fetch backpressure:** stagger census dispatches over 5-minute windows rather than hitting FHIR and Anthropic simultaneously at session open. Physicians see a loading state for <5 seconds rather than a spike-induced 20-second delay.

---

### 10,000 Users — ~$43,750/month

**API (before optimization):** 132,000 sessions/month × $0.35 = $46,200

**Model mix optimization becomes economically justified at this tier.** The triage rationale (one-line explanation per patient, clicked ~8× per session) is the highest-volume, simplest task in the system. Routing it to Haiku 4.5 ($0.80/$4.00 per MTok):

- Current rationale cost per session: ~$0.04
- With Haiku: ~$0.007
- Savings: $0.033 × 132,000 = **$4,356/month** — enough to pay for the Kubernetes cluster

**Optimized API cost: ~$42,000/month**

**Infrastructure:**
| Item | Cost |
|---|---|
| Kubernetes cluster (3 nodes, HPA on `agent-api`) | $1,000/month |
| Redis Cluster (3 primaries + replicas) | $300/month |
| CDN for React bundle | $50/month |
| API gateway (rate limiting per provider) | $100/month |
| Observability (Langfuse Cloud or Datadog) | $300/month |
| **Total infra** | **$1,750/month** |

**Total: ~$43,750/month | Per user: $4.38/month**

**What actually breaks — the morning rush becomes a spike problem.** 6,000 physicians × 16 patients = 96,000 FHIR requests in 30 minutes, and 6,000 simultaneous Anthropic API calls at census open. The Anthropic API has rate limits — you will hit them without queuing.

**Architectural changes required:**
- **Kubernetes with HPA** scaling on request queue depth, not CPU. CPU is a lagging indicator for async I/O-bound workloads.
- **Census queue with backpressure:** census pre-fetches enter a Redis queue; workers pull with a ceiling on concurrent Anthropic calls. Physicians receive partial results via SSE as each patient's bundle completes rather than waiting for all 16.
- **Patient bundle deduplication:** at a large hospital, multiple physicians share patients. A bundle cached by patient ID (not physician session) cuts repeat FHIR calls by 40–60% during morning rounds.
- **Async handoff via Batch API:** handoff is not time-sensitive. Route it through Anthropic's Batch API (50% cost reduction, results ready in under 1 minute in practice). This alone saves ~$3,150/month at this scale.

---

### 100,000 Users — ~$301,000/month

**API (base, no optimization):** 1,320,000 sessions/month × $0.35 = $462,000

**Optimizations that only make sense at this scale:**

| Optimization | Monthly savings | Notes |
|---|---|---|
| Haiku for triage rationale | ~$42,000 | Low engineering cost — call is already isolated |
| Batch API for handoff (~15% of spend) | ~$35,000 | Low engineering cost — already queued |
| Anthropic enterprise discount (~25%) | ~$96,000 | Procurement, not engineering |
| Fine-tuned routing classifier | ~$8,000 | 3–6 month engineering project — worth it at this scale |

**Optimized API cost: ~$281,000/month**

The fine-tuned routing classifier deserves explanation. Every agent query today costs one full Sonnet dispatcher call just to decide which tool to invoke. At 1.32M sessions × ~3 queries per session = ~4M routing decisions/month. A fine-tuned Haiku or lightweight classifier makes that decision for near-zero cost. The data collection, fine-tuning, and shadow-testing pipeline is a 3–6 month project — only economically justified above ~50K users.

**Infrastructure:**
| Item | Cost |
|---|---|
| Multi-region K8s (us-east-1, us-west-2, eu-west-1) | $8,000/month |
| Redis Cluster per region | $2,000/month |
| CDN + global load balancing | $2,000/month |
| Observability (Datadog enterprise) | $5,000/month |
| API gateway + WAF | $1,000/month |
| SRE tooling | $2,000/month |
| **Total infra** | **$20,000/month** |

**Total: ~$301,000/month | Per user: $3.01/month**

**The architectural shift at this tier is geographic, not just horizontal.** Morning rounds hit at 7 AM in every timezone. That is a rolling global spike, not a single spike — which is actually *better* than a single-timezone product. But it requires:

- **Session affinity to home region.** A physician must always route to their home region or their Redis session state won't be found. Cross-region cache reads are latency-killers against a 3-second SLA.
- **Patient bundle as a shared global resource.** A patient admitted to a hospital whose attending is Dr. Chen and whose covering hospitalist is Dr. Patel should not require two independent FHIR fetches from two different sessions. A global patient bundle cache with access-control enforcement is the single highest-impact optimization at this scale. At 100K physicians covering overlapping patient panels, deduplication reduces total FHIR calls — and the Anthropic cache-creation tokens that go with them — by an estimated 35–50%.
- **Redis geo-replication** per region with no cross-region reads in the hot path.
- **Tenant isolation at the key namespace level.** Hospital system A cannot share a Redis keyspace with Hospital system B — not for technical reasons but for contractual and compliance reasons.

---

## Summary

| Scale | API Cost | Infra | Total | Per User/Month |
|---|---|---|---|---|
| Dev spend (one-time) | ~$50 | ~$75 | ~$125 | — |
| 100 users | $630 | $70 | **$700** | $7.00 |
| 1,000 users | $4,620 | $220 | **$4,840** | $4.84 |
| 10,000 users | $42,000 | $1,750 | **$43,750** | $4.38 |
| 100,000 users | $281,000 | $20,000 | **$301,000** | $3.01 |

**Cost per user drops as you scale** for three compounding reasons:

1. **FHIR bundle deduplication** — physicians share patients; caching bundles by patient ID reduces cache-creation token spend proportionally to panel overlap, which increases with deployment size
2. **Model mix optimization** — Haiku for rationale and Batch API for handoff only make engineering sense above ~5K users; below that the savings don't justify the complexity
3. **Enterprise pricing** — volume discounts apply above a negotiated threshold

Infrastructure grows but stays a small fraction of total cost. At 100K users, infra is 6.6% of spend. The API bill is the business. SaaS pricing to a hospital system at $15–20/physician/month yields positive margin at every tier shown above.
