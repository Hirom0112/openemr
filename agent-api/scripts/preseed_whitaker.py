#!/usr/bin/env python3
"""Pre-seed Whitaker (pid 27) with the demo fixture set.

Five fixtures, two tiers:

Tier 1 — rubric core (typed PDFs that exercise the headline modalities):
  - p02-whitaker-intake.pdf   (intake_form modality)
  - p02-whitaker-cbc.pdf      (lab_report modality, table_heavy)

Tier 2 — multimodal breadth (W2 §4 expansion story):
  - p02-whitaker-referral.docx        (docx_referral)
  - p02-whitaker-oru-r01.hl7          (hl7_v2 lab observations)
  - p02-whitaker-workbook.xlsx        (xlsx_workbook)

Idempotency: SHA-512 hash of file bytes is queried against
``documents.hash WHERE foreign_id=<pid>`` before each ingest. The
OpenEMR Document writer populates this column on every upload (see
``library/classes/Document.class.php``), so re-running this script is
a no-op for any fixture already loaded.

Usage::

    # Local
    AGENT_API_URL=http://localhost:8400 \\
    MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306 \\
    MYSQL_USER=openemr MYSQL_PASS=openemr MYSQL_DB=openemr \\
    python3 -m scripts.preseed_whitaker

    # Railway (mariadb-proxy host + creds from `railway variables -s MySQL`)
    AGENT_API_URL=https://copilot-agent-api-production.up.railway.app \\
    MYSQL_HOST=<railway-proxy> MYSQL_PORT=<port> \\
    MYSQL_USER=root MYSQL_PASS=<root-pass> MYSQL_DB=openemr \\
    python3 -m scripts.preseed_whitaker

    # Dry-run (compute hashes, check DB, don't POST)
    python3 -m scripts.preseed_whitaker --dry-run

This is a one-shot demo-prep utility, not part of the request path.
Synchronous, prints to stdout, exits non-zero on the first hard failure
so the operator notices.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import jwt as pyjwt
import pymysql

DEFAULT_PATIENT_ID = 27
INGEST_TIMEOUT_SECONDS = 120.0
TOKEN_TTL_SECONDS = 600  # 10 min — long enough to load 5 fixtures sequentially.


def _mint_token(secret: str, *, provider_id: str = "system") -> str:
    """Mint an HS256 JWT the agent-api jwt_middleware accepts.

    Required claims (per agent-api/auth/jwt_middleware.py): iss must be
    ``openemr-copilot``, sub is the principal id, iat + exp present.
    The middleware bypasses auth entirely when COPILOT_JWT_SECRET is
    empty on the server, so an empty secret here means we don't bother
    sending the header at all.
    """
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": "openemr-copilot",
            "sub": provider_id,
            "provider_id": provider_id,
            "sid": f"preseed-{now}",
            # 'admin' satisfies _require_mutating_role on the staging
            # router (clinician|admin allowed) so the auto-approval call
            # downstream lands writes into copilot_observations /
            # copilot_conditions instead of leaving them pending.
            "role": "admin",
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
        },
        secret,
        algorithm="HS256",
    )


@dataclass(frozen=True)
class Fixture:
    """One demo fixture to ingest.

    ``relative_path`` is resolved against the agent-api repo root
    (i.e. the directory two levels above this file). ``mime_type`` is
    sent in the multipart upload — the agent-api dispatcher detects
    format from bytes regardless, but a correct mime helps the legacy
    Documents-tab mirror infer extension. ``doc_type_hint`` is set for
    the typed-PDF tier-1 fixtures so the extractor's classifier picks
    the right schema on the happy path.
    """

    name: str
    relative_path: str
    mime_type: str
    doc_type_hint: Optional[str]
    tier: str


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        name="whitaker-intake",
        relative_path="tests/fixtures/eval/real-examples/p02-whitaker-intake.pdf",
        mime_type="application/pdf",
        doc_type_hint="intake_form",
        tier="core",
    ),
    Fixture(
        name="whitaker-cbc",
        relative_path="tests/fixtures/eval/real-examples/p02-whitaker-cbc.pdf",
        mime_type="application/pdf",
        doc_type_hint="lab_report",
        tier="core",
    ),
    Fixture(
        name="whitaker-referral",
        relative_path="tests/fixtures/w2/multimodal/docx/p02-whitaker-referral.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        doc_type_hint=None,
        tier="breadth",
    ),
    Fixture(
        name="whitaker-oru-r01",
        relative_path="tests/fixtures/w2/multimodal/hl7v2/p02-whitaker-oru-r01.hl7",
        mime_type="application/hl7-v2",
        doc_type_hint=None,
        tier="breadth",
    ),
    Fixture(
        name="whitaker-workbook",
        relative_path="tests/fixtures/w2/multimodal/xlsx/p02-whitaker-workbook.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        doc_type_hint=None,
        tier="breadth",
    ),
)


def _agent_api_root() -> Path:
    """Repo root for agent-api (one level above scripts/)."""
    return Path(__file__).resolve().parent.parent


def _read_fixture(fx: Fixture) -> bytes:
    path = _agent_api_root() / fx.relative_path
    if not path.is_file():
        raise FileNotFoundError(f"Fixture missing on disk: {path}")
    return path.read_bytes()


def _sha512_hex(blob: bytes) -> str:
    """Match OpenEMR's Document::makeDocumentHash convention (sha512 hex)."""
    return hashlib.sha512(blob).hexdigest()


def _open_mysql() -> pymysql.connections.Connection:
    """Open a MySQL connection from MYSQL_* env vars.

    Raises with a clear message if anything is missing — this script is
    operator-run, so a fast loud failure is the right behavior.
    """
    required = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASS")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASS"],
        database=os.environ.get("MYSQL_DB", "openemr"),
        autocommit=True,
        charset="utf8mb4",
    )


def _hash_already_seeded(
    conn: pymysql.connections.Connection,
    *,
    patient_id: int,
    file_hash_hex: str,
) -> bool:
    """True iff a documents row exists for this patient with this hash.

    OpenEMR's documents table populates ``hash`` on every upload via the
    Document writer (sha512 hex string). The ``foreign_id`` column ties
    the document to the patient (pid). Pair is the unique idempotency
    key we want — a re-upload of the same fixture for the same patient
    is a no-op.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM documents WHERE foreign_id=%s AND hash=%s LIMIT 1",
            (patient_id, file_hash_hex),
        )
        return cur.fetchone() is not None


def _ingest(
    *,
    agent_api_url: str,
    fx: Fixture,
    file_bytes: bytes,
    patient_id: int,
    bearer_token: Optional[str],
) -> dict:
    """POST the fixture to /document/ingest. Returns the JSON response."""
    url = f"{agent_api_url.rstrip('/')}/document/ingest"
    files = {"file": (Path(fx.relative_path).name, file_bytes, fx.mime_type)}
    data: dict[str, str] = {"patient_id": str(patient_id)}
    if fx.doc_type_hint is not None:
        data["doc_type_hint"] = fx.doc_type_hint
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    with httpx.Client(timeout=INGEST_TIMEOUT_SECONDS) as client:
        response = client.post(url, data=data, files=files, headers=headers)
        response.raise_for_status()
        return response.json()


def _batch_approve(
    *,
    agent_api_url: str,
    pending_ids: list[int],
    bearer_token: Optional[str],
) -> dict:
    """Approve staged extractions so the writer fires.

    Without this, /document/ingest returns rows in 'pending' state and
    nothing lands in copilot_observations / copilot_conditions — the
    dashboard sidebar cards stay empty even though the documents
    themselves are visible in the chart Documents tab. The token must
    carry role in {clinician, admin} for the staging router to accept
    the mutation.
    """
    url = f"{agent_api_url.rstrip('/')}/pending-extractions/batch-approve"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    with httpx.Client(timeout=INGEST_TIMEOUT_SECONDS) as client:
        response = client.post(url, json={"ids": pending_ids}, headers=headers)
        response.raise_for_status()
        return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patient-id",
        type=int,
        default=DEFAULT_PATIENT_ID,
        help=f"Target OpenEMR pid (default {DEFAULT_PATIENT_ID} = Whitaker)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute hashes + check idempotency but do not POST",
    )
    parser.add_argument(
        "--tier",
        choices=("core", "breadth", "all"),
        default="all",
        help="Subset of fixtures to attempt (default: all)",
    )
    args = parser.parse_args()

    agent_api_url = os.environ.get("AGENT_API_URL", "http://localhost:8400")
    copilot_jwt_secret = os.environ.get("COPILOT_JWT_SECRET", "")
    bearer_token: Optional[str] = (
        _mint_token(copilot_jwt_secret) if copilot_jwt_secret else None
    )
    selected = (
        FIXTURES
        if args.tier == "all"
        else tuple(f for f in FIXTURES if f.tier == args.tier)
    )

    print(f"[preseed] target pid={args.patient_id}, agent-api={agent_api_url}")
    print(
        f"[preseed] tier={args.tier}, fixtures={len(selected)}, "
        f"dry_run={args.dry_run}, jwt_auth={'yes' if bearer_token else 'no'}"
    )

    try:
        conn = _open_mysql()
    except Exception as exc:
        print(f"[preseed] ERROR opening MySQL: {exc}", file=sys.stderr)
        return 2

    skipped: list[str] = []
    ingested: list[str] = []
    failed: list[tuple[str, str]] = []
    pending_ids: list[int] = []
    try:
        for fx in selected:
            try:
                file_bytes = _read_fixture(fx)
            except Exception as exc:
                print(f"[preseed] {fx.name}: SKIP (fixture read failed: {exc})")
                failed.append((fx.name, f"read failed: {exc}"))
                continue

            file_hash = _sha512_hex(file_bytes)
            already = _hash_already_seeded(
                conn, patient_id=args.patient_id, file_hash_hex=file_hash
            )
            print(
                f"[preseed] {fx.name}: bytes={len(file_bytes)}, "
                f"hash={file_hash[:16]}…, "
                f"already_seeded={already}"
            )

            if already:
                skipped.append(fx.name)
                continue
            if args.dry_run:
                print(f"[preseed] {fx.name}: DRY-RUN — would ingest")
                continue

            try:
                result = _ingest(
                    agent_api_url=agent_api_url,
                    fx=fx,
                    file_bytes=file_bytes,
                    patient_id=args.patient_id,
                    bearer_token=bearer_token,
                )
            except httpx.HTTPStatusError as exc:
                body = (exc.response.text or "")[:500]
                print(
                    f"[preseed] {fx.name}: HTTP {exc.response.status_code} from agent-api: {body}",
                    file=sys.stderr,
                )
                failed.append((fx.name, f"HTTP {exc.response.status_code}"))
                continue
            except Exception as exc:
                print(
                    f"[preseed] {fx.name}: unexpected error: {exc}",
                    file=sys.stderr,
                )
                failed.append((fx.name, str(exc)))
                continue

            doc_ref = (
                result.get("document_reference_id")
                or result.get("documentReferenceId")
                or result.get("metadata", {}).get("document_reference_id")
                or "<unknown>"
            )
            staging = result.get("metadata", {}).get("staging") or {}
            pending = list(staging.get("pending_extraction_ids") or [])
            print(
                f"[preseed] {fx.name}: INGESTED, doc_ref={doc_ref}, "
                f"pending_extractions={len(pending)}"
            )
            ingested.append(fx.name)
            pending_ids.extend(pending)
    finally:
        conn.close()

    # Auto-approve every pending extraction we created so the writer
    # populates copilot_observations / copilot_conditions and the
    # dashboard cards have data to render. Skipped under --dry-run.
    approved_ok = 0
    approved_fail = 0
    if pending_ids and not args.dry_run:
        print()
        print(f"[preseed] approving {len(pending_ids)} pending extractions…")
        try:
            ar = _batch_approve(
                agent_api_url=agent_api_url,
                pending_ids=pending_ids,
                bearer_token=bearer_token,
            )
            for item in ar.get("results") or []:
                if item.get("state") == "written":
                    approved_ok += 1
                else:
                    approved_fail += 1
                    print(
                        f"[preseed]   pending_id={item.get('pending_id')}: "
                        f"state={item.get('state')} "
                        f"error={item.get('error') or item.get('write_error')}"
                    )
            print(f"[preseed] approval results: written={approved_ok}, other={approved_fail}")
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "")[:500]
            print(
                f"[preseed] batch-approve HTTP {exc.response.status_code}: {body}",
                file=sys.stderr,
            )
            failed.append(("batch-approve", f"HTTP {exc.response.status_code}"))
        except Exception as exc:
            print(f"[preseed] batch-approve unexpected error: {exc}", file=sys.stderr)
            failed.append(("batch-approve", str(exc)))

    print()
    print(
        f"[preseed] summary: ingested={len(ingested)}, skipped={len(skipped)}, "
        f"failed={len(failed)}, approved_written={approved_ok}, "
        f"approved_other={approved_fail}"
    )
    if ingested:
        print(f"[preseed]   ingested: {', '.join(ingested)}")
    if skipped:
        print(f"[preseed]   skipped (already seeded): {', '.join(skipped)}")
    if failed:
        print(f"[preseed]   failed: {json.dumps(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
