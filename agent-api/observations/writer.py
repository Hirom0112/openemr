"""FHIR-Observation writer that targets the custom Co-Pilot endpoint.

W2 Phase 2 deliverable: derive one ``Observation`` per extracted
``LabValue`` and POST it to
``/interface/modules/custom_modules/oe-module-clinical-copilot/public/observation.php``,
authenticated by the same HS256 JWT shape ``JwtMinter.php`` mints (shared
secret ``copilot_jwt_secret``, issuer ``openemr-copilot``).

Idempotency
-----------
Resource ids are deterministic — ``copilot-{document_id}-{loinc_code}`` —
so re-extraction overwrites the same row in OpenEMR's
``copilot_observations`` table rather than duplicating it. The custom
endpoint UPSERTs by primary key.

Privacy
-------
PSR-3 logging only. Never logs raw values, citations, prompt text, or
free-text clinical fields. Counts and durations only — see
``W1_ARCHITECTURE.md`` §5.2 / §9.2.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any, Optional

import httpx

from documents.fhir_writer import _mint_copilot_jwt
from extractors.schemas import LabValue
from config import settings

_logger = logging.getLogger(__name__)


# ── LOINC mapping for common labs ───────────────────────────────────────────
#
# Tiny static table for the v1 demo. v2 should bridge into a maintained
# vocabulary (e.g. RxNorm / LOINC bulk download). When the normalised test
# name has no entry, we fall back to ``LP-UNKNOWN`` and hand the human-
# readable test name through as the display so the chart still renders
# something meaningful.

_LOINC_TABLE: dict[str, tuple[str, str]] = {
    "lactate": ("32693-4", "Lactate [Moles/volume] in Blood"),
    "sodium": ("2951-2", "Sodium [Moles/volume] in Serum or Plasma"),
    "potassium": ("2823-3", "Potassium [Moles/volume] in Serum or Plasma"),
    "creatinine": ("2160-0", "Creatinine [Mass/volume] in Serum or Plasma"),
    "hemoglobin": ("718-7", "Hemoglobin [Mass/volume] in Blood"),
    "hgb": ("718-7", "Hemoglobin [Mass/volume] in Blood"),
    "hematocrit": ("4544-3", "Hematocrit [Volume Fraction] of Blood"),
    "hct": ("4544-3", "Hematocrit [Volume Fraction] of Blood"),
    "wbc": ("6690-2", "Leukocytes [#/volume] in Blood"),
    "platelets": ("777-3", "Platelets [#/volume] in Blood"),
    "glucose": ("2345-7", "Glucose [Mass/volume] in Serum or Plasma"),
    "cholesterol": ("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma"),
    "ldl": ("2089-1", "LDL Cholesterol [Mass/volume] in Serum or Plasma"),
    "hdl": ("2085-9", "HDL Cholesterol [Mass/volume] in Serum or Plasma"),
    "triglycerides": ("2571-8", "Triglyceride [Mass/volume] in Serum or Plasma"),
    "bun": ("3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma"),
    "urea_nitrogen": ("3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma"),
    "alt": ("1742-6", "Alanine aminotransferase [Enzymatic activity/volume]"),
    "ast": ("1920-8", "Aspartate aminotransferase [Enzymatic activity/volume]"),
    "albumin": ("1751-7", "Albumin [Mass/volume] in Serum or Plasma"),
    "bilirubin": ("1975-2", "Bilirubin.total [Mass/volume] in Serum or Plasma"),
    "calcium": ("17861-6", "Calcium [Mass/volume] in Serum or Plasma"),
    "magnesium": ("19123-9", "Magnesium [Mass/volume] in Serum or Plasma"),
    "phosphate": ("2777-1", "Phosphate [Mass/volume] in Serum or Plasma"),
    "tsh": ("3016-3", "Thyrotropin [Units/volume] in Serum or Plasma"),
    "a1c": ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "hba1c": ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "inr": ("6301-6", "INR in Platelet poor plasma by Coagulation assay"),
    "troponin": ("6598-7", "Troponin T.cardiac [Mass/volume] in Serum or Plasma"),
    # Phase 9 Slice 9.3 — widen for HL7 v2 fixtures (deferred from Slice 9.4).
    "bnp": ("30934-4", "Natriuretic peptide.B [Mass/volume] in Serum or Plasma"),
    "nt_probnp": ("33762-6", "Natriuretic peptide.B prohormone N-Terminal [Mass/volume] in Serum or Plasma"),
    "egfr": ("33914-3", "Glomerular filtration rate/1.73 sq M.predicted [Volume Rate/Area]"),
    "hco3": ("1963-8", "Bicarbonate [Moles/volume] in Serum or Plasma"),
    "bicarbonate": ("1963-8", "Bicarbonate [Moles/volume] in Serum or Plasma"),
    "co2": ("2028-9", "Carbon dioxide [Moles/volume] in Serum or Plasma"),
    "chloride": ("2075-0", "Chloride [Moles/volume] in Serum or Plasma"),
}

_LOINC_FALLBACK: tuple[str, str] = ("LP-UNKNOWN", "Unknown laboratory analyte")


# Same id rule the PHP controller enforces — keep them in sync.
_ID_PATTERN = re.compile(r"^copilot-\d+-[\w.-]+$")


def lookup_loinc(normalized_test_name: str) -> tuple[str, str]:
    """Return ``(code, display)`` for the given normalised lab name.

    Falls back to ``("LP-UNKNOWN", normalized_test_name or fallback display)``
    when no entry exists. The fallback display preserves the model's normalised
    name so a clinician viewing the chart sees something meaningful.
    """
    key = (normalized_test_name or "").strip().lower().replace(" ", "_")
    if key in _LOINC_TABLE:
        return _LOINC_TABLE[key]
    display = normalized_test_name.strip() if normalized_test_name else _LOINC_FALLBACK[1]
    return (_LOINC_FALLBACK[0], display or _LOINC_FALLBACK[1])


def _resolve_loinc_for_lab(lab_value: LabValue) -> tuple[str, str]:
    """Return ``(code, display)`` for a ``LabValue``.

    Prefers ``lab_value.loinc_code`` when set (e.g. carried through from
    HL7 OBX-3.1) — the carried code is authoritative, and we only use the
    name-based ``lookup_loinc`` for the display string. Otherwise fall back
    to name-based lookup. This prevents the UPSERT collapse where multiple
    HL7 OBX rows whose normalised names all resolve to ``LP-UNKNOWN`` would
    share one ``deterministic_observation_id``.
    """
    if lab_value.loinc_code:
        _, display = lookup_loinc(lab_value.normalized_test_name)
        return lab_value.loinc_code, display
    return lookup_loinc(lab_value.normalized_test_name)


def deterministic_observation_id(
    document_id: str | int,
    loinc_code: str,
    *,
    slug: str = "",
) -> str:
    """Deterministic id: ``copilot-{document_id}-{loinc_code}[-{slug}]``.

    The PHP endpoint enforces ``r"^copilot-\\d+-[\\w.-]+$"``; we sanitise the
    LOINC code (strip anything outside the allowed character set) so a stray
    code like ``"LP UNKNOWN"`` doesn't 400 the upsert.

    ``slug`` is an optional per-row distinguisher appended after the LOINC.
    Use it when multiple distinct ``LabValue``s in the same document can
    share one LOINC — e.g. a CBC report whose panel header is "LOINC
    58410-2 (CBC WITH DIFFERENTIAL)" and the LLM stamps that single panel
    code onto every per-test ``LabValue.loinc_code``. Without a slug all
    rows would collapse onto one ``(document_reference_id, target_resource_id)``
    via the ``copilot_pending_extractions_pending_unique_idx`` partial
    unique index. With a slug (typically ``normalized_test_name``), rows
    get distinct ids: ``copilot-450-58410-2-wbc``, ``…-rbc``, ``…-hgb``…

    Backward compatible: callers that omit ``slug`` get the original
    ``copilot-{doc}-{loinc}`` shape, so existing single-LOINC ingests
    (HL7 OBX-3.1 carry-through, name-resolved single LOINCs) keep their
    deterministic ids and the UPSERT semantics for re-extraction still
    overwrite the same row.
    """
    sanitised = re.sub(r"[^\w.-]+", "-", str(loinc_code).strip())
    if not sanitised:
        sanitised = "unknown"
    sanitised_slug = re.sub(r"[^\w.-]+", "-", str(slug or "").strip()).strip("-")
    if sanitised_slug:
        return f"copilot-{document_id}-{sanitised}-{sanitised_slug}"
    return f"copilot-{document_id}-{sanitised}"


def _custom_observation_url() -> str:
    return (
        settings.openemr_base_url.rstrip("/")
        + "/interface/modules/custom_modules/oe-module-clinical-copilot/public/observation.php"
    )


def _custom_condition_url() -> str:
    """Companion to ``_custom_observation_url`` for the FHIR Condition
    endpoint added in Phase 4 of the 2026-05-08 problem_list build.

    Same module, same JWT auth, parallel public shim
    (public/condition.php). Used by ``write_condition`` and the
    ``_perform_write`` short-circuit branch that routes grounded-
    ICD-10 problem_list rows through to FHIR Condition on approve.
    """
    return (
        settings.openemr_base_url.rstrip("/")
        + "/interface/modules/custom_modules/oe-module-clinical-copilot/public/condition.php"
    )


# ── FHIR Condition writer (Phase 4) ─────────────────────────────────────────
#
# Lives alongside the Observation writers in this file rather than in a new
# module. Justification: zero cross-module imports, share the same JWT
# minter (_mint_copilot_jwt) and id pattern (_ID_PATTERN), share the same
# 7-error taxonomy in _perform_write. A new module would force one extra
# import in main.py per call site without any architectural payoff.

_ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
_SNOMED_SYSTEM = "http://snomed.info/sct"
_CONDITION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-category"
_CLINICAL_STATUS_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-clinical"
_VERIFICATION_STATUS_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-ver-status"


def deterministic_condition_id(document_id: str | int, code_or_idx: str) -> str:
    """Deterministic id: ``copilot-{document_id}-{code_or_idx}``.

    Same shape as :func:`deterministic_observation_id` so the PHP
    ConditionController's id regex (mirrored from ObservationController:
    ``r"^copilot-\\d+-[\\w.-]+$"``) accepts it. ``code_or_idx`` is
    typically the sanitised ICD-10 code (e.g. ``"I48-91"`` from the dot-
    decimal ``I48.91``) for grounded rows; for code-less rows the caller
    passes ``"cond-{idx}"`` so multiple problems on the same document
    don't collide on a single id.
    """
    sanitised = re.sub(r"[^\w.-]+", "-", str(code_or_idx).strip())
    if not sanitised:
        sanitised = "cond"
    return f"copilot-{document_id}-{sanitised}"


def _build_condition(
    *,
    document_id: str,
    patient_id: str,
    problem: dict[str, Any],
    condition_id: str,
) -> dict[str, Any]:
    """Construct a FHIR R4 Condition body from a ProblemListItem-shaped dict.

    Mirrors ``_build_observation``'s shape contract: the body conforms
    to FHIR R4 + USCDI v3 (Condition Problems & Health Concerns), with
    a private ``_copilot_citations`` extension that the PHP controller
    strips before persisting (citations land in their own column).

    Required by USCDI: clinicalStatus, verificationStatus, category,
    code (or code.text), subject. We default verificationStatus to
    'confirmed' here because every approved Co-Pilot row has been
    clinician-reviewed; v1.5 may downgrade to 'unconfirmed' if rows
    sit in pending state for too long.
    """
    icd10 = problem.get("icd10_code")
    snomed = problem.get("snomed_code")
    condition_text = str(problem.get("condition") or "Problem")

    codings: list[dict[str, Any]] = []
    if isinstance(icd10, str) and icd10.strip():
        codings.append(
            {"system": _ICD10_SYSTEM, "code": icd10.strip(), "display": condition_text}
        )
    if isinstance(snomed, str) and snomed.strip():
        codings.append(
            {"system": _SNOMED_SYSTEM, "code": snomed.strip(), "display": condition_text}
        )
    code_payload: dict[str, Any] = {"text": condition_text}
    if codings:
        code_payload["coding"] = codings

    clinical_status = (problem.get("status") or "active").lower()
    if clinical_status not in ("active", "resolved", "inactive"):
        clinical_status = "active"

    body: dict[str, Any] = {
        "id": condition_id,
        "resourceType": "Condition",
        "clinicalStatus": {
            "coding": [
                {
                    "system": _CLINICAL_STATUS_SYSTEM,
                    "code": clinical_status,
                    "display": clinical_status.capitalize(),
                }
            ]
        },
        "verificationStatus": {
            "coding": [
                {
                    "system": _VERIFICATION_STATUS_SYSTEM,
                    "code": "confirmed",
                    "display": "Confirmed",
                }
            ]
        },
        "category": [
            {
                "coding": [
                    {
                        "system": _CONDITION_CATEGORY_SYSTEM,
                        "code": "problem-list-item",
                        "display": "Problem List Item",
                    }
                ]
            }
        ],
        "code": code_payload,
        "subject": {"reference": f"Patient/{patient_id}"},
        "derivedFrom": [
            {"reference": f"DocumentReference/copilot-{document_id}"}
        ],
    }

    onset_raw = problem.get("onset_date")
    if isinstance(onset_raw, str) and onset_raw.strip():
        onset_clean = onset_raw.strip()
        # FHIR R4 dateTime stems: YYYY, YYYY-MM, YYYY-MM-DD, or full
        # ISO timestamp. Accept all four; everything else (e.g. "~2018",
        # "adolescence", "Unknown") falls into onsetString so the
        # imprecise value survives the round-trip without being coerced.
        if re.match(
            r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?)?)?)?$",
            onset_clean,
        ):
            # If it's a bare year / year-month / year-month-day, leave
            # it as-is. FHIR-compliant.
            body["onsetDateTime"] = onset_clean
        else:
            body["onsetString"] = onset_clean

    citations = problem.get("citations") or []
    if isinstance(citations, list) and citations:
        body["_copilot_citations"] = [
            {
                "bbox_id": cit.get("field_or_chunk_id") if isinstance(cit, dict) else None,
                "quote_or_value": (
                    cit.get("quote_or_value") if isinstance(cit, dict) else None
                ),
                "page": (
                    cit.get("page_or_section") if isinstance(cit, dict) else None
                ),
            }
            for cit in citations
        ]

    return body


async def write_condition(
    *,
    document_id: str,
    patient_id: str,
    problem: dict[str, Any],
    condition_id: str | None = None,
) -> dict[str, Any]:
    """POST one FHIR Condition to the custom Co-Pilot endpoint.

    Parallel to :func:`write_observation` — same JWT, same id rule,
    same 7-error taxonomy lifted into the response shape. Raises
    ``RuntimeError`` on jwt mint failure or non-2xx response.
    """
    if condition_id is None:
        icd10 = problem.get("icd10_code")
        if isinstance(icd10, str) and icd10.strip():
            condition_id = deterministic_condition_id(document_id, icd10)
        else:
            # Fall back to the row's index when the caller didn't pass
            # a deterministic suffix. Rare path — the dispatcher branch
            # always supplies one.
            condition_id = deterministic_condition_id(document_id, "cond")

    if _ID_PATTERN.match(condition_id) is None:
        raise ValueError("condition_id failed copilot id pattern")

    token = _mint_copilot_jwt()
    if token is None:
        raise RuntimeError("copilot_jwt_secret unset — condition write skipped")

    body = _build_condition(
        document_id=document_id,
        patient_id=patient_id,
        problem=problem,
        condition_id=condition_id,
    )

    url = _custom_condition_url()
    t0 = _dt.datetime.now(_dt.timezone.utc)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/fhir+json",
                "Accept": "application/json",
            },
        )
    duration_ms = int(
        (_dt.datetime.now(_dt.timezone.utc) - t0).total_seconds() * 1000
    )

    if response.status_code >= 400:
        _logger.warning(
            "condition_write_failed",
            extra={
                "condition_id": condition_id,
                "document_id": document_id,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        raise RuntimeError(
            f"condition write returned status {response.status_code}"
        )

    payload = response.json() if response.content else {}
    if not isinstance(payload, dict):
        payload = {}

    _logger.info(
        "condition_write_ok",
        extra={
            "condition_id": condition_id,
            "document_id": document_id,
            "icd10_code": problem.get("icd10_code"),
            "action": payload.get("action"),
            "duration_ms": duration_ms,
        },
    )
    return payload


def _value_quantity(lab_value: LabValue) -> dict[str, Any] | None:
    """Build a FHIR ``valueQuantity`` from a LabValue, if numeric."""
    raw = (lab_value.value or "").strip()
    if not raw:
        return None
    # Strip leading comparators / spaces; keep first token.
    head = raw.split()[0].lstrip("<>=~")
    try:
        numeric = float(head)
    except ValueError:
        return None
    out: dict[str, Any] = {"value": numeric}
    unit = lab_value.normalized_unit or lab_value.unit
    if unit:
        out["unit"] = unit
    return out


def _build_observation(
    *,
    document_id: str,
    patient_id: str,
    lab_value: LabValue,
    observation_id: str,
    loinc: tuple[str, str],
) -> dict[str, Any]:
    code, display = loinc
    body: dict[str, Any] = {
        "id": observation_id,
        "resourceType": "Observation",
        "status": "final",
        "code": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": code,
                    "display": display,
                }
            ]
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "derivedFrom": [
            {"reference": f"DocumentReference/copilot-{document_id}"}
        ],
    }

    vq = _value_quantity(lab_value)
    if vq is not None:
        body["valueQuantity"] = vq
    else:
        body["valueString"] = lab_value.value

    if lab_value.collection_date is not None:
        # collection_date is a date; FHIR effectiveDateTime needs at least
        # "YYYY-MM-DD". Append midnight UTC so the PHP DATETIME column has a
        # parseable timestamp.
        body["effectiveDateTime"] = f"{lab_value.collection_date.isoformat()}T00:00:00Z"

    if lab_value.abnormal_flag and lab_value.abnormal_flag != "unknown":
        body["interpretation"] = [
            {"text": lab_value.abnormal_flag}
        ]

    if lab_value.reference_range:
        body["referenceRange"] = [{"text": lab_value.reference_range}]

    # Private extension — citations live on the resource so the PHP side
    # can persist them in the citations column. Stripped from the
    # persisted FHIR body by the PHP controller.
    body["_copilot_citations"] = [
        {
            "bbox_id": cit.field_or_chunk_id,
            "quote_or_value": cit.quote_or_value,
            "page": cit.page_or_section,
        }
        for cit in lab_value.citations
    ]
    return body


async def write_observation(
    *,
    document_id: str,
    patient_id: str,
    lab_value: LabValue,
    observation_id: str | None = None,
) -> dict[str, Any]:
    """POST one FHIR Observation to the custom Co-Pilot endpoint.

    Returns the JSON envelope from the endpoint
    (``{id, document_id, patient_id, action}``). Raises ``RuntimeError`` if
    the JWT secret is unset/short or if the endpoint returns non-2xx.
    Callers MUST swallow the exception and surface a soft-warn — see
    ``main.document_ingest``.
    """
    code, display = _resolve_loinc_for_lab(lab_value)
    if observation_id is None:
        # Slug = normalized_test_name so panel-LOINC reports (e.g. CBC where
        # the LLM stamps 58410-2 onto every LabValue) produce distinct ids
        # per row instead of collapsing onto one via the pending-state
        # partial unique index. See deterministic_observation_id() docstring.
        observation_id = deterministic_observation_id(
            document_id, code, slug=lab_value.normalized_test_name
        )

    if _ID_PATTERN.match(observation_id) is None:
        raise ValueError("observation_id failed copilot id pattern")

    token = _mint_copilot_jwt()
    if token is None:
        raise RuntimeError("copilot_jwt_secret unset — observation write skipped")

    body = _build_observation(
        document_id=document_id,
        patient_id=patient_id,
        lab_value=lab_value,
        observation_id=observation_id,
        loinc=(code, display),
    )

    url = _custom_observation_url()
    t0 = _dt.datetime.now(_dt.timezone.utc)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/fhir+json",
                "Accept": "application/json",
            },
        )
    duration_ms = int(
        (_dt.datetime.now(_dt.timezone.utc) - t0).total_seconds() * 1000
    )

    if response.status_code >= 400:
        _logger.warning(
            "observation_write_failed",
            extra={
                "observation_id": observation_id,
                "document_id": document_id,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        raise RuntimeError(
            f"observation write returned status {response.status_code}"
        )

    payload = response.json() if response.content else {}
    if not isinstance(payload, dict):
        payload = {}

    _logger.info(
        "observation_write_ok",
        extra={
            "observation_id": observation_id,
            "document_id": document_id,
            "loinc_code": code,
            "action": payload.get("action"),
            "duration_ms": duration_ms,
        },
    )
    return payload


# ─────────────────── Phase 9 Slice 9.3 — Pending-write siblings ──────────────
#
# All derived writes from Phase 9 forward stage by default. The pre-existing
# W2 PDF/PNG ingest path keeps using ``write_observation`` directly for
# back-compat — see ``main.py:1763`` (do not migrate that call site).
#
# 7-error taxonomy for ``approved → written`` failures (Slice 9.3):
#
#   php_5xx                     — Observation endpoint returned 5xx
#   php_4xx                     — Observation endpoint returned 4xx
#   network_timeout             — httpx.TimeoutException (read/connect)
#   network_unreachable         — httpx.ConnectError / DNS failures
#   jwt_mint_failed             — _mint_copilot_jwt returned None / raised
#   task_writer_unavailable     — target_resource_type='Task' (no PHP writer)
#   allergy_writer_unavailable  — target_resource_type='AllergyIntolerance'
#                                  (no PHP writer)
#   payload_invalid             — body shape failed local validation
#
# The PHP ObservationController accepts ONLY ``resourceType='Observation'``
# (ObservationController.php:85). Tasks and AllergyIntolerance stage
# successfully but transition straight to ``failed`` with the matching
# write_error string when the watchdog or approve endpoint dispatches them.

from typing import Literal as _Literal

WriteOutcome = _Literal["written", "failed"]


async def _perform_write(row: dict[str, Any]) -> tuple[WriteOutcome, str | None]:
    """Shared writer used by approve endpoint + stuck-approved watchdog reaper.

    Walks the 7-error taxonomy. Returns ``("written", None)`` on success
    and ``("failed", write_error)`` on every taxonomised failure. The
    caller decides whether to record the result via
    ``staging.store.mark_written`` / ``mark_failed``.

    The function never raises — taxonomy gaps fall through to
    ``"payload_invalid"`` so the caller always has a stable string.
    """
    target_type = str(row.get("target_resource_type") or "")
    if target_type == "IntakeFormField":
        # Phase 4 of the 2026-05-08 problem_list build — IntakeFormField
        # rows whose target_resource_id slug is 'problem_list' AND whose
        # payload carries a grounded ICD-10 code route through to the
        # FHIR Condition writer. Other intake field kinds (allergies,
        # medications, demographics, family hx, chief concern, code
        # status) and code-less problem_list rows continue to no-op-
        # write as before — approval is informational, the extracted
        # text is citable from the document store, no discrete FHIR
        # write happens.
        target_id = str(row.get("target_resource_id") or "")
        payload = row.get("payload") or {}
        if (
            isinstance(payload, dict)
            and "intake-problem_list-" in target_id
            and isinstance(payload.get("icd10_code"), str)
            and payload.get("icd10_code").strip()
        ):
            outcome, write_error = await _problem_list_condition_write(
                target_id=target_id,
                payload=payload,
                row=row,
            )
            return outcome, write_error
        return "written", None
    if target_type == "Task":
        return "failed", "task_writer_unavailable"
    if target_type == "AllergyIntolerance":
        return "failed", "allergy_writer_unavailable"
    if target_type != "Observation":
        return "failed", "payload_invalid"

    payload = row.get("payload")
    if not isinstance(payload, dict):
        return "failed", "payload_invalid"

    body = dict(payload)
    body.setdefault("resourceType", "Observation")
    obs_id = str(row.get("target_resource_id") or "")
    if not obs_id or _ID_PATTERN.match(obs_id) is None:
        return "failed", "payload_invalid"
    body["id"] = obs_id

    try:
        token = _mint_copilot_jwt()
    except Exception as exc:
        _logger.warning(
            "observation_write_jwt_mint_raised",
            extra={"observation_id": obs_id, "error_type": type(exc).__name__},
        )
        return "failed", "jwt_mint_failed"
    if token is None:
        return "failed", "jwt_mint_failed"

    url = _custom_observation_url()
    t0 = _dt.datetime.now(_dt.timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/fhir+json",
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException:
        return "failed", "network_timeout"
    except httpx.ConnectError:
        return "failed", "network_unreachable"
    except httpx.HTTPError as exc:
        _logger.warning(
            "observation_write_http_error",
            extra={"observation_id": obs_id, "error_type": type(exc).__name__},
        )
        return "failed", "network_unreachable"
    except Exception as exc:
        _logger.warning(
            "observation_write_unexpected",
            extra={"observation_id": obs_id, "error_type": type(exc).__name__},
        )
        return "failed", "payload_invalid"

    duration_ms = int(
        (_dt.datetime.now(_dt.timezone.utc) - t0).total_seconds() * 1000
    )

    if 400 <= response.status_code < 500:
        _logger.warning(
            "observation_write_4xx",
            extra={
                "observation_id": obs_id,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return "failed", "php_4xx"
    if response.status_code >= 500:
        _logger.warning(
            "observation_write_5xx",
            extra={
                "observation_id": obs_id,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return "failed", "php_5xx"

    _logger.info(
        "observation_write_ok_via_staging",
        extra={
            "observation_id": obs_id,
            "duration_ms": duration_ms,
        },
    )
    return "written", None


async def _problem_list_condition_write(
    *,
    target_id: str,
    payload: dict[str, Any],
    row: dict[str, Any],
) -> tuple[WriteOutcome, str | None]:
    """Approve-time hook for grounded-ICD-10 problem_list rows.

    Maps the Condition write onto the same 7-error taxonomy
    ``_perform_write`` uses for Observations so the staging watchdog
    + retry loops see a stable string for failures. We re-derive the
    document_id and patient_id from the row (not the body) so this
    function never needs the agent-api request_id_var or any global
    state.
    """
    # target_id format: copilot-{document_id}-intake-problem_list-{idx}
    m = re.match(r"^copilot-(\d+)-intake-problem_list-(\d+)$", target_id)
    if m is None:
        return "failed", "payload_invalid"
    document_id = m.group(1)
    idx = m.group(2)
    patient_id = str(row.get("patient_id") or "")
    if not patient_id:
        return "failed", "payload_invalid"

    # UUID → numeric pid resolution. The PHP ConditionController's
    # subject regex /^Patient\\/(\\w+)$/ rejects hyphens (UUIDs), and
    # the patient_id INT column expects a numeric pid. The pending
    # row stores whatever the form sent at ingest time (UUID for
    # iframe ingests, numeric for tests), so we always resolve here
    # rather than assume the caller already did. We resolve via a
    # direct MySQL query against patient_data — the FHIR layer
    # surfaces the same UUID back to us, not the pid, so it can't
    # help. Reuses the same aiomysql plumbing
    # `read_observations_for_document` already proved (single
    # short-lived connection per call; OpenEMR-pool credentials).
    if "-" in patient_id:
        resolved = await _resolve_pid_from_uuid(patient_id)
        if resolved is None:
            _logger.warning(
                "condition_write_pid_resolve_failed",
                extra={
                    "target_id": target_id,
                    "patient_uuid_prefix": patient_id[:8],
                },
            )
            return "failed", "payload_invalid"
        patient_id = resolved

    icd10 = payload.get("icd10_code") or ""
    # Sanitised id derivation: prefer ICD-10 (deterministic across re-
    # extractions of the same problem on the same document); fall back
    # to the row index for code-less rows (this branch shouldn't fire
    # because the caller gates on grounded icd10, but kept for defense).
    suffix = icd10.strip() if isinstance(icd10, str) and icd10.strip() else f"cond-{idx}"
    condition_id = deterministic_condition_id(document_id, suffix)

    try:
        body = _build_condition(
            document_id=document_id,
            patient_id=patient_id,
            problem=payload,
            condition_id=condition_id,
        )
    except Exception as exc:
        _logger.warning(
            "condition_write_build_failed",
            extra={"target_id": target_id, "error_type": type(exc).__name__},
        )
        return "failed", "payload_invalid"

    if _ID_PATTERN.match(condition_id) is None:
        return "failed", "payload_invalid"

    try:
        token = _mint_copilot_jwt()
    except Exception as exc:
        _logger.warning(
            "condition_write_jwt_mint_raised",
            extra={"condition_id": condition_id, "error_type": type(exc).__name__},
        )
        return "failed", "jwt_mint_failed"
    if token is None:
        return "failed", "jwt_mint_failed"

    url = _custom_condition_url()
    t0 = _dt.datetime.now(_dt.timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/fhir+json",
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException:
        return "failed", "network_timeout"
    except httpx.ConnectError:
        return "failed", "network_unreachable"
    except httpx.HTTPError as exc:
        _logger.warning(
            "condition_write_http_error",
            extra={"condition_id": condition_id, "error_type": type(exc).__name__},
        )
        return "failed", "network_unreachable"
    except Exception as exc:
        _logger.warning(
            "condition_write_unexpected",
            extra={"condition_id": condition_id, "error_type": type(exc).__name__},
        )
        return "failed", "payload_invalid"

    duration_ms = int(
        (_dt.datetime.now(_dt.timezone.utc) - t0).total_seconds() * 1000
    )
    if 400 <= response.status_code < 500:
        _logger.warning(
            "condition_write_4xx",
            extra={
                "condition_id": condition_id,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return "failed", "php_4xx"
    if response.status_code >= 500:
        _logger.warning(
            "condition_write_5xx",
            extra={
                "condition_id": condition_id,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return "failed", "php_5xx"
    _logger.info(
        "condition_write_ok_via_staging",
        extra={
            "condition_id": condition_id,
            "duration_ms": duration_ms,
        },
    )
    return "written", None


async def stage_observation(
    *,
    document_id: str,
    patient_id: str,
    lab_value: LabValue,
    file_batch_id: str,
    document_reference_id: str,
    locator: str | None = None,
    source_format: str = "pdf",
    observation_id: str | None = None,
    request_id: str | None = None,
    provider_id: str | None = None,
) -> int:
    """Stage one FHIR Observation derived from a ``LabValue``.

    Builds the same body ``write_observation`` would POST, but inserts a
    ``state='pending'`` row in ``copilot_pending_extractions`` instead of
    firing the HTTP write. Returns the integer ``pending_id``.
    """
    from staging import store as _staging_store  # local — keep cycle-free

    code, display = _resolve_loinc_for_lab(lab_value)
    if observation_id is None:
        # Slug = normalized_test_name so panel-LOINC reports (e.g. CBC where
        # the LLM stamps 58410-2 onto every LabValue) produce distinct ids
        # per row instead of collapsing onto one via the pending-state
        # partial unique index. See deterministic_observation_id() docstring.
        observation_id = deterministic_observation_id(
            document_id, code, slug=lab_value.normalized_test_name
        )
    if _ID_PATTERN.match(observation_id) is None:
        raise ValueError("observation_id failed copilot id pattern")

    body = _build_observation(
        document_id=document_id,
        patient_id=patient_id,
        lab_value=lab_value,
        observation_id=observation_id,
        loinc=(code, display),
    )
    # `_build_observation` puts citations under `_copilot_citations` (with a
    # `bbox_id` field name) — the FHIR-shape extension that the PHP
    # controller persists into its own column on real writes. The agent-ui's
    # `_findFirstCitation` walker only looks for arrays under the key
    # `citations` (with `field_or_chunk_id`), so without a top-level
    # `citations[]` the review panel never finds them and the document
    # bbox-highlight stays blank. Add the LabValue's original citations[]
    # at the payload root for the staging path only — this row is
    # never POSTed to FHIR (informational/staging-side artifact). Mirrors
    # the same shape every IntakeFormField row already carries
    # (medications, allergies, family_history all stage their model_dump
    # whose citations[] sits at the root).
    body["citations"] = [c.model_dump(mode="json") for c in lab_value.citations]
    return await _staging_store.stage_pending(
        document_reference_id=document_reference_id,
        file_batch_id=file_batch_id,
        patient_id=patient_id,
        source_format=source_format,
        target_resource_type="Observation",
        target_resource_id=observation_id,
        payload=body,
        locator=locator,
        confidence=None,
        request_id=request_id,
        provider_id=provider_id,
    )


async def stage_task(
    *,
    document_id: str,
    patient_id: str,
    file_batch_id: str,
    document_reference_id: str,
    measure: str,
    status: str,
    description: str | None = None,
    locator: str | None = None,
    source_format: str = "xlsx",
    request_id: str | None = None,
    provider_id: str | None = None,
) -> int:
    """Stage a FHIR Task derived from an XLSX Care_Gaps row.

    NOTE — the v1 PHP ObservationController accepts ONLY
    ``resourceType='Observation'`` (see ObservationController.php:85). On
    ``approved → written`` dispatch this row transitions to ``failed``
    with ``write_error='task_writer_unavailable'``. The schema still
    carries the payload from day one so a v1.5 Task writer can land
    without a re-stage round-trip.
    """
    from staging import store as _staging_store

    sanitised = re.sub(r"[^\w.-]+", "-", measure.strip()) or "task"
    target_id = f"copilot-{document_id}-{sanitised}"
    body: dict[str, Any] = {
        "id": target_id,
        "resourceType": "Task",
        "status": "requested",
        "intent": "order",
        "for": {"reference": f"Patient/{patient_id}"},
        "focus": {"reference": f"DocumentReference/copilot-{document_id}"},
        "code": {"text": measure},
        "businessStatus": {"text": status},
    }
    if description:
        body["description"] = description

    return await _staging_store.stage_pending(
        document_reference_id=document_reference_id,
        file_batch_id=file_batch_id,
        patient_id=patient_id,
        source_format=source_format,
        target_resource_type="Task",
        target_resource_id=target_id,
        payload=body,
        locator=locator,
        confidence=None,
        request_id=request_id,
        provider_id=provider_id,
    )


async def stage_allergy(
    *,
    document_id: str,
    patient_id: str,
    file_batch_id: str,
    document_reference_id: str,
    substance: str,
    reaction: str | None = None,
    locator: str | None = None,
    source_format: str = "xlsx",
    request_id: str | None = None,
    provider_id: str | None = None,
) -> int:
    """Stage a FHIR AllergyIntolerance from an XLSX Patient sheet row.

    NOTE — the v1 PHP ObservationController accepts ONLY
    ``resourceType='Observation'`` (see ObservationController.php:85). On
    ``approved → written`` dispatch this row transitions to ``failed``
    with ``write_error='allergy_writer_unavailable'``. The schema still
    carries the payload so a v1.5 AllergyIntolerance writer can land
    without a re-stage.
    """
    from staging import store as _staging_store

    sanitised = re.sub(r"[^\w.-]+", "-", substance.strip()) or "allergy"
    target_id = f"copilot-{document_id}-allergy-{sanitised}"
    body: dict[str, Any] = {
        "id": target_id,
        "resourceType": "AllergyIntolerance",
        "clinicalStatus": {"text": "active"},
        "verificationStatus": {"text": "unconfirmed"},
        "patient": {"reference": f"Patient/{patient_id}"},
        "code": {"text": substance},
    }
    if reaction:
        body["reaction"] = [{"manifestation": [{"text": reaction}]}]

    return await _staging_store.stage_pending(
        document_reference_id=document_reference_id,
        file_batch_id=file_batch_id,
        patient_id=patient_id,
        source_format=source_format,
        target_resource_type="AllergyIntolerance",
        target_resource_id=target_id,
        payload=body,
        locator=locator,
        confidence=None,
        request_id=request_id,
        provider_id=provider_id,
    )


async def stage_pertinent_lab(
    *,
    document_id: str,
    patient_id: str,
    file_batch_id: str,
    document_reference_id: str,
    lab_value: LabValue,
    field_index: int,
    source_format: str = "pdf",
    request_id: str | None = None,
    provider_id: str | None = None,
) -> int:
    """Stage one ``IntakeFormField`` row for a ``pertinent_labs`` entry.

    The payload is the FHIR-Observation body that ``_build_observation``
    produces, so the existing ``'lab'`` UI editor (``LabInputs``,
    ``_shortLabelFor``, ``_labelFor``) renders without modification — it
    already reads ``code.coding[0].display`` + ``valueQuantity.value/unit``.

    Provenance stays distinct from real lab-report Observations:
    ``target_resource_type='IntakeFormField'`` (informational approval, no
    FHIR write), and the deterministic id includes ``intake-lab-`` so the
    UI's ``_kindFromRow`` regex picks the ``'lab'`` editor branch.

    Approval is a no-op write (``_perform_write`` short-circuits
    ``IntakeFormField`` to ``("written", None)``) — same semantics as
    every other intake field today.
    """
    from staging import store as _staging_store

    code, display = _resolve_loinc_for_lab(lab_value)
    target_resource_id = f"copilot-{document_id}-intake-lab-{field_index}"
    body = _build_observation(
        document_id=document_id,
        patient_id=patient_id,
        lab_value=lab_value,
        observation_id=target_resource_id,
        loinc=(code, display),
    )
    # `_build_observation` puts citations under `_copilot_citations` (with
    # `bbox_id` field name), but the UI's `_findFirstCitation` walker only
    # looks for arrays under the key `citations` (with `field_or_chunk_id`).
    # IntakeFormField rows never POST to FHIR — `_perform_write` short-
    # circuits to ("written", None) — so adding the canonical citations
    # array is safe and makes the docx preview's paraIdx-based highlight
    # resolve. Mirrors the shape that every other IntakeFormField row
    # carries (medications, allergies, family_history all stage their
    # `model_dump` whose `citations` array sits at the payload root).
    body["citations"] = [c.model_dump(mode="json") for c in lab_value.citations]
    locator = (
        lab_value.citations[0].field_or_chunk_id
        if lab_value.citations
        else None
    )
    return await _staging_store.stage_pending(
        document_reference_id=document_reference_id,
        file_batch_id=file_batch_id,
        patient_id=patient_id,
        source_format=source_format,
        target_resource_type="IntakeFormField",  # type: ignore[arg-type]
        target_resource_id=target_resource_id,
        payload=body,
        locator=locator,
        confidence=None,
        request_id=request_id,
        provider_id=provider_id,
    )


async def stage_intake_field(
    *,
    document_id: str,
    patient_id: str,
    file_batch_id: str,
    document_reference_id: str,
    field_kind: str,
    field_index: int,
    payload: dict[str, Any],
    locator: str | None = None,
    source_format: str = "pdf",
    request_id: str | None = None,
    provider_id: str | None = None,
) -> int:
    """Stage one ``IntakeFormField`` row in ``copilot_pending_extractions``.

    ``field_kind`` is one of ``allergy | medication | demographics |
    family_history | chief_concern | code_status | key_fact``. The
    deterministic ``target_resource_id`` is
    ``copilot-{document_id}-intake-{field_kind}-{field_index}``.

    The approve transition for ``IntakeFormField`` rows is informational
    (no FHIR write) — the extracted text becomes chat-citable from the
    document store directly. The approve→write dispatcher in
    ``staging/router.py`` short-circuits this target_resource_type.
    """
    from staging import store as _staging_store

    sanitised_kind = re.sub(r"[^\w.-]+", "-", field_kind.strip()) or "field"
    target_resource_id = (
        f"copilot-{document_id}-intake-{sanitised_kind}-{field_index}"
    )
    pending_id = await _staging_store.stage_pending(
        document_reference_id=document_reference_id,
        file_batch_id=file_batch_id,
        patient_id=patient_id,
        source_format=source_format,
        target_resource_type="IntakeFormField",  # type: ignore[arg-type]
        target_resource_id=target_resource_id,
        payload=payload,
        locator=locator,
        confidence=None,
        request_id=request_id,
        provider_id=provider_id,
    )
    return pending_id


# ─────────────────── Post-approval-context — MySQL read-back ─────────────────
#
# The post-approval-context route needs to fan out RAG against approved
# ``copilot_observations`` rows for a given document. The eval runner has a
# similar one-shot reader (``evals/runner.py:583``) but uses a separate
# ``COPILOT_OBSERVATIONS_MYSQL_URL`` env knob. The shared OpenEMR pool used
# by ``audit/openemr_log.py`` is the correct surface for production reads.

_OBS_READBACK_SQL = (
    "SELECT id, fhir_resource FROM copilot_observations WHERE document_id=%s"
)

# 2026-05-08 — UUID → pid resolver used by _problem_list_condition_write.
# OpenEMR's patient_data.uuid is stored as a 16-byte BINARY column;
# the FHIR layer hands us the dash-separated 36-char string form. We
# unhex(replace(...)) to land in the binary form for the WHERE clause.
_PID_RESOLVE_SQL = (
    "SELECT pid FROM patient_data WHERE uuid = UNHEX(REPLACE(%s, '-', ''))"
)


async def _resolve_pid_from_uuid(patient_uuid: str) -> Optional[str]:
    """Return the numeric pid for a patient_data UUID, or None on miss.

    Used by the Condition write path because the PHP controller
    expects a numeric pid in subject.reference (its regex
    /^Patient\\/(\\w+)$/ rejects hyphens) and patient_id is an INT
    column. Mirrors the soft-fail discipline of
    ``read_observations_for_document``: any error returns None and
    the caller surfaces ``payload_invalid`` rather than crashing the
    approve flow.
    """
    try:
        import aiomysql  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover — optional dep
        return None
    try:
        conn = await aiomysql.connect(
            host=settings.openemr_db_host,
            port=settings.openemr_db_port,
            user=settings.openemr_db_user,
            password=settings.openemr_db_password,
            db=settings.openemr_db_name,
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001 — soft path
        _logger.warning(
            "pid_resolve_connect_failed",
            extra={"error_type": type(exc).__name__},
        )
        return None
    try:
        cur = await conn.cursor()
        await cur.execute(_PID_RESOLVE_SQL, (patient_uuid,))
        row = await cur.fetchone()
        if row is None:
            return None
        pid = row[0]
        if pid is None:
            return None
        return str(int(pid))
    except Exception as exc:  # noqa: BLE001 — soft path
        _logger.warning(
            "pid_resolve_query_failed",
            extra={"error_type": type(exc).__name__},
        )
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def read_observations_for_document(
    document_id: str | int,
) -> list[dict[str, Any]]:
    """Read ``copilot_observations`` rows for a given numeric ``document_id``.

    Returns ``[{"id", "fhir_resource", "loinc_code", "display", "value"}, ...]``.
    Returns ``[]`` if MySQL is unreachable, the table is empty, or aiomysql
    is not installed — callers must treat this as a soft path (RAG can
    still fire on intake-field text).
    """
    import json as _json

    try:
        import aiomysql  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover — optional dep
        return []

    # Reuse the same connection settings as audit/openemr_log.py — that pool
    # already targets the OpenEMR app DB where copilot_observations lives.
    try:
        conn = await aiomysql.connect(
            host=settings.openemr_db_host,
            port=settings.openemr_db_port,
            user=settings.openemr_db_user,
            password=settings.openemr_db_password,
            db=settings.openemr_db_name,
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001 — soft path
        _logger.warning(
            "observation_readback_connect_failed",
            extra={"error_type": type(exc).__name__},
        )
        return []

    out: list[dict[str, Any]] = []
    try:
        cur = await conn.cursor()
        await cur.execute(_OBS_READBACK_SQL, (str(document_id),))
        rows = await cur.fetchall()
        for row in rows or []:
            obs_id = row[0]
            try:
                fhir_resource = (
                    _json.loads(row[1])
                    if isinstance(row[1], (str, bytes))
                    else (row[1] or {})
                )
            except Exception:
                fhir_resource = {}
            if not isinstance(fhir_resource, dict):
                fhir_resource = {}

            loinc_code = ""
            display = ""
            coding = (
                (fhir_resource.get("code") or {}).get("coding")
                if isinstance(fhir_resource.get("code"), dict)
                else None
            )
            if isinstance(coding, list) and coding:
                first = coding[0]
                if isinstance(first, dict):
                    loinc_code = str(first.get("code") or "")
                    display = str(first.get("display") or "")

            value: Any = None
            vq = fhir_resource.get("valueQuantity")
            if isinstance(vq, dict):
                value = vq.get("value")
            elif "valueString" in fhir_resource:
                value = fhir_resource.get("valueString")

            out.append(
                {
                    "id": obs_id,
                    "fhir_resource": fhir_resource,
                    "loinc_code": loinc_code,
                    "display": display,
                    "value": value,
                }
            )
    except Exception as exc:  # noqa: BLE001 — soft path
        _logger.warning(
            "observation_readback_query_failed",
            extra={"error_type": type(exc).__name__},
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return out


__all__ = [
    "deterministic_condition_id",
    "deterministic_observation_id",
    "lookup_loinc",
    "read_observations_for_document",
    "stage_allergy",
    "stage_intake_field",
    "stage_observation",
    "stage_task",
    "write_condition",
    "write_observation",
]
