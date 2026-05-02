# agent-api OpenAPI contract

`openapi.json` and `openapi.yaml` are checked-in artifacts generated from the
FastAPI app in `agent-api/main.py`. They are the source of truth for the HTTP
contract between the Python service and its consumers.

## Consumers

- **`agent-ui/`** — the standalone clinical co-pilot UI calls these routes
  directly.
- **`interface/modules/custom_modules/oe-module-clinical-copilot/`** — the
  PHP iframe module embedded in OpenEMR consumes the same routes.

## Regenerating

Run from the repo root:

```bash
PYTHONPATH=agent-api python3 agent-api/scripts/dump_openapi.py
```

Or from inside `agent-api/`:

```bash
python3 scripts/dump_openapi.py
```

## When to regenerate

Regenerate **whenever a FastAPI route changes** — new endpoint, removed
endpoint, changed path/method, changed request or response model, or any edit
to a Pydantic schema referenced by a route. Commit the regenerated
`openapi.json` and `openapi.yaml` alongside the code change so consumers see
the new contract in the same PR.
