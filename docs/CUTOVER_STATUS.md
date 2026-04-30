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
- [ ] `ARCHITECTURE.md` updated to reflect shipped state (human review required)

## Retirement procedure

1. Set `LEGACY_ENDPOINTS_ENABLED=false` in production env
2. Monitor error rate for 24h — rollback is `LEGACY_ENDPOINTS_ENABLED=true`
3. After stable: remove legacy route handlers from `main.py` in a subsequent release
4. Update this document status to COMPLETE
