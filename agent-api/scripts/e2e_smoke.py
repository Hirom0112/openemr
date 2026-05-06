#!/usr/bin/env python3
"""E2E smoke harness for /agent/query (Phase 3 Wave 3).

Why: the W2 Eval Suite is the only automated path that exercises the real
graph today, and it costs ~$15-30 of Anthropic spend per run. This script
is a $0 smoke that hits a *running* agent-api over HTTP with five
representative query payloads and asserts the live contract still holds.

PRECONDITIONS (operator must satisfy before invoking):
  1. agent-api running locally at $AGENT_API_URL (default localhost:8400).
  2. Patient records seeded for each manifest entry's ``patient_id``. Use
     OpenEMR's fixtures + the FHIR ingest path; this script does NOT seed.
  3. ANTHROPIC_API_KEY set in the agent-api environment.

USAGE:
  python -m scripts.e2e_smoke                  # 5 fixtures, default URL
  AGENT_API_URL=http://localhost:8400 \\
    python -m scripts.e2e_smoke --verbose      # printed payload + response

EXIT CODES:
  0 — every manifest entry passed
  1 — at least one entry failed (HTTP non-2xx, missing citations, or
       /health unreachable within the probe timeout)
  2 — invocation problem (bad CLI args, JSON parse error)

This harness is deliberately NOT wired into copilot-eval.yml; it's only
exposed via a workflow_dispatch job in e2e-smoke.yml so it never burns
Anthropic credits on every push. Run it locally before merging Phase 3
work to validate the full /agent/query path end-to-end.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_URL = os.environ.get("AGENT_API_URL", "http://localhost:8400")
HEALTH_PROBE_TIMEOUT_S = 3.0
QUERY_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class SmokeFixture:
    """One smoke entry. ``patient_id`` must already be seeded in the running
    agent-api before this script is invoked."""
    name: str
    doc_class: str
    message: str
    patient_id: str


# Five representative entries — one per modality bucket from the W2 Eval Suite.
# patient_ids are placeholders; operator overrides via $E2E_SMOKE_MANIFEST
# (a JSON file with the same SmokeFixture shape).
DEFAULT_MANIFEST: tuple[SmokeFixture, ...] = (
    SmokeFixture(
        name="intake_form",
        doc_class="intake_form",
        message="Summarize this patient's most recent intake form, including allergies and DNR status.",
        patient_id="smoke-patient-intake-001",
    ),
    SmokeFixture(
        name="typed_pdf",
        doc_class="lab_report_tabular",
        message="What were this patient's most recent lab results? Cite the source row for each value.",
        patient_id="smoke-patient-typed-001",
    ),
    SmokeFixture(
        name="multi_column",
        doc_class="other_narrative",
        message="Pull all medications listed in this patient's discharge summary with their citations.",
        patient_id="smoke-patient-multicol-001",
    ),
    SmokeFixture(
        name="scanned_pdf",
        doc_class="other_narrative",
        message="Extract the chief complaint from the most recent scanned consult note.",
        patient_id="smoke-patient-scanned-001",
    ),
    SmokeFixture(
        name="table_heavy",
        doc_class="lab_report_tabular",
        message="List every abnormal value in the most recent comprehensive metabolic panel with the source row.",
        patient_id="smoke-patient-table-001",
    ),
)


def _load_manifest() -> tuple[SmokeFixture, ...]:
    path = os.environ.get("E2E_SMOKE_MANIFEST")
    if not path:
        return DEFAULT_MANIFEST
    try:
        rows = json.loads(open(path, "r").read())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: failed to read manifest at {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    return tuple(SmokeFixture(**row) for row in rows)


def _probe_health(base_url: str) -> tuple[bool, str]:
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/health", timeout=HEALTH_PROBE_TIMEOUT_S)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        return True, "ok"
    except httpx.HTTPError as exc:
        return False, f"unreachable: {exc.__class__.__name__}: {exc}"


def _run_one(base_url: str, fixture: SmokeFixture, *, verbose: bool) -> tuple[bool, str]:
    payload = {
        "message": fixture.message,
        "session_id": f"e2e-smoke-{fixture.name}-{int(time.time())}",
        "provider_id": "e2e-smoke-provider",
        "patient_ids": [fixture.patient_id],
        "provider_name": "E2E Smoke",
    }
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/agent/query",
            json=payload,
            timeout=QUERY_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        return False, f"transport error: {exc.__class__.__name__}: {exc}"

    if verbose:
        print(f"  request:  {json.dumps(payload)}")
        print(f"  response: HTTP {resp.status_code} ({len(resp.content)} bytes)")

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except json.JSONDecodeError:
        return False, "response was not JSON"

    citations = body.get("citations") or body.get("data", {}).get("citations") or []
    if not isinstance(citations, list) or not citations:
        return False, "no citations[] in response"

    has_locator = any(
        isinstance(c, dict) and (c.get("bbox") or c.get("polygon") or c.get("page"))
        for c in citations
    )
    if not has_locator:
        return False, "citations present but none carry bbox/polygon/page locator"

    return True, f"{len(citations)} citation(s)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"agent-api base URL (default: {DEFAULT_URL})")
    parser.add_argument("--verbose", action="store_true", default=False,
                        help="Print request/response details for each fixture")
    args = parser.parse_args(argv)

    print(f"E2E smoke against {args.url}")

    ok, reason = _probe_health(args.url)
    if not ok:
        print(f"FAIL  /health probe: {reason}")
        print("\nGATE: FAIL (service unreachable; bring agent-api up first)")
        return 1
    print(f"PASS  /health probe: {reason}")

    manifest = _load_manifest()
    failures: list[str] = []
    for fixture in manifest:
        print(f"\n→ {fixture.name} ({fixture.doc_class})")
        passed, msg = _run_one(args.url, fixture, verbose=args.verbose)
        if passed:
            print(f"  PASS  {msg}")
        else:
            print(f"  FAIL  {msg}")
            failures.append(f"{fixture.name}: {msg}")

    print(f"\n──── Summary ────")
    print(f"{len(manifest) - len(failures)}/{len(manifest)} passed")
    for f in failures:
        print(f"  - {f}")
    if failures:
        print("\nGATE: FAIL")
        return 1
    print("\nGATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
