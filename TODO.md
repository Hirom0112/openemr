# Clinical Co-Pilot — TODO

> Completed phase history: [docs/archive/copilot-todo-history-2026-04.md](docs/archive/copilot-todo-history-2026-04.md)
> Last archived: 2026-04-30

---

## Now — Must Do This Week

- [ ] Fix `scripts/04-verify-cutover-gates.sh` Gate 2 (p95 latency): add a census warm-up call before the 10 timed briefing queries. The 4s target applies to cached in-session calls, not cold first-call latency (~34s). Gate test must warm Redis first, then measure.
- [ ] Schedule clinician priority review: 1-hour session with a hospitalist or charge nurse. Walk through P1–P10, all thresholds, 5 representative patients in `agent-api/triage/rules/rules_engine_config.yaml`. Ask: "Would you trust this list at the start of a shift?" — update config and re-run eval suite (`python3 -m pytest agent-api/tests`) based on feedback.

---

## Blocked / Manual — Human-Only External Actions

### Deployment verification (Railway live session)

- [ ] **[HUMAN]** Run a full representative session: census → 3+ briefings → 2+ queries → medication check → handoff. Monitor for 24 hours with no errors.
- [ ] **[HUMAN]** After 24h stable soak, disable legacy endpoints: `railway variables set LEGACY_ENDPOINTS_ENABLED=false --service copilot-agent-api`. Verify `POST /triage/census` returns 404 and `POST /agent/query` continues working.
- [ ] **[HUMAN]** Confirm pt-019 at P9, pt-020 at P10 in full 23-patient live census (requires live session).
- [ ] **[HUMAN]** Check Grafana: error rate ≤ 1% (Gate 4) and misroute rate ≤ 2% (Gate 5) — import `agent-monitoring/grafana/dashboards/co-pilot-overview.json`.
- [ ] **[HUMAN]** Check Langfuse Cloud: cache-hit tokens ≥ 70% for UC-2/3/4 (Gate 6) — compare `cached_input_tokens / total_input_tokens` in traces.
- [ ] **[HUMAN]** Update `ARCHITECTURE.md` to reflect shipped state (paused per prior instruction — resume when ready).

### OpenEMR verification (Railway)

- [ ] **[HUMAN]** Log in to OpenEMR → confirm "Co-Pilot" appears in the navigation bar.
- [ ] **[HUMAN]** Click "Co-Pilot" tab → confirm chat panel opens, greets by provider name, census auto-dispatches within ~5 seconds.
- [ ] **[HUMAN]** Confirm session-open flow (greeting → census) under 5 seconds end-to-end.
- [ ] **[HUMAN]** Confirm click-to-expand rationale under 2 seconds.
- [ ] **[HUMAN]** Confirm all other nav bar items (Calendar, Flow, Recalls, Messages, Patient) work normally.
- [ ] **[HUMAN]** Confirm Co-Pilot tab persists when switching to other tabs and back.

### OpenEMR verification (local Docker)

- [ ] **[HUMAN]** Log in as `sara` at `http://localhost:8300` → confirm Co-Pilot nav tab appears and census auto-dispatches within ~5 seconds.
- [ ] **[HUMAN]** Enable module if needed via Admin UI (`http://localhost:8300/interface/modules/zend_modules/public/Installer/`) or `scripts/05-enable-openemr-module.sh` against local MySQL.
- [ ] **[HUMAN]** Confirm sidebar does NOT appear on the OpenEMR login page (unauthenticated — bootstrap skips injection).
- [ ] **[HUMAN]** Confirm Co-Pilot tab persists across OpenEMR page navigation within the same session.

---

## Next — Queued

### Clinician follow-up (after priority table review)

- [ ] Re-grade eval tests whose expected priority levels change based on clinician feedback. Document which tests were re-graded and why in `agent-api/tests/` docstrings.
- [ ] If any threshold changes propagate to Phase 8 routing eval cases, update `agent-api/tests/test_agent_routing.py` accordingly.

### Secret rotation checklist (verify after any credentials are shared or copied)

- [ ] Verify the Anthropic API key in `docker/development-easy/.env.copilot` is the intended dev key; rotate at <https://console.anthropic.com> if any doubt.
- [ ] Rotate the local FHIR OAuth client secret if the local dev instance was shared or the secret was logged.
- [ ] Rotate the Langfuse secret key at <https://cloud.langfuse.com> if the project was shared with others.

---

## Monthly Hygiene

- [ ] Archive any completed items from this file to `docs/archive/` (next sweep: 2026-05-30).
- [ ] Run `detect-secrets scan --baseline .secrets.baseline` and review any new findings.
