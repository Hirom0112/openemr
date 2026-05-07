"""Pre-extraction patient resolver (W2_ARCHITECTURE Slice 9.2).

Pure module — no I/O, no logging — sibling to ``check.py``. The caller
(HTTP layer in ``main.py``) supplies an async FHIR search callable; the
resolver is responsible only for:

  1. Probing the inbound document for an identity hint (per format).
  2. Translating that hint into a single FHIR ``Patient?identifier=...``
     (and optionally ``Patient?given=&family=&birthdate=``) query.
  3. Returning a discriminated :class:`ResolverOutcome` — ``Pass`` or
     ``Quarantine``.

Soft-resolve safety contract (locked, do not relitigate):

  Soft-resolve (name+DOB lookup, MRN absent or unreadable) is allowed
  to return ``Pass`` ONLY when ALL FOUR of these conditions hold:

    1. exact normalized name match against the chart Patient,
    2. exact ISO ``YYYY-MM-DD`` DOB match against the chart Patient,
    3. the FHIR search was issued with ``_count=2`` and returned exactly
       one entry (so a hidden second candidate cannot silently win), and
    4. the extracted name and DOB came from the same document region
       (probes set ``parsed_identity['_same_region']=True`` only when
       this is structurally guaranteed by the source format — e.g. PID-5
       and PID-7 in the same HL7 PID segment).

If any of those fail we quarantine. The post-extraction §5.6 demographic
check (``demographics.check.check_demographics``) re-validates after OCR
runs — when called for a soft-resolve match the §5.6 caller MUST upgrade
any soft-warn or hard-block into a quarantine route-back with reason
``SOFT_RESOLVE_INVALIDATED`` (see ``quarantine.invalidate_soft_resolve``).

Format probes for XLSX / DOCX / TIFF are stubbed in this slice and return
``None``. Slices 9.5 (XLSX), 9.6 (DOCX/TIFF) plug their parsers in at the
documented hook points; the resolver decision matrix does not change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Union


# ── Outcome types ────────────────────────────────────────────────────────────

ReasonCode = Literal[
    "MRN_NOT_FOUND",
    "MRN_AMBIGUOUS",
    "MRN_UNREADABLE",
    "NO_IDENTITY_HINTS",
    "NAME_DOB_NOT_FOUND",
    "NAME_DOB_AMBIGUOUS",
    "NAME_DOB_REGION_MISMATCH",
    "MISSING_DOB",
    "PROBE_FAILED",
    "SOFT_RESOLVE_INVALIDATED",
]

MatchSource = Literal["mrn_exact", "name_dob_soft", "supplied"]


@dataclass(frozen=True)
class Pass:
    """Resolver picked a single chart patient with sufficient confidence."""

    patient_id: str
    source: MatchSource
    candidates_seen: int = 1


@dataclass(frozen=True)
class Quarantine:
    """Resolver could not pick a unique chart patient — caller must
    write a ``copilot_quarantined_documents`` row and return HTTP 202."""

    reason_code: ReasonCode
    candidate_hints: list[dict[str, Any]] = field(default_factory=list)


ResolverOutcome = Union[Pass, Quarantine]


# ── Parsed-identity shape ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedIdentity:
    """What a per-format probe extracted from the inbound document.

    ``mrn`` semantics mirror :mod:`demographics.check`: the sentinel string
    ``"UNREADABLE"`` means the probe saw an MRN region but could not transcribe
    it; ``None`` means no MRN region was present at all.

    ``same_region`` is True only when the source format structurally
    guarantees that ``name`` and ``dob`` came from the same physical /
    logical region (e.g. PID-5 and PID-7 in one HL7 PID segment, or one row
    of an XLSX Patient sheet). The flag is consulted by soft-resolve and
    MUST default False whenever the probe assembled fields from separate
    regions.
    """

    mrn: str | None = None
    name: str | None = None
    dob: str | None = None  # ISO YYYY-MM-DD
    format: str = "unknown"
    same_region: bool = False
    raw_hint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mrn": self.mrn,
            "name": self.name,
            "dob": self.dob,
            "format": self.format,
            "same_region": self.same_region,
            "raw_hint": dict(self.raw_hint),
        }


# ── Per-format probes ────────────────────────────────────────────────────────


_HL7_PID_LINE_RE = re.compile(rb"(?:^|[\r\n])(PID[^\r\n]*)")
_DATE_8_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def _hl7_split_field(field: bytes, sep: bytes = b"^") -> list[bytes]:
    return field.split(sep)


def probe_hl7_v2(raw: bytes) -> ParsedIdentity | None:
    """Minimal HL7 v2 PID probe.

    Slice 9.2 deliberately avoids depending on hl7apy — Slice 9.4 owns
    that. We extract just enough for identity resolution:

      * PID-3.1 → MRN (first ``^``-component of the first ``|``-field
        AFTER ``PID``; see HL7 v2 PID grammar).
      * PID-5.1 → family name, PID-5.2 → given name (both first
        component of the first repetition).
      * PID-7   → DOB; YYYYMMDD or YYYYMMDDHHMM, normalised to
        ``YYYY-MM-DD``.

    Fixtures lack ``\\r`` segment terminators (single-line concat); the
    line-level regex tolerates either ``\\r`` or ``\\n``. We do NOT
    inject ``\\r`` here — Slice 9.4's pre-parse normalizer owns that.

    Returns ``None`` when no PID segment is present (caller treats this
    as ``probe failed``).
    """
    if not raw:
        return None
    m = _HL7_PID_LINE_RE.search(raw)
    if not m:
        return None
    pid_line = m.group(1)
    fields = pid_line.split(b"|")
    # PID-3 sits at index 3 (PID-1 at 1, PID-2 at 2). PID-3 may carry
    # multiple repetitions separated by '~'; take the first repetition.
    mrn: str | None = None
    if len(fields) > 3:
        rep = fields[3].split(b"~", 1)[0]
        components = _hl7_split_field(rep)
        if components and components[0]:
            try:
                mrn = components[0].decode("utf-8", errors="replace").strip() or None
            except Exception:
                mrn = None

    # PID-5 family^given^...
    name: str | None = None
    if len(fields) > 5:
        rep = fields[5].split(b"~", 1)[0]
        components = _hl7_split_field(rep)
        if len(components) >= 2:
            try:
                family = components[0].decode("utf-8", errors="replace").strip()
                given = components[1].decode("utf-8", errors="replace").strip()
                if family or given:
                    name = (f"{given} {family}").strip() or None
            except Exception:
                name = None

    # PID-7 DOB
    dob: str | None = None
    if len(fields) > 7:
        try:
            raw_dob = fields[7].decode("utf-8", errors="replace").strip()
        except Exception:
            raw_dob = ""
        dm = _DATE_8_RE.match(raw_dob) if raw_dob else None
        if dm:
            dob = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"

    if mrn is None and name is None and dob is None:
        return None

    return ParsedIdentity(
        mrn=mrn,
        name=name,
        dob=dob,
        format="hl7_v2",
        # PID-5 and PID-7 are the same PID segment, so name+DOB are
        # structurally co-located — soft-resolve gate condition (4) holds.
        same_region=(name is not None and dob is not None),
        raw_hint={"segment": "PID"},
    )


def probe_xlsx(raw: bytes) -> ParsedIdentity | None:  # noqa: ARG001 — Slice 9.5 plug-in
    """Stub.

    Slice 9.5 (XLSX parser) plugs in here: read ``Patient`` sheet vertical
    key/value pairs, return a ``ParsedIdentity`` with ``format='xlsx'`` and
    ``same_region=True`` when MRN/name/DOB all live in the same ``Patient``
    sheet. Until then the resolver treats XLSX as ``no identity hints``.
    """
    return None


def probe_docx(raw: bytes) -> ParsedIdentity | None:  # noqa: ARG001 — Slice 9.6 plug-in
    """Stub.

    Slice 9.6 (DOCX loader) plugs in here: ``RE:`` regex on the first
    paragraphs surfaces patient name + DOB; MRN may or may not be present.
    Soft-resolve over DOCX is permitted only when the regex finds name +
    DOB in adjacent runs of the same paragraph (set ``same_region=True``).
    """
    return None


def probe_tiff(raw: bytes) -> ParsedIdentity | None:  # noqa: ARG001 — Slice 9.6 plug-in
    """Stub.

    Slice 9.6 (TIFF loader + fax preprocess) plugs in here: page-1
    cover-strip OCR over the demographic banner. ``same_region=True`` when
    MRN/name/DOB land within the same OCR'd cover-strip region.
    """
    return None


def probe_pdf(raw: bytes) -> ParsedIdentity | None:  # noqa: ARG001
    """Stub.

    PDF identity probing is out of scope for Slice 9.2 — the existing
    /document/ingest pipeline still requires a supplied ``patient_id`` for
    PDFs (the contract change for this slice ONLY affects the structured
    + doc-lane formats added in Phase 9). Returning ``None`` lets the
    caller decision matrix branch on ``NO_IDENTITY_HINTS``.
    """
    return None


# ── Format dispatch ──────────────────────────────────────────────────────────


def probe(raw: bytes, *, format_hint: str) -> ParsedIdentity | None:
    """Dispatch to the per-format probe by hint string.

    ``format_hint`` is one of ``hl7_v2``, ``xlsx``, ``docx``, ``tiff``,
    ``pdf``. Unknown hints route to ``None`` (treated as
    ``NO_IDENTITY_HINTS`` by the caller).
    """
    fmt = (format_hint or "").lower()
    if fmt in {"hl7", "hl7_v2", "hl7v2"}:
        return probe_hl7_v2(raw)
    if fmt == "xlsx":
        return probe_xlsx(raw)
    if fmt == "docx":
        return probe_docx(raw)
    if fmt in {"tiff", "tif"}:
        return probe_tiff(raw)
    if fmt == "pdf":
        return probe_pdf(raw)
    return None


# ── FHIR-shape helpers ───────────────────────────────────────────────────────

# Lookup callable signature: takes a {"resource": str, "params": dict[str, str]}
# request and returns the FHIR Bundle dict (or raises). The HTTP layer wires
# this to ``auth.fhir_client.fhir_client.search`` so the resolver itself
# stays free of the ``auth`` import (per .importlinter ``demographics-isolated``
# contract).
FhirSearch = Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]]


def _wsclean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _bundle_entries(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    entries = bundle.get("entry") or []
    return [e for e in entries if isinstance(e, dict)]


def _resource_name(resource: dict[str, Any]) -> str | None:
    """Build a flattened ``given family`` name from a FHIR Patient.name[0]."""
    names = resource.get("name") or []
    if not isinstance(names, list) or not names:
        return None
    first = names[0]
    if not isinstance(first, dict):
        return None
    given = first.get("given") or []
    family = first.get("family") or ""
    given_str = " ".join(g for g in given if isinstance(g, str)).strip()
    family_str = family.strip() if isinstance(family, str) else ""
    full = f"{given_str} {family_str}".strip()
    return full or None


def _resource_dob(resource: dict[str, Any]) -> str | None:
    dob = resource.get("birthDate")
    return dob if isinstance(dob, str) and dob else None


def _resource_id(resource: dict[str, Any]) -> str | None:
    rid = resource.get("id")
    return str(rid) if rid is not None else None


# ── Public entry point ───────────────────────────────────────────────────────


async def resolve(
    *,
    raw: bytes,
    format_hint: str,
    fhir_search: FhirSearch,
) -> ResolverOutcome:
    """Decide whether the inbound document maps to a single chart patient.

    Decision matrix (per todo.md Slice 9.2):

      ┌────────────────────────────┬──────────────────────────────────────┐
      │ Probe outcome              │ Resolver outcome                     │
      ├────────────────────────────┼──────────────────────────────────────┤
      │ no probe / probe None      │ Quarantine(NO_IDENTITY_HINTS)        │
      │ MRN present, exact-1 hit   │ Pass(mrn_exact)                      │
      │ MRN present, multi hits    │ Quarantine(MRN_AMBIGUOUS)            │
      │ MRN present, zero hits     │ Quarantine(MRN_NOT_FOUND)            │
      │ MRN == "UNREADABLE"        │ try name+DOB soft-resolve;           │
      │                            │ on miss → Quarantine(MRN_UNREADABLE) │
      │ MRN absent, name+DOB+      │ Pass(name_dob_soft) iff ALL 4        │
      │ same_region                │   soft-resolve gates hold; else      │
      │                            │   Quarantine(NAME_DOB_*)             │
      │ MRN absent, DOB missing    │ Quarantine(MISSING_DOB)              │
      └────────────────────────────┴──────────────────────────────────────┘

    The caller MUST pass through ``parsed_identity`` (extractable via the
    ``Quarantine.candidate_hints`` payload) into the
    ``copilot_quarantined_documents`` row so the operator sees what the
    resolver saw.
    """
    parsed = probe(raw, format_hint=format_hint)
    if parsed is None:
        return Quarantine(reason_code="NO_IDENTITY_HINTS", candidate_hints=[])

    # ── MRN dominant path ───────────────────────────────────────────────────
    if parsed.mrn and parsed.mrn != "UNREADABLE":
        try:
            bundle = await fhir_search("Patient", {"identifier": parsed.mrn})
        except Exception:
            return Quarantine(
                reason_code="PROBE_FAILED",
                candidate_hints=[parsed.to_dict()],
            )
        entries = _bundle_entries(bundle)
        n = len(entries)
        if n == 1:
            resource = entries[0].get("resource") or {}
            pid = _resource_id(resource)
            if pid is None:
                return Quarantine(
                    reason_code="PROBE_FAILED",
                    candidate_hints=[parsed.to_dict()],
                )
            return Pass(patient_id=pid, source="mrn_exact", candidates_seen=1)
        if n == 0:
            return Quarantine(
                reason_code="MRN_NOT_FOUND",
                candidate_hints=[parsed.to_dict()],
            )
        # Multiple hits — never pick automatically. Surface up to 5
        # PHI-safe hint rows so the operator can disambiguate.
        hints: list[dict[str, Any]] = [parsed.to_dict()]
        for entry in entries[:5]:
            resource = entry.get("resource") or {}
            hints.append(
                {
                    "candidate_id": _resource_id(resource),
                    "name": _resource_name(resource),
                    "dob": _resource_dob(resource),
                }
            )
        return Quarantine(reason_code="MRN_AMBIGUOUS", candidate_hints=hints)

    # ── MRN unreadable: fall through to soft-resolve, but if it fails the
    #     reason_code surfaces as MRN_UNREADABLE (not NAME_DOB_*) so the
    #     operator gets the right banner. ──────────────────────────────────
    fallback_reason: ReasonCode = (
        "MRN_UNREADABLE" if parsed.mrn == "UNREADABLE" else "NO_IDENTITY_HINTS"
    )

    # ── Name+DOB soft-resolve ───────────────────────────────────────────────
    # Gate (4) — name and DOB must have come from the same source region.
    if not parsed.same_region:
        return Quarantine(
            reason_code=(
                fallback_reason
                if parsed.mrn == "UNREADABLE"
                else "NAME_DOB_REGION_MISMATCH"
            ),
            candidate_hints=[parsed.to_dict()],
        )

    if not parsed.dob:
        return Quarantine(
            reason_code=(
                fallback_reason if parsed.mrn == "UNREADABLE" else "MISSING_DOB"
            ),
            candidate_hints=[parsed.to_dict()],
        )
    if not parsed.name:
        return Quarantine(
            reason_code=fallback_reason
            if parsed.mrn == "UNREADABLE"
            else "NO_IDENTITY_HINTS",
            candidate_hints=[parsed.to_dict()],
        )

    # Split the name into given+family for FHIR search; keep best-effort
    # (HL7 PID-5 already gave us "given family" in that order).
    parts = parsed.name.split()
    given = parts[0] if parts else ""
    family = parts[-1] if len(parts) >= 2 else ""

    params: dict[str, str] = {"birthdate": parsed.dob, "_count": "2"}
    if family:
        params["family"] = family
    if given:
        params["given"] = given

    try:
        bundle = await fhir_search("Patient", params)
    except Exception:
        return Quarantine(
            reason_code="PROBE_FAILED", candidate_hints=[parsed.to_dict()]
        )
    entries = _bundle_entries(bundle)
    n = len(entries)
    if n == 0:
        return Quarantine(
            reason_code=(
                fallback_reason
                if parsed.mrn == "UNREADABLE"
                else "NAME_DOB_NOT_FOUND"
            ),
            candidate_hints=[parsed.to_dict()],
        )
    if n >= 2:
        # Gate (3) — _count=2 confirmation must return exactly one entry
        # for the resolver to pick. Two or more hits → quarantine.
        hints = [parsed.to_dict()]
        for entry in entries[:5]:
            resource = entry.get("resource") or {}
            hints.append(
                {
                    "candidate_id": _resource_id(resource),
                    "name": _resource_name(resource),
                    "dob": _resource_dob(resource),
                }
            )
        return Quarantine(
            reason_code=(
                fallback_reason
                if parsed.mrn == "UNREADABLE"
                else "NAME_DOB_AMBIGUOUS"
            ),
            candidate_hints=hints,
        )

    # n == 1: enforce gates (1) + (2) — exact normalized name + exact ISO DOB.
    resource = entries[0].get("resource") or {}
    chart_name = _resource_name(resource) or ""
    chart_dob = _resource_dob(resource) or ""
    if _wsclean(chart_name) != _wsclean(parsed.name) or chart_dob != parsed.dob:
        return Quarantine(
            reason_code=(
                fallback_reason
                if parsed.mrn == "UNREADABLE"
                else "NAME_DOB_NOT_FOUND"
            ),
            candidate_hints=[
                parsed.to_dict(),
                {
                    "candidate_id": _resource_id(resource),
                    "name": chart_name or None,
                    "dob": chart_dob or None,
                },
            ],
        )

    pid = _resource_id(resource)
    if pid is None:
        return Quarantine(
            reason_code="PROBE_FAILED", candidate_hints=[parsed.to_dict()]
        )
    return Pass(patient_id=pid, source="name_dob_soft", candidates_seen=1)


__all__ = [
    "FhirSearch",
    "ParsedIdentity",
    "Pass",
    "Quarantine",
    "ReasonCode",
    "ResolverOutcome",
    "probe",
    "probe_docx",
    "probe_hl7_v2",
    "probe_pdf",
    "probe_tiff",
    "probe_xlsx",
    "resolve",
]
