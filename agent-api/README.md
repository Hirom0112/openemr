# Clinical Co-Pilot — Agent API

FastAPI service that powers the Clinical Co-Pilot feature: triage ranking,
query routing, medication safety checks, and clinician briefing generation.

## Running the eval suite

All commands run from the **repo root** (not from inside `agent-api/`).

### Install dependencies

```bash
pip install -r agent-api/requirements.txt pytest
```

### Canonical test command

```bash
python3 -m pytest agent-api/tests
```

pytest resolves `agent-api/pytest.ini` as the rootdir and adds `agent-api/`
to `sys.path` automatically — no `cd` or `PYTHONPATH` gymnastics required.

### Marker-gated subsets

```bash
# Tests that block deploy on any failure (100% pass required)
python3 -m pytest agent-api/tests -m hard_failure

# Tests asserting clinician-meaningful correctness (95% accuracy gate)
python3 -m pytest agent-api/tests -m clinical_accuracy
```

### Useful flags

```bash
# Verify test collection and marker coverage without running
python3 -m pytest agent-api/tests --collect-only

# Show which tests a marker selects
python3 -m pytest agent-api/tests -m hard_failure --collect-only

# Stop on first failure
python3 -m pytest agent-api/tests -x
```

## Marker taxonomy

Every test must carry at least one of these markers or the full-suite run
will fail at collection time (enforced by `tests/conftest.py`).

| Marker | Semantics | Gate |
|---|---|---|
| `hard_failure` | If this test fails, would you block deploy? | 100% pass |
| `clinical_accuracy` | Does this assert something a clinician cares about? | 95% accuracy |

A test may carry both markers.

## Project layout

```
agent-api/
  auth/           — FHIR OAuth token management
  briefing/       — Patient briefing schema and context builder
  checkpointer/   — LangGraph checkpoint persistence
  handoff/        — Async handoff protocol
  medication/     — Medication safety checks
  query/          — FHIR query router
  triage/         — Triage criteria extraction and rules engine
  verification/   — Domain constraint and source attribution verification
  tests/          — Eval suite
    fixtures/     — Static FHIR bundle fixtures
```
