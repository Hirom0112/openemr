#!/usr/bin/env python3
"""Backfill legacy ``documents`` rows for Co-Pilot DocumentReferences that
were written via FHIR before the Phase 6.1 mirror hook landed.

Reads FHIR ``DocumentReference`` resources for the configured patient
(or all patients with the Co-Pilot LOINC type), downloads the linked
``Binary`` bytes, and POSTs each to the JWT-protected custom-module
``UploadController.php`` so the file shows up in the chart Documents tab.

Idempotency is enforced by a local JSON ledger
(``./.backfill_ledger.json`` by default): each successfully mirrored
DocumentReference id is written to the ledger and skipped on subsequent
runs. ``--dry-run`` reports what *would* be posted without doing it.

Usage::

    # Dry-run for one patient
    python3 -m scripts.backfill_legacy_documents_from_fhir \\
        --patient-id 51 --dry-run

    # Real run, all patients with the Co-Pilot LOINC type
    python3 -m scripts.backfill_legacy_documents_from_fhir

    # Custom ledger path
    python3 -m scripts.backfill_legacy_documents_from_fhir \\
        --ledger /tmp/copilot-backfill-ledger.json

This is a one-shot maintenance utility, not part of the request path —
it is intentionally synchronous-async (``asyncio.run``) and prints to
stdout instead of going through the structured logger.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth.fhir_client import get_access_token  # noqa: E402
from config import settings  # noqa: E402
from documents.fhir_writer import (  # noqa: E402
    _custom_upload_url,
    _extension_for_mime,
    _mint_copilot_jwt,
)

# LOINC codes we treat as Co-Pilot-written documents. ``34109-9`` is the
# UploadController category code; the FHIR DocumentReference path uses
# the per-hint codes from ``_LOINC_BY_HINT`` plus ``_LOINC_DEFAULT``.
COPILOT_LOINC_CODES = {"34109-9", "34108-1", "34105-7", "11502-2"}

DEFAULT_LEDGER_PATH = ROOT / ".backfill_ledger.json"


def _fhir_base() -> str:
    return settings.openemr_base_url.rstrip("/") + "/apis/default/fhir"


def _load_ledger(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("mirrored"), list):
            return {str(x) for x in data["mirrored"]}
    except Exception as exc:  # noqa: BLE001 — best-effort read
        print(f"[warn] could not read ledger {path}: {exc}", file=sys.stderr)
    return set()


def _save_ledger(path: Path, mirrored: set[str]) -> None:
    path.write_text(
        json.dumps({"mirrored": sorted(mirrored)}, indent=2),
        encoding="utf-8",
    )


async def _list_document_references(
    *,
    client: httpx.AsyncClient,
    token: str,
    patient_id: str | None,
) -> list[dict[str, Any]]:
    """Page through DocumentReference, optionally filtered by patient id."""
    url = f"{_fhir_base()}/DocumentReference"
    params: dict[str, str] = {"_count": "100"}
    if patient_id:
        params["patient"] = patient_id

    out: list[dict[str, Any]] = []
    while url:
        resp = await client.get(
            url,
            params=params if params else None,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/fhir+json",
            },
        )
        resp.raise_for_status()
        bundle = resp.json()
        for entry in bundle.get("entry", []) or []:
            res = entry.get("resource") or {}
            if res.get("resourceType") == "DocumentReference":
                out.append(res)
        # Walk pagination via Bundle.link[rel=next]
        next_url = None
        for link in bundle.get("link", []) or []:
            if link.get("relation") == "next" and link.get("url"):
                next_url = link["url"]
                break
        url = next_url
        params = {}  # next link already encodes everything
    return out


def _is_copilot(docref: dict[str, Any]) -> bool:
    """True if a DocumentReference carries one of the Co-Pilot LOINC codes."""
    type_block = docref.get("type") or {}
    for coding in type_block.get("coding", []) or []:
        if str(coding.get("system", "")).lower().startswith("http://loinc.org"):
            if str(coding.get("code", "")) in COPILOT_LOINC_CODES:
                return True
    return False


def _binary_id(docref: dict[str, Any]) -> str | None:
    for content in docref.get("content", []) or []:
        attachment = content.get("attachment") or {}
        url = str(attachment.get("url", ""))
        if url.startswith("Binary/"):
            return url.split("/", 1)[1]
    return None


def _patient_id_from(docref: dict[str, Any]) -> str | None:
    subject = docref.get("subject") or {}
    ref = str(subject.get("reference", ""))
    if ref.startswith("Patient/"):
        return ref.split("/", 1)[1]
    return None


def _mime_from(docref: dict[str, Any]) -> str:
    for content in docref.get("content", []) or []:
        attachment = content.get("attachment") or {}
        mime = str(attachment.get("contentType", "")).strip()
        if mime:
            return mime
    return "application/pdf"


def _title_from(docref: dict[str, Any]) -> str | None:
    for content in docref.get("content", []) or []:
        attachment = content.get("attachment") or {}
        title = attachment.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


async def _fetch_binary(
    *,
    client: httpx.AsyncClient,
    token: str,
    binary_id: str,
) -> bytes:
    url = f"{_fhir_base()}/Binary/{binary_id}"
    resp = await client.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/fhir+json",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data")
    if not isinstance(data, str) or not data:
        raise RuntimeError(f"Binary/{binary_id} returned no data field")
    return base64.b64decode(data)


async def _mirror_one(
    *,
    client: httpx.AsyncClient,
    docref: dict[str, Any],
    fhir_token: str,
    jwt_token: str,
    dry_run: bool,
) -> tuple[bool, str]:
    """Return (success, message)."""
    docref_id = str(docref.get("id", ""))
    pid = _patient_id_from(docref)
    bid = _binary_id(docref)
    if not docref_id or not pid or not bid:
        return False, f"skip {docref_id or '?'}: missing id/patient/binary"

    mime = _mime_from(docref)
    title = _title_from(docref)
    filename = (title or f"copilot-{docref_id}") + _extension_for_mime(mime)

    if dry_run:
        return True, f"DRY-RUN would mirror DocumentReference/{docref_id} pid={pid} mime={mime} file={filename}"

    pdf_bytes = await _fetch_binary(client=client, token=fhir_token, binary_id=bid)

    files = {"file": (filename, pdf_bytes, mime)}
    data: dict[str, str] = {
        "patient_id": pid,
        "fhir_doc_ref_id": docref_id,
    }
    resp = await client.post(
        _custom_upload_url(),
        files=files,
        data=data,
        headers={"Authorization": f"Bearer {jwt_token}"},
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    legacy_id = payload.get("documentId")
    return True, f"mirrored DocumentReference/{docref_id} -> documents.id={legacy_id}"


async def _run(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger).resolve()
    mirrored = _load_ledger(ledger_path)
    print(f"[info] ledger: {ledger_path} ({len(mirrored)} prior entries)")

    jwt_token = _mint_copilot_jwt()
    if jwt_token is None and not args.dry_run:
        print(
            "[error] copilot_jwt_secret unset/short — cannot post to UploadController",
            file=sys.stderr,
        )
        return 2

    fhir_token = await get_access_token(force_refresh=False)

    async with httpx.AsyncClient(timeout=120) as client:
        all_refs = await _list_document_references(
            client=client,
            token=fhir_token,
            patient_id=args.patient_id,
        )
        copilot_refs = [r for r in all_refs if _is_copilot(r)]
        print(
            f"[info] found {len(all_refs)} DocumentReferences "
            f"({len(copilot_refs)} Co-Pilot LOINC matches)"
        )

        ok_count = 0
        skip_count = 0
        fail_count = 0
        for ref in copilot_refs:
            ref_id = str(ref.get("id", ""))
            if ref_id in mirrored:
                skip_count += 1
                continue
            try:
                ok, msg = await _mirror_one(
                    client=client,
                    docref=ref,
                    fhir_token=fhir_token,
                    jwt_token=jwt_token or "",
                    dry_run=args.dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                fail_count += 1
                print(f"[fail] {ref_id}: {exc}", file=sys.stderr)
                continue
            print(f"[ok]   {msg}" if ok else f"[skip] {msg}")
            if ok and not args.dry_run:
                mirrored.add(ref_id)
                ok_count += 1
                # Persist after each success so a mid-run crash doesn't
                # cost us idempotency.
                _save_ledger(ledger_path, mirrored)
            elif ok:
                ok_count += 1

    print(
        f"[done] ok={ok_count} skip={skip_count} fail={fail_count} "
        f"dry_run={args.dry_run}"
    )
    return 0 if fail_count == 0 else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--patient-id",
        default=os.environ.get("BACKFILL_PATIENT_ID") or None,
        help="Restrict to one patient id (FHIR Patient.id). Default: all patients.",
    )
    parser.add_argument(
        "--ledger",
        default=str(DEFAULT_LEDGER_PATH),
        help=f"Path to JSON ledger (default: {DEFAULT_LEDGER_PATH}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be mirrored, don't post.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
