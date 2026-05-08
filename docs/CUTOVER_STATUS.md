# Phase 13 Cutover Gate Status

**Last updated:** 2026-04-30
**Cross-reference:** `docs/AGENT_CONTRACT.md` §2 and §6

Cutover from legacy per-use-case endpoints to the dispatcher (`POST /agent/query`) proceeds when all gates below are PASSED. Legacy endpoints remain behind `LEGACY_ENDPOINTS_ENABLED=true` (default) for one full release cycle after cutover is confirmed.

---

## Gates verified from codebase (no live deployment needed)

| Gate | Status | Evidence |
|------|--------|----------|
| Phase 5 verification shipped | **PASSED** | `verification/dispatcher_response.py` hooked at `end_turn`; 13 `@pytest.mark.hard_failure` tests green in CI |
| Routing accuracy ≥ 95% on eval set | **PASSED** | `test_agent_routing.py` 24/24 tests pass (100%); includes authorization probe, ambiguous queries, wrong-tool regression |
| Tool misroute handling implemented | **PASSED** | Dispatcher has misroute detection + 1 self-correction max; `misroute_detected` / `self_corrected` tagged in Langfuse + Prometheus |
| Legacy endpoints tagged | **PASSED** | All 5 legacy handlers in `main.py` marked `# LEGACY — retire after Phase 13 cutover`; startup warning logged when `LEGACY_ENDPOINTS_ENABLED=true` |

---

## Gates requiring live deployment (manual verification)

| Gate | Threshold | Status | How to verify |
|------|-----------|--------|--------------|
| Dispatcher p95 latency | ≤ 4s | **PENDING** | Run load test against deployed agent-api; check Grafana `agent_dispatch_latency_seconds` p95 panel |
| Error rate | ≤ 1% over 24h soak | **PENDING** | Monitor Grafana error rate panel for 24h after deployment |
| Tool misroute rate | ≤ 2% | **PENDING** | Check `agent_tool_misroute_total / agent_tool_calls_total` in Prometheus after 24h soak |
| Cache-hit input tokens | ≥ 70% for UC-2/3/4 | **PENDING** | Check Langfuse token telemetry: `cache_read_input_tokens / input_tokens` averaged across UC-2/3/4 calls within a session |
| 23 synthetic patients loaded on Railway | required | **PENDING** | Run `python3 synthetic_data/load.py` (set `BASE_URL`, `CLIENT_ID`, `CLIENT_SECRET`, `MYSQL_PASS`); verify `GET /apis/default/fhir/Patient` returns 23 |
| Census count match | required | **PENDING** | Compare `/fhir/Patient` count with `POST /agent/query` census response patient count |

---

## Cutover decision checklist

Before retiring legacy endpoints, all of the following must be true:

- [ ] All PENDING gates above are PASSED
- [ ] `LEGACY_ENDPOINTS_ENABLED=false` tested in staging — no dependent client breaks
- [ ] One full release cycle elapsed with legacy endpoints behind the flag
- [ ] `W1_ARCHITECTURE.md` updated to reflect shipped state (human review required)

## Retirement procedure

1. Set `LEGACY_ENDPOINTS_ENABLED=false` in production env
2. Monitor error rate for 24h — rollback is `LEGACY_ENDPOINTS_ENABLED=true`
3. After stable: remove legacy route handlers from `main.py` in a subsequent release
4. Update this document status to COMPLETE

---

## Phase 15 Acceptance

**Date verified:** 2026-04-30

| Item | Status | Evidence |
|------|--------|----------|
| Clean test run — 255 passed, 0 failed | **PASSED** | `python3 -m pytest agent-api/tests/` → 255 passed, 0 failed, 0 skipped |
| CI gates configured (hard_failure + clinical_accuracy + routing) | **PASSED** | `copilot-eval.yml`: triggers on `agent-api/**`; 3 gates with empty-selection guards; hard_failure 205 tests, clinical_accuracy 163 tests, routing 24 tests |
| Routing accuracy ≥ 95% on ≥ 20-query eval set | **PASSED** | `test_agent_routing.py` 24/24 = 100%; includes authorization probe (hard_failure), ambiguous queries, missing-data, dual-path rationale |
| Session-open flow < 5s end-to-end | **PENDING** | Static greeting: instant (React constant). Pre-fetch: async, fires on mount. Budget is `get_census_summary` LLM response. Requires live deployment measurement against Railway. |
| Click-to-expand rationale < 2s | **PENDING** | `POST /agent/triage_rationale` → `get_triage_rationale` direct (no dispatcher). Reads Redis cache + rules engine only — LLM not called. Requires live deployment measurement. |
| Citations present on all clinical claims per claim taxonomy | **PASSED** | `test_claim_without_citation_stripped` passes; all 6 tools return `Citation.to_dict()` objects; 7 claim classes covered; claim-without-citation strip active in `dispatcher_response.py` |
| Domain constraints run on final dispatcher output | **PASSED** | `verify_dispatcher_response()` hooked at `end_turn` in `dispatcher.py`; 13/13 `hard_failure` tests pass including NKDA strip, stale critical, canary block, recommendation strip, isolation/code-status flags |

### Items requiring live deployment before final acceptance sign-off

- Session-open flow < 5s: measure via Grafana latency panel after Railway deployment
- Click-to-expand < 2s: measure via Grafana after Railway deployment
- All 6 PENDING cutover gates in the table above (p95 latency, error rate, misroute rate, cache-hit, Railway patient load, census count match)
