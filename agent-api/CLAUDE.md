# Ground yourself before acting

This file governs Co-Pilot work — anything under `agent-api/`, `agent-ui/`, or
`interface/modules/custom_modules/oe-module-clinical-copilot/`. The root
`CLAUDE.md` governs *how* you execute (orchestrator role, PHP standards,
PHPStan, commit conventions); this file governs *what you're executing
against*. Read both. The two stack — neither replaces the other.

The Co-Pilot is an AI agent embedded in a forked OpenEMR helping a hospitalist
(Dr. Sarah Chen) get patient context in the 90-second window between rounding
rooms. A confidently-stated hallucination here doesn't just damage trust — it
can directly harm a patient. The verification layer, deterministic triage
rules engine, and eval gate exist to catch that failure mode upstream. Code
written against a stale mental model bypasses them silently.

## Layer 1 — Design docs (intent)

- `W1_ARCHITECTURE.md`, `W2_ARCHITECTURE.md` — system design, dispatcher,
  verification, eval gates, W2 graph + RAG
- `USERS.md` — Dr. Chen, UC-1..UC-5, ranking weights (UC-1 is rules-driven,
  not LLM)
- `W1_AUDIT.md` — why FHIR-only, why Redis pre-fetch, synthetic-data constraint
- `todo.md` (repo root, gitignored as personal working plan — `b645131e4`) — phase tracking + slice status + cut-line decisions. Not present on fresh clones; ask the operator for current state if absent.
- `Week_1__AgentForge2.pdf` — requirements floor

## Layer 2 — Code (reality)

The docs lag the code. If a doc and code disagree, verify against source.
Useful entry points:

- `agent-api/agent/tools/__init__.py`, `agent/tool_registry.py` — actual tools
- `agent-api/main.py` — actual routes (`/agent/query`,
  `/agent/triage_rationale/{patient_id}`, W2 routes incl. `/agent/w2/dispatch`,
  `/document/ingest`, `/document/post-ingest-context`)
- `agent-api/agent/dispatcher.py` — tool_use loop, response envelope
- `agent-api/verification/` — source attribution + domain constraints
- `agent-api/graph/`, `documents/`, `extractors/`, `rag/` — W2 (LangGraph
  supervisor + workers + critic; document ingest + OCR; hybrid sparse+dense
  retrieval + Cohere rerank)
- `agent-api/tests/conftest.py`, `pytest.ini` — marker enforcement, gate config
- `agent-api/.importlinter` — architectural boundary contracts (run
  `cd agent-api && lint-imports` before any PR touching agent-api)
- `interface/modules/custom_modules/oe-module-clinical-copilot/` — PHP shell;
  `public/upload.php` and `public/observation.php` are the v1 workarounds
  for FHIR `Binary` POST 404 and legacy REST `/api/patient/{pid}/document`
  401-on-OAuth in this OpenEMR build
- `agent-ui/` — React panel (compiled into the module's `public/copilot.js`)
- `docker/development-easy/docker-compose.yml` — service map

## Known drift (re-verify; do not trust this list as eternal)

Verified against code at the time of writing. Drift accumulates — re-check
with `grep` before citing. Many of the entries below have been actively
reconciled in the W1/W2 docs; see `DOCS-DRIFT-AUDIT.md` at the repo root
for the systematic pass. Re-grep before treating any entry as still
outstanding.

- W1_ARCHITECTURE §4.5 originally listed 5 tools; registry has 6.
  `get_triage_rationale` is defined at `agent/tools/__init__.py:1091` and
  registered as direct-call only in `agent/tool_registry.py`
  (`DIRECT_TOOL_REGISTRY`, line 34) — intentionally excluded from the
  dispatcher's `TOOL_REGISTRY`. **Reconciled in Item 8 of the doc-drift pass.**
- W1_ARCHITECTURE §6.1 originally said "47 test cases organized into five
  categories" with no marker language. Reality: `tests/conftest.py:21`
  enforces `REQUIRED_MARKERS = {"hard_failure", "clinical_accuracy"}` and
  fails collection on any unmarked test (`conftest.py:49-67`).
  `pytest.ini:9-10` declares both gates (100% / 95%). The latency category
  in §6.1 has no corresponding marker in code. **Reconciled in Item 7;
  latency annotation in Item 17.**
- W1_ARCHITECTURE §4.6 (HTTP Routes) now exists and enumerates 32 routes
  (26 in `main.py` via `@app.*` decorators + 6 in `staging/router.py`
  mounted via `app.include_router(_staging_router)` at `main.py:3657`).
  The two canonical agent endpoints: `/agent/triage_rationale/{patient_id}`
  at `main.py:816` and `/agent/query` at `main.py:841`. **Reconciled in
  Item 10.**
- W1_ARCHITECTURE §4.4 prohibition wording (verbatim): "All LLM calls use
  Anthropic's tool_use response format to produce machine-parseable output.
  Free-text prose responses are not accepted for any use case." This is the
  authoritative phrasing — prefer it over paraphrase.
- W1_ARCHITECTURE §8.1 originally said LangGraph is a v2 trigger ("the
  first commit that implements a second agent type"). Reality:
  `requirements.txt:62` pins `langgraph==0.2.60`;
  `agent-api/graph/{build.py,state.py,nodes/}` exists; the W2 doc-ingest
  graph is shipped. Both true: W1 dispatcher = raw Anthropic SDK + custom
  Checkpointer; W2 doc-ingest = LangGraph. **Reconciled in Item 9.**
- W2 ingestion uses custom JWT-protected upload + custom Observation
  `derivedFrom` endpoints (`oe-module-clinical-copilot/public/upload.php`,
  `public/observation.php`) because in this OpenEMR build, FHIR `Binary` POST
  returns 404 (resource not registered) and legacy REST
  `/api/patient/{pid}/document` returns 401 (OAuth scope drop + ACL gate).
  W2_ARCHITECTURE.md §4.2.1 documents this v1 deviation; W1_ARCHITECTURE
  §4.7 now cross-references it. **Reconciled in Item 11.**

## Rules

1. Read both layers before answering anything architectural or behavioral.
2. Cite both when relevant: "ARCH §X says A — code reality at `path:line` is B."
3. Distinguish (a) shipped + doc-consistent, (b) shipped but doc stale,
   (c) designed but not built. TODO.md phase status is the build log.
4. Distinguish v1 (built/in flight) from v2 (descoped). ARCH §8 + W2 §8 gap
   tables are authoritative if current; cross-check against TODO.md.
5. If the user contradicts the docs or code, flag it. Don't assume either
   side is right.
6. If neither docs nor code answer it, say "not grounded" and propose what
   needs to be added. Don't invent.
7. Push back when the user is about to break a constraint the verification
   layer, eval gate, failure-isolation rule, or audit findings exist to
   enforce. UC-1 ranking is deterministic by design (USERS.md §5 "Ranking
   Priority Weights"); the LLM only writes one-line explanations after rules
   rank. Do not propose LLM-driven ranking.

In any new session, before answering anything substantive: read both layers,
then demonstrate (in whatever format fits) that you can speak to the
dispatcher loop, verification layer, deterministic triage rules engine, W2
graph, citation contract, and at least one drift you found in this read.
Then ask what to dig into.

Skipping the read produces subtle wrongness that matters in a clinical
setting. Don't skip.
