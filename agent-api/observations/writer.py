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
from typing import Any

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


def deterministic_observation_id(document_id: str | int, loinc_code: str) -> str:
    """Deterministic id: ``copilot-{document_id}-{loinc_code}``.

    The PHP endpoint enforces ``r"^copilot-\\d+-[\\w.-]+$"``; we sanitise the
    LOINC code (strip anything outside the allowed character set) so a stray
    code like ``"LP UNKNOWN"`` doesn't 400 the upsert.
    """
    sanitised = re.sub(r"[^\w.-]+", "-", str(loinc_code).strip())
    if not sanitised:
        sanitised = "unknown"
    return f"copilot-{document_id}-{sanitised}"


def _custom_observation_url() -> str:
    return (
        settings.openemr_base_url.rstrip("/")
        + "/interface/modules/custom_modules/oe-module-clinical-copilot/public/observation.php"
    )


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
        observation_id = deterministic_observation_id(document_id, code)

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
        # Documents-tab redesign: intake_form fields (allergies, medications,
        # demographics, family hx, chief concern, code status) extracted from
        # PDF intake forms and DOCX referral letters have no FHIR writers in
        # this build. Approval is informational — the extracted text is
        # citable from the document store; no discrete FHIR write happens.
        # Returning "written" here lets the row reach a terminal state so the
        # stuck-approved reaper doesn't keep retrying it.
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
        observation_id = deterministic_observation_id(document_id, code)
    if _ID_PATTERN.match(observation_id) is None:
        raise ValueError("observation_id failed copilot id pattern")

    body = _build_observation(
        document_id=document_id,
        patient_id=patient_id,
        lab_value=lab_value,
        observation_id=observation_id,
        loinc=(code, display),
    )
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
    "deterministic_observation_id",
    "lookup_loinc",
    "read_observations_for_document",
    "stage_allergy",
    "stage_intake_field",
    "stage_observation",
    "stage_task",
    "write_observation",
]
