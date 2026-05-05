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
``ARCHITECTURE.md`` §5.2 / §9.2.
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
    code, display = lookup_loinc(lab_value.normalized_test_name)
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


__all__ = [
    "deterministic_observation_id",
    "lookup_loinc",
    "write_observation",
]
