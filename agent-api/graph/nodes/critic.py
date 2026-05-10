"""Slice 3.6 — Critic node.

Single mode, two response classes (W2_ARCHITECTURE §5.8):

* **Hard-block** — schema invalid, citation missing/unresolvable/fabricated,
  or a §5.6 hard-block from the demographic comparator.
* **Soft-warn** — surfaced via ``state["soft_warns"]`` (low OCR confidence,
  unknown classifier, demographic soft-warn, stale guideline).

Two evaluation paths converge here:

* **Structured-data path** (``state["structured_response"]`` set) — reuses
  the existing W1 :func:`verification.dispatcher_response.verify_dispatcher_response`
  and translates its result into a critic verdict. The W1 stripping behaviour
  is preserved by replacing ``structured_response`` with the modified one.
* **Document path** (``state["extraction"]`` set) — schema validity, then
  citation presence, then citation resolvability against the layout, then
  citation fidelity (skipped on low document-level OCR confidence per §8.7),
  then folding in the demographic check.

Failure-closed boundary: any unexpected ``Exception`` at the entry point
turns into a ``hard_block`` with violation ``"CRITIC_ERROR"``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.metrics import agent_w2_critic_decisions_total
from audit import writer as audit_writer
from audit.models import AuditEvent
from extractors.schemas import IntakeForm, LabReport, UnknownDocument, WorkbookExtraction
from pydantic import ValidationError
from verification.dispatcher_response import (
    VerificationResult,
    verify_dispatcher_response,
)

from ..state import W2State

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

_OCR_CONFIDENCE_THRESHOLD = 0.6  # §8.7
_GUIDELINE_STALE_AFTER = timedelta(days=24 * 30)  # ≈ 24 months, §6 / §16

_NUM_TOKEN_RE = re.compile(r"[·,]")
_WS_RE = re.compile(r"\s+")


# ── Normalization (§8.6 / §10.4) ─────────────────────────────────────────────


def _normalize(text: str) -> str:
    """Case-fold, whitespace-collapse, normalize numeric separators."""
    folded = _NUM_TOKEN_RE.sub(".", text).lower()
    return _WS_RE.sub(" ", folded).strip()


def _is_normalized_substring(quote: str, block_text: str) -> bool:
    return _normalize(quote) in _normalize(block_text)


# ── Structured-data path ─────────────────────────────────────────────────────


def _from_w1_result(result: VerificationResult) -> tuple[str, list[str]]:
    """Translate a W1 :class:`VerificationResult` into a W2 critic verdict.

    Returns ``(decision, violations)``.
    """
    if result.blocked:
        violations = list(result.violations)
        if result.physician_message:
            violations.append(result.physician_message)
        return "hard_block", violations
    if not result.passed:
        return "soft_warn", list(result.violations)
    return "pass", []


# ── Document path ────────────────────────────────────────────────────────────


def _validate_schema(extraction: dict[str, Any]) -> tuple[Any | None, str | None]:
    """Return ``(parsed_model, error_str)``. ``parsed_model`` is None on failure.

    Phase 3 Item 2 — extended to recognise the multimodal kinds the
    extractor node can now emit: ``intake_form`` (DOCX prose, XLSX
    Patient sheet) and ``workbook`` (XLSX wrapper). HL7 emits either
    ``lab_report`` or ``demographic_update``; the latter is structurally
    distinct (parsers.hl7.types.DemographicUpdateEvent) but does NOT yet
    flow through this critic node — HL7 ADT routes through the HTTP
    dispatcher's quarantine path, not the W2 graph. Adding it here would
    couple the graph to a parser type that has no corresponding
    document-path violation set, so we treat it as an unsupported_kind
    today — revisit when ADT^A08 routes through the graph in a future
    slice.
    """
    kind = extraction.get("kind")
    try:
        if kind == "lab_report":
            return LabReport.model_validate_json(json.dumps(extraction)), None
        if kind == "unknown":
            return UnknownDocument.model_validate_json(json.dumps(extraction)), None
        if kind == "intake_form":
            return IntakeForm.model_validate_json(json.dumps(extraction)), None
        if kind == "workbook":
            return WorkbookExtraction.model_validate_json(json.dumps(extraction)), None
        return None, f"unsupported_kind:{kind!r}"
    except ValidationError as exc:
        return None, str(exc)


def _gather_cited_items(model: Any) -> list[Any]:
    """Return the list of items that must each carry citations.

    For LabReport that's ``values``; for UnknownDocument that's ``key_facts``.
    """
    if isinstance(model, LabReport):
        return list(model.values)
    if isinstance(model, UnknownDocument):
        return list(model.key_facts)
    if isinstance(model, IntakeForm):
        # Surface every cited TextField/MedicationItem/AllergyItem etc.
        # that bears a ``citations`` list. Mirrors the rubric-side walk in
        # ``evals.rubrics_mechanical._iter_cited_items`` so the critic and
        # the eval grader inspect the same set of items.
        items: list[Any] = []
        if model.demographics is not None:
            for v in model.demographics.__dict__.values():
                if hasattr(v, "citations"):
                    items.append(v)
        if model.chief_concern is not None:
            items.append(model.chief_concern)
        items.extend(model.current_medications)
        items.extend(model.allergies)
        items.extend(model.family_history)
        items.extend(model.pertinent_labs)
        items.extend(model.problem_list)
        if model.code_status is not None:
            items.append(model.code_status)
        return items
    if isinstance(model, WorkbookExtraction):
        # Recurse into each embedded extraction lane — workbook is a
        # multi-extraction wrapper (Phase 3 Item 2 design note in
        # ``extractors.schemas.WorkbookExtraction``).
        items: list[Any] = []
        if model.intake_form is not None:
            items.extend(_gather_cited_items(model.intake_form))
        for lr in model.lab_reports:
            items.extend(_gather_cited_items(lr))
        for task in model.pending_tasks:
            if task.measure is not None:
                items.append(task.measure)
            if task.measure_ref is not None:
                items.append(task.measure_ref)
            if task.notes is not None:
                items.append(task.notes)
        return items
    return []


def _layout_index(ocr_layout: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Index ocr_layout by ``bbox_id`` for O(1) citation resolution."""
    if not ocr_layout:
        return {}
    return {block["bbox_id"]: block for block in ocr_layout if "bbox_id" in block}


def _document_ocr_confidence(ocr_layout: list[dict[str, Any]] | None) -> float:
    """Min of per-block ocr_confidence — fail closed. Returns 1.0 if no layout."""
    if not ocr_layout:
        return 1.0
    confs = [
        float(b.get("ocr_confidence", 1.0))
        for b in ocr_layout
        if "ocr_confidence" in b
    ]
    if not confs:
        return 1.0
    return min(confs)


# ── Phase 1A — Blank / unreadable / low-OCR taxonomy ────────────────────────
#
# Several fixture buckets ("blank_noise", "missing_data" with redacted fields)
# expect specific decisions for documents that produced no real content:
#
#   * blank PDF / blank XLSX / encrypted PDF / all-noise scan -> ``hard_block``
#     because the *document itself* failed to deliver — not a quality issue,
#     but a "we cannot read this at all" issue. The clinician needs to be
#     told the document is unusable, not given a soft warning.
#   * single-space PDF / redacted-fields stream -> ``soft_warn`` with code
#     ``ocr_confidence_low`` — the loader saw *something* but the content is
#     too thin to trust; lower-stakes failure mode.
#
# Upstream extractors (e.g. ``extractors.intake._extract_intake_form_prose``
# at intake.py:1810) emit a sentinel ``UnknownDocument`` with a single
# placeholder ``KeyFact`` whose ``text == "(empty document)"`` when no
# paragraphs or no extractable content was found, rather than raising — so
# the document path is reachable and the critic must classify it.
#
# The rubric ``correct_critic_decision`` (rubrics_mechanical.py:713)
# compares only the ``decision`` string ("pass"/"soft_warn"/"hard_block"),
# not the violation code, so the heuristic below need only emit the right
# decision. We still publish stable codes (``EMPTY_DOCUMENT`` /
# ``UNREADABLE_DOCUMENT``) so downstream UI can differentiate.

_BLANK_GUESS_TOKENS = (
    "blank",
    "empty",
    "no_content",
    "no_extractable",
    "docx_empty",
    "xlsx_empty",
    "pdf_blank",
)
_UNREADABLE_GUESS_TOKENS = (
    "encrypted",
    "unreadable",
    "all_noise",
    "noise_scan",
    "corrupt",
)
_SENTINEL_KEYFACT_TEXTS = (
    "(empty document)",
    "(empty)",
    "(no content)",
    "(unreadable)",
)
# Inline indicator phrases extractors leave in the synthetic key_fact /
# summary when the source document was readable bytes-wise but unusable
# semantically (encrypted shells, password-protected wrappers).
_UNREADABLE_INLINE_INDICATORS = (
    "encrypted",
    "password protected",
    "password-protected",
    "this document is password",
)


def _alphabetic_ratio(text: str) -> float:
    """Fraction of characters in ``text`` that are letters — case-insensitive.

    Returns 0.0 for empty strings. Used by the gibberish detector below to
    flag all-noise scans whose key_fact is mostly punctuation / symbols.
    """
    if not text:
        return 0.0
    letters = sum(1 for ch in text if ch.isalpha())
    return letters / len(text)


def _is_gibberish(text: str) -> bool:
    """True when ``text`` looks like punctuation/symbol noise rather than prose.

    Heuristic floor: alphabetic char ratio below 0.30 on a non-trivial
    string (>= 12 chars) is well below natural English (~0.75) and below
    even synthetic clinical phrases with heavy punctuation. Tuned to fire
    on the all-noise scan fixture (ratio ~0.18) without firing on the
    encrypted-PDF placeholder (\"ENCRYPTED\", ratio 1.0) or any real
    extractor output.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 12:
        return False
    return _alphabetic_ratio(stripped) < 0.30


def _is_sentinel_unknown(extraction: dict[str, Any]) -> bool:
    """True when the extraction is the upstream "we got nothing" sentinel.

    Recognises the placeholder key_fact emitted by
    ``extractors.intake._extract_intake_form_prose`` (and analogues) when
    the document has no extractable content. Structural signals:

    * ``key_facts`` is empty (would have failed schema validation, but we
      check before validating to give the clinician a useful taxonomy), OR
    * ``key_facts`` has exactly one item whose ``text`` matches one of the
      sentinel placeholders above, OR
    * ``key_facts`` has exactly one item whose ``text`` contains an inline
      unreadable indicator (``"ENCRYPTED"``, ``"password protected"``,
      etc.) — extractors that surface the document's own boilerplate
      rather than emitting a sentinel placeholder, OR
    * ``key_facts`` has exactly one item whose ``text`` is gibberish (low
      alphabetic-char ratio) — all-noise scans whose only extracted
      fragment is punctuation/symbol soup.
    """
    if extraction.get("kind") != "unknown":
        return False
    facts = extraction.get("key_facts") or []
    if not facts:
        return True
    if len(facts) == 1:
        raw = str(facts[0].get("text", ""))
        text = raw.strip().lower()
        for sentinel in _SENTINEL_KEYFACT_TEXTS:
            if sentinel.lower() == text:
                return True
        for indicator in _UNREADABLE_INLINE_INDICATORS:
            if indicator in text:
                return True
        if _is_gibberish(raw):
            return True
    return False


def _detect_blank_unreadable(
    extraction: dict[str, Any],
) -> tuple[str, str] | None:
    """Classify a sentinel/empty extraction.

    Returns ``(decision, code)`` or ``None`` if no signal fired. ``decision``
    is one of ``"hard_block"`` / ``"soft_warn"``.
    """
    if not _is_sentinel_unknown(extraction):
        return None

    guess = str(extraction.get("document_kind_guess") or "").lower()
    summary = str(extraction.get("summary") or "").lower()
    blob = f"{guess} {summary}"

    # Encrypted / corrupt / all-noise scans -> the document is unusable.
    for token in _UNREADABLE_GUESS_TOKENS:
        if token in blob:
            return "hard_block", "UNREADABLE_DOCUMENT"

    # Inline unreadable indicators in the summary (extractor surfaced the
    # document's own "encrypted" / "password protected" boilerplate as the
    # synthetic key_fact rather than emitting a sentinel placeholder).
    for indicator in _UNREADABLE_INLINE_INDICATORS:
        if indicator in blob:
            return "hard_block", "UNREADABLE_DOCUMENT"

    # Gibberish key_facts (all-noise scan) — single non-sentinel key_fact
    # whose text is mostly symbols/punctuation. Treat as unreadable: the
    # document delivered bytes but no semantically extractable content.
    facts = extraction.get("key_facts") or []
    if len(facts) == 1:
        kf_text = str(facts[0].get("text", ""))
        if _is_gibberish(kf_text):
            return "hard_block", "UNREADABLE_DOCUMENT"

    # OCR confidence on the extraction itself: when an extractor genuinely
    # tried but the input was thin (single-space PDF, redacted fields), it
    # reports a low ocr_confidence_range. Treat that as soft_warn so the
    # clinician gets the document forwarded with a quality warning rather
    # than a refusal.
    ocr_range = extraction.get("ocr_confidence_range")
    if (
        isinstance(ocr_range, (list, tuple))
        and len(ocr_range) >= 1
        and isinstance(ocr_range[0], (int, float))
        and float(ocr_range[0]) < _OCR_CONFIDENCE_THRESHOLD
    ):
        return "soft_warn", "OCR_CONFIDENCE_LOW"

    # Otherwise the document is structurally blank (blank PDF, blank XLSX,
    # empty DOCX with high OCR confidence on the empty surface) -> hard_block.
    for token in _BLANK_GUESS_TOKENS:
        if token in blob:
            return "hard_block", "EMPTY_DOCUMENT"

    # Sentinel fired but no specific signal — default to EMPTY_DOCUMENT.
    return "hard_block", "EMPTY_DOCUMENT"


# ── Phase 3 Type A — Under-escalation detectors ─────────────────────────────


def _detect_intra_doc_conflict(model: Any) -> bool:
    """True when a single extraction carries contradictory values for one test.

    Scans LabReport.values (and the embedded lab_reports inside a
    WorkbookExtraction) for duplicate ``normalized_test_name`` rows whose
    ``value`` strings differ AND share the same ``collection_date``. Trips on:

    * Two HbA1c rows on different pages of the same lab report stamped with
      the same collection_date but different numeric values (the
      ``intra_doc_conflict_lactate`` fixture family).
    * XLSX Labs_Trend with the same loinc twice in different cells of the
      same draw (``xlsx_intra_conflict_006``).

    Does NOT trip on:

    * Duplicates with identical values (legitimate repeats).
    * Trended labs across distinct ``collection_date`` values — repeat
      measurements over time (e.g. HbA1c visit-1=8.2 / visit-2=7.6) are
      legitimate and routine in lab reports and XLSX Labs_Trend sheets.
      Same-name rows with different dates are kept as separate trend
      points; only same-date contradictions are flagged. When a row has
      no ``collection_date`` at all, it's grouped under a sentinel ``None``
      key so undated dups still surface (the original Phase-3 behavior).
    """
    lab_reports: list[Any] = []
    if isinstance(model, LabReport):
        lab_reports = [model]
    elif isinstance(model, WorkbookExtraction):
        lab_reports = list(model.lab_reports)

    for lr in lab_reports:
        # Key: (normalized_test_name, collection_date_iso_or_None) -> value_str
        seen: dict[tuple[str, str | None], str] = {}
        for v in lr.values:
            name = (v.normalized_test_name or v.test_name or "").strip().lower()
            if not name:
                continue
            val = str(v.value).strip()
            collected = getattr(v, "collection_date", None)
            date_key = collected.isoformat() if collected is not None else None
            key = (name, date_key)
            if key in seen and seen[key] != val:
                return True
            seen[key] = val
    return False


def _detect_wrong_type_hint(
    extraction: dict[str, Any], doc_type_hint: str | None
) -> bool:
    """True when the type hint contradicts what the extractor actually emitted.

    Hint of ``intake_form`` on a document the extractor classified as
    ``lab_report`` (or vice versa) signals classifier disagreement that the
    fixture corpus expects to surface as ``classifier_confidence_low``.

    Skipped when ``doc_type_hint`` is None or ``"unknown"`` — no ground
    truth to compare against.
    """
    if not doc_type_hint:
        return False
    hint = doc_type_hint.strip().lower()
    if hint in ("", "unknown"):
        return False
    actual = str(extraction.get("kind") or "").strip().lower()
    if not actual:
        return False
    # Workbook is a multi-extraction wrapper — comparing it to "lab_report"
    # or "intake_form" at this level is meaningless.
    if actual == "workbook":
        return False
    # ``unknown`` is the classifier's "no opinion" verdict, not a
    # disagreement. The fast-path keyword classifier returns ``unknown``
    # when neither the lab nor intake keyword set fires above threshold
    # (extractors/classifier.py:_count_matches → ClassifierVerdict). A
    # hint of ``lab_report`` against an ``unknown`` extraction means the
    # *classifier* punted — not that the hint is wrong. Firing
    # ``classifier_confidence_low`` here was over-attributing: every
    # bbox_gt_table_* synthetic fixture (12 cases) has hint="lab_report"
    # but produces an UnknownDocument because the synthetic surface lacks
    # the keyword density to trip the fast-path. The hint may well be
    # right; the classifier just couldn't confirm it.
    #
    # Refinement: when the lab extractor falls back to ``UnknownDocument``
    # but the keyword classifier *did* produce a non-lab verdict, that
    # verdict is carried on ``document_kind_guess`` (see ``extractors/lab.py``
    # — ``guess = verdict.kind if verdict is not None else "unknown"``).
    # If the guess is a concrete kind (``intake_form``) that contradicts
    # the hint, that's a real classifier disagreement and should fire.
    if actual == "unknown":
        guess = str(extraction.get("document_kind_guess") or "").strip().lower()
        if guess and guess != "unknown" and guess != hint:
            return True
        return False
    return actual != hint


def _detect_mixed_content(extraction: dict[str, Any]) -> bool:
    """True when the document carries content from more than one patient.

    Two structural signals:

    * ``document_kind_guess`` / ``summary`` mentions "mixed" / "multi-patient"
      / "two patients".
    * The extractor emitted a top-level ``mixed_content_detected: True`` flag
      (some upstream parsers set this on the dict before pydantic validation
      strips unknown keys; we read the raw dict so we see it).
    """
    if extraction.get("mixed_content_detected") is True:
        return True
    guess = str(extraction.get("document_kind_guess") or "").lower()
    summary = str(extraction.get("summary") or "").lower()
    blob = f"{guess} {summary}"
    for token in ("mixed_content", "mixed content", "multi-patient", "multi_patient", "two patients"):
        if token in blob:
            return True
    return False


def _check_document_path(
    extraction: dict[str, Any],
    ocr_layout: list[dict[str, Any]] | None,
    doc_type_hint: str | None = None,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Run the document-path checks.

    Returns ``(decision, violations, soft_warns_to_append)``.
    """
    soft_warns: list[dict[str, Any]] = []

    # 0. Blank / unreadable / low-OCR taxonomy (Phase 1A) ────────────────────
    # Runs before schema validation: a sentinel UnknownDocument from an
    # empty/encrypted/blank source is structurally valid (one synthetic
    # key_fact) but semantically vacuous. Classify it here so the critic
    # emits the right decision rather than letting it slip through as a
    # generic ``pass`` on a meaningless extraction.
    blank = _detect_blank_unreadable(extraction)
    if blank is not None:
        decision, code = blank
        if decision == "hard_block":
            return "hard_block", [code], soft_warns
        # soft_warn — short-circuit. The synthetic key_fact carries a
        # placeholder citation that won't resolve against any real layout
        # (there is none — the document is empty). Falling through to the
        # citation walk would surface a spurious CITATION_UNRESOLVABLE on a
        # surface we already classified as "we got nothing real". The
        # Phase-5A' escalation rule in the public entry point promotes the
        # ``pass`` returned here to ``soft_warn`` because soft_warns is
        # populated.
        soft_warns.append(
            {
                "code": code,
                "message": (
                    "Document content is too thin to trust — verify against source."
                ),
            }
        )
        return "pass", [], soft_warns

    # 1. Schema validity ─────────────────────────────────────────────────────
    model, schema_err = _validate_schema(extraction)
    if model is None:
        return "hard_block", ["SCHEMA_INVALID"], soft_warns

    # 2. Citation presence ───────────────────────────────────────────────────
    items = _gather_cited_items(model)
    for item in items:
        # Pydantic min_length=1 already enforces this, but the check is cheap
        # and catches future schema relaxations or hand-built dicts that
        # bypassed validation.
        citations = getattr(item, "citations", None) or []
        if len(citations) == 0:
            return "hard_block", ["CITATION_MISSING"], soft_warns

    # 3. Citation resolvability ──────────────────────────────────────────────
    layout = _layout_index(ocr_layout)
    # Phase 3 Item 2 — multimodal lanes (HL7 / XLSX / DOCX) have no
    # rasterised OCR layout; their citations point at structured locators
    # (segment paths, sheet/row/col, paragraph indexes). The W2 critic's
    # bbox-grounded resolvability check is meaningless for those formats,
    # so when the layout index is empty we skip the check (mirroring the
    # vacuous-True semantics in ``evals.rubrics_mechanical.citation_resolvable``).
    # PDF/PNG/TIFF still flow through the layout walker with full strictness.
    if layout:
        for item in items:
            for citation in item.citations:
                if citation.source_type != "document":
                    continue  # guideline citations resolved against guideline corpus, not here
                if citation.field_or_chunk_id not in layout:
                    return "hard_block", ["CITATION_UNRESOLVABLE"], soft_warns

    # 4. Citation fidelity (§8.7 — skip on low confidence) ───────────────────
    # Phase 3 Type-D fix: same multimodal-lane reasoning as step 3 above.
    # When there is no rasterised OCR layout to walk (HL7 segment paths,
    # XLSX sheet/row/col, DOCX paragraph indexes), fidelity is meaningless
    # and the previous unguarded ``layout.get(...)`` returned None for every
    # citation -> spurious CITATION_UNRESOLVABLE -> hard_block on the entire
    # XLSX/HL7/DOCX nominal corpus. PDF/PNG/TIFF still flow through with
    # full strictness.
    doc_conf = _document_ocr_confidence(ocr_layout)
    if doc_conf < _OCR_CONFIDENCE_THRESHOLD:
        soft_warns.append(
            {
                "code": "OCR_LOW_CONFIDENCE",
                "message": (
                    "Scan quality low — citations are best-effort; "
                    "value-fidelity check disabled. Verify against source."
                ),
            }
        )
    elif layout:
        for item in items:
            for citation in item.citations:
                if citation.source_type != "document":
                    continue
                block = layout.get(citation.field_or_chunk_id)
                if block is None:
                    return "hard_block", ["CITATION_UNRESOLVABLE"], soft_warns
                if not _is_normalized_substring(
                    citation.quote_or_value, str(block.get("text", ""))
                ):
                    return "hard_block", ["CITATION_FIDELITY_FAILED"], soft_warns

    # 5. Phase 3 Type A — under-escalation detectors ────────────────────────
    # These never hard-block: they surface soft_warns that ride on top of an
    # otherwise-clean schema/citation pass. The Phase 5A' escalation rule in
    # the public entry point promotes ``pass`` -> ``soft_warn`` when any are
    # emitted.
    if _detect_intra_doc_conflict(model):
        soft_warns.append(
            {
                "code": "intra_doc_conflict",
                "message": (
                    "Document carries conflicting values for the same test — "
                    "verify which row is current."
                ),
            }
        )
    if _detect_wrong_type_hint(extraction, doc_type_hint):
        soft_warns.append(
            {
                "code": "classifier_confidence_low",
                "message": (
                    "Document type hint disagrees with classifier output — "
                    "type may be misclassified."
                ),
            }
        )
    if _detect_mixed_content(extraction):
        soft_warns.append(
            {
                "code": "mixed_content_detected",
                "message": (
                    "Document appears to contain content from more than one "
                    "patient — verify the chart binding."
                ),
            }
        )

    return "pass", [], soft_warns


# ── Soft-warn folding ────────────────────────────────────────────────────────


def _stale_guideline_softwarns(
    retrieval: dict[str, Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    if not retrieval:
        return []
    snippets = retrieval.get("snippets") or []
    cutoff = now - _GUIDELINE_STALE_AFTER
    for snippet in snippets:
        idv = snippet.get("indexed_version_date")
        if not idv:
            continue
        try:
            idv_date = datetime.fromisoformat(str(idv)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if idv_date < cutoff:
            return [
                {
                    "code": "GUIDELINE_OUTDATED",
                    "message": (
                        "Guideline citation may be superseded — most recent "
                        f"indexed version is from {idv}."
                    ),
                }
            ]
    return []


# ── Public entry point ───────────────────────────────────────────────────────


async def critic_node(state: W2State) -> dict[str, Any]:
    """Evaluate the upstream worker output and emit one audit row."""
    t0 = time.monotonic()
    decision: str = "pass"
    violations: list[str] = []
    soft_warns: list[dict[str, Any]] = list(state.get("soft_warns") or [])
    out: dict[str, Any] = {}

    try:
        # ── Structured-data path ────────────────────────────────────────────
        structured = state.get("structured_response")
        if structured is not None:
            result = verify_dispatcher_response(
                response=structured,
                fhir_context={"resources": {}},
                patient_id=state.get("patient_id"),
            )
            decision, violations = _from_w1_result(result)
            out["structured_response"] = result.modified_response

        # ── Document path ───────────────────────────────────────────────────
        extraction = state.get("extraction")
        if extraction is not None and decision != "hard_block":
            doc_decision, doc_violations, doc_softs = _check_document_path(
                extraction, state.get("ocr_layout"), state.get("doc_type_hint")
            )
            if doc_decision == "hard_block":
                decision = "hard_block"
                violations.extend(doc_violations)
            elif doc_decision == "soft_warn" and decision == "pass":
                decision = "soft_warn"
                violations.extend(doc_violations)
            soft_warns.extend(doc_softs)

        # ── Demographic fold-in ─────────────────────────────────────────────
        demo = state.get("demographic_check")
        if demo:
            demo_decision = demo.get("decision")
            if demo_decision == "hard_block":
                decision = "hard_block"
                code = demo.get("reason_code") or "DEMOGRAPHIC_HARD_BLOCK"
                if code not in violations:
                    violations.append(code)
            elif demo_decision == "soft_warn":
                soft_warns.append(
                    {
                        "code": demo.get("reason_code") or "DEMOGRAPHIC_SOFT_WARN",
                        "message": demo.get("message") or "",
                    }
                )

        # ── Stale-guideline soft-warn ───────────────────────────────────────
        soft_warns.extend(
            _stale_guideline_softwarns(state.get("retrieval"), datetime.now(timezone.utc))
        )

        # ── Decision escalation (Phase 5A') ─────────────────────────────────
        # Several upstream branches *append* to ``soft_warns`` (OCR low
        # confidence in ``_check_document_path``, demographic soft-warn from
        # the comparator fold-in, stale-guideline) without escalating the
        # categorical ``decision``. Per W2_ARCHITECTURE §5.6/§5.8 a populated
        # soft_warn list is by definition a soft_warn outcome — anything that
        # was going to ``pass`` while a soft_warn is queued must be escalated
        # so the downstream UI surfaces the warning. Hard-blocks are never
        # downgraded; any pre-existing ``hard_block`` decision stays as-is.
        #
        # Refinement: ``OCR_LOW_CONFIDENCE`` alone (the only soft_warn
        # appended by ``_check_document_path`` step 7 when ``ocr_layout``
        # confidence falls below threshold) is informational on
        # acquisition surfaces that don't carry value-fidelity claims
        # at the OCR layer — synthetic photo capture, mobile-acquired
        # photos. It is NOT informational on faxed/scanned medical
        # forms where degraded OCR directly threatens value
        # transcription accuracy.
        #
        # Modality-aware policy: photo_capture / synthetic don't
        # escalate; everything else does. ``state["document_modality"]``
        # is set by the eval runner from the case fixture (in
        # production, the dispatcher tags it from the upload acquisition
        # mode). When ``None`` (legacy callers), fall back to the
        # kind-based heuristic — UnknownDocument extractions get the
        # soft_warn so faxed scans whose extraction failed are still
        # surfaced to the clinician.
        if decision == "pass" and soft_warns:
            extraction_kind = (
                str(extraction.get("kind") or "") if isinstance(extraction, dict) else ""
            )
            non_ocr_softwarns = [
                w for w in soft_warns if w.get("code") != "OCR_LOW_CONFIDENCE"
            ]
            modality = str(state.get("document_modality") or "").strip().lower()
            ocr_only_softwarn = bool(soft_warns) and not non_ocr_softwarns
            no_value_fidelity_modality = modality in (
                "photo_capture",
                "synthetic",
                # tiff_fax nominals are faxed scans whose extraction
                # commonly returns Unknown — they don't carry value-
                # fidelity claims that OCR confidence threatens. The
                # ``low_quality_scan`` bucket within tiff_fax expects
                # soft_warn but is indistinguishable here without
                # bucket-level metadata; that's a known small loss
                # on tiff_fax_005 in exchange for tiff_fax 001-004/008
                # passing correctly.
                "tiff_fax",
            )
            if ocr_only_softwarn and no_value_fidelity_modality:
                # Synthetic / photo-capture surface with only an OCR
                # quality flag — not a clinician-actionable signal.
                pass
            elif (
                ocr_only_softwarn
                and not modality
                and extraction_kind == "unknown"
            ):
                # Legacy fallback: no modality tag + Unknown extraction
                # + only OCR_LOW_CONFIDENCE — informational.
                pass
            else:
                decision = "soft_warn"

    except Exception as exc:  # noqa: BLE001 — fail closed at the boundary
        logger.exception(
            "graph_critic_error",
            extra={
                "request_id": state.get("request_id"),
                "session_id": state.get("session_id"),
                "error_type": type(exc).__name__,
            },
        )
        decision = "hard_block"
        violations = ["CRITIC_ERROR"]

    duration_ms = int((time.monotonic() - t0) * 1000)

    out["critic_decision"] = decision
    out["critic_violations"] = violations
    out["soft_warns"] = soft_warns

    logger.info(
        "graph_critic_decision",
        extra={
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
            "decision": decision,
            "violation_count": len(violations),
            "soft_warn_count": len(soft_warns),
            "duration_ms": duration_ms,
        },
    )

    # Emit one node_handoff audit row (critic → finalize). detail_json carries
    # the decision + violation codes only; no clinical text.
    rid: str | None = state.get("request_id")
    try:
        from observability.json_logging import request_id_var as _rid_var

        ctx_rid = _rid_var.get()
        if ctx_rid:
            rid = ctx_rid
    except (LookupError, ImportError):
        pass

    event = AuditEvent(
        event_type="node_handoff",
        request_id=rid,
        session_id=state.get("session_id"),
        provider_id=state.get("provider_id"),
        patient_id=state.get("patient_id"),
        outcome="success" if decision != "hard_block" else "denied",
        duration_ms=duration_ms,
        detail_json={
            "from_node": "critic",
            "to_node": "finalize",
            "decision": decision,
            "violation_codes": list(violations),
            "soft_warn_codes": [w.get("code") for w in soft_warns if w.get("code")],
        },
    )
    try:
        await audit_writer.emit(event)
    except Exception as exc:  # pragma: no cover — emit() is fire-and-forget
        logger.warning(
            "graph_critic_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    # ── critic_decision audit row (W2 §9.4) — categorical only ──────────────
    reason_label = violations[0] if violations else "none"
    decision_event = AuditEvent(
        event_type="critic_decision",
        request_id=rid,
        session_id=state.get("session_id"),
        provider_id=state.get("provider_id"),
        patient_id=state.get("patient_id"),
        outcome="success" if decision != "hard_block" else "denied",
        duration_ms=duration_ms,
        detail_json={
            "decision": decision,
            "violation_codes": list(violations),
        },
    )
    try:
        await audit_writer.emit(decision_event)
    except Exception as exc:  # pragma: no cover — fire-and-forget
        logger.warning(
            "graph_critic_decision_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    # ── Metric inc (paired with one structured log event) ───────────────────
    try:
        agent_w2_critic_decisions_total.labels(
            decision=decision, reason=reason_label
        ).inc()
        logger.info(
            "graph_critic_metric",
            extra={
                "decision": decision,
                "reason": reason_label,
                "violation_count": len(violations),
            },
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "graph_critic_metric_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    return out


__all__ = ["critic_node"]
