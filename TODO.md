# Clinical Co-Pilot — TODO

## Phase 0: Synthetic Data + Infrastructure

- [x] Confirm Railway OpenEMR deploy is live (`GET /apis/default/fhir/Patient` returns 200)
- [x] Verify SMART / Client Credentials OAuth is configured on the Railway instance
- [x] Run `synthetic_data/generate.py` → 18 patient FHIR R4 bundles, fixed seed (seed=42)
- [x] Run `synthetic_data/load.py` against Railway `BASE_URL`
- [x] Spot-check: confirm 18 patients returned from `GET /fhir/Patient`
- [x] Spot-check: qSOFA vitals present for Marcus Webb (bed 501) — LOINC 9279-1 RR=26, 8867-4 HR=118, SpO2=91 ✓
- [x] Spot-check: Delia Fontaine K+=6.4 critical lab via FHIR ✓
- [x] Commit static JSON bundles to repo (`synthetic_data/bundles/`)
- [ ] Docker compose additions: agent-api, Redis, Langfuse, Prometheus, Grafana
- [ ] Redis Checkpointer: RedisSaver + SqliteSaver
- [ ] FHIR Client Credentials auth wired into agent-api

## Phase 1: UC-1 Triage Engine

- [ ] Deterministic rules engine (YAML config, 10 priority levels)
- [ ] Census context builder
- [ ] First LLM call (one-line explanations only — not ranking)
- [ ] Minimal verification layer (domain constraints only)
- [ ] Langfuse + Prometheus wired in from this point forward

## Phase 2: UC-2 Pre-Encounter Briefing

- [ ] Full context builder for patient briefing
- [ ] Full verification layer (source attribution + domain constraints)
- [ ] Structured output schema locked

## Phase 3: UC-3 Targeted Record Query

- [ ] Query router (hybrid: classifier first, LLM fallback on low-confidence)
- [ ] Extended FHIR search window
- [ ] Multi-turn conversation continuity

## Phase 4: UC-4 + UC-5

- [ ] UC-4: Medication safety surface
- [ ] UC-5: Parallel handoff generation

## Phase 5: Frontend

- [ ] Thin PHP shell module (`oe-module-clinical-copilot`)
- [ ] React sidebar panel

## Phase 6: Eval Suite

- [ ] 47-test suite (hard failure: 100%, clinical accuracy: 95%, latency: 95%)
- [ ] CI gate wired to prompt / tool / config changes

---

> **Note:** `DEPLOYMENT.md` may need to be deleted once all phases above are confirmed complete. Do not delete until everything is done and verified.
