"""Dump the FastAPI app's OpenAPI schema to JSON and YAML.

Run from the repo root:

    PYTHONPATH=agent-api python3 agent-api/scripts/dump_openapi.py

Or from inside agent-api/:

    python3 scripts/dump_openapi.py

Outputs:
  - agent-api/openapi.json
  - agent-api/openapi.yaml  (skipped if PyYAML is not installed)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make sure agent-api/ is importable so `from main import app` works whether
# we're run from the repo root or from inside agent-api/.
HERE = Path(__file__).resolve().parent
AGENT_API_DIR = HERE.parent
if str(AGENT_API_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_API_DIR))


def main() -> int:
    from main import app  # noqa: PLC0415  (deferred so sys.path is set first)

    schema = app.openapi()

    json_path = AGENT_API_DIR / "openapi.json"
    json_path.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n")
    print(f"wrote {json_path} ({json_path.stat().st_size} bytes)")

    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        print("PyYAML not installed; skipping openapi.yaml")
        return 0

    yaml_path = AGENT_API_DIR / "openapi.yaml"
    yaml_path.write_text(yaml.safe_dump(schema, sort_keys=False))
    print(f"wrote {yaml_path} ({yaml_path.stat().st_size} bytes)")

    paths = list(schema.get("paths", {}).keys())
    schemas = list(schema.get("components", {}).get("schemas", {}).keys())
    print(f"paths: {len(paths)}  schemas: {len(schemas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
