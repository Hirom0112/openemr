"""Tests for ``observations.writer`` (W2 Phase 2).

The custom-endpoint POST is stubbed via ``httpx.MockTransport`` so no real
network IO happens. We assert id determinism, LOINC fallback, payload
shape, ``derivedFrom`` provenance, and the privacy invariant that no test
record contains the raw lab value.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.schemas import Citation, LabValue  # noqa: E402
from observations.writer import (  # noqa: E402
    deterministic_observation_id,
    lookup_loinc,
    write_observation,
)

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]


_VALUE_SENTINEL = "4.2-LACTATE-PRIVACY-CHECK"


def _make_lab_value(
    *,
    test_name: str = "Lactate",
    normalized_test_name: str = "lactate",
    value: str = "4.2",
    unit: str = "mmol/L",
    citation_quote: str = _VALUE_SENTINEL,
) -> LabValue:
    return LabValue(
        test_name=test_name,
        normalized_test_name=normalized_test_name,
        value=value,
        unit=unit,
        normalized_unit=unit,
        reference_range="0.5-2.2",
        collection_date=_dt.date(2026, 5, 4),
        abnormal_flag="high",
        citations=[
            Citation(
                source_type="document",
                source_id="copilot-117",
                page_or_section="1",
                field_or_chunk_id="p1-b042",
                quote_or_value=citation_quote,
            )
        ],
    )


class _PatchedAsyncClient(httpx.AsyncClient):
    _handler: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(type(self)._handler)
        super().__init__(*args, **kwargs)


def _patch_httpx(handler) -> Any:
    cls = type(
        "_BoundClient",
        (_PatchedAsyncClient,),
        {"_handler": staticmethod(handler)},
    )
    return patch("observations.writer.httpx.AsyncClient", cls)


# ── lookup_loinc ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected_code",
    [
        ("lactate", "32693-4"),
        ("Lactate", "32693-4"),  # case-insensitive
        ("sodium", "2951-2"),
        ("creatinine", "2160-0"),
        ("hemoglobin", "718-7"),
        ("glucose", "2345-7"),
        ("cholesterol", "2093-3"),
        ("bun", "3094-0"),
        ("hba1c", "4548-4"),
    ],
)
def test_lookup_loinc_known(name: str, expected_code: str) -> None:
    code, display = lookup_loinc(name)
    assert code == expected_code
    assert display


def test_lookup_loinc_falls_back_to_unknown() -> None:
    code, display = lookup_loinc("ferritin_unmapped")
    assert code == "LP-UNKNOWN"
    # Fallback display preserves the model's normalised name so the chart
    # still has something legible.
    assert "ferritin_unmapped" in display


def test_lookup_loinc_empty_string_uses_default_display() -> None:
    code, display = lookup_loinc("")
    assert code == "LP-UNKNOWN"
    assert display


# ── deterministic_observation_id ────────────────────────────────────────────


def test_deterministic_observation_id_basic() -> None:
    assert deterministic_observation_id(117, "2093-3") == "copilot-117-2093-3"
    assert deterministic_observation_id("117", "2093-3") == "copilot-117-2093-3"


def test_deterministic_observation_id_sanitises_unsafe_chars() -> None:
    # Spaces / slashes get mapped to '-' so the PHP regex matches.
    obs_id = deterministic_observation_id(42, "LP UNKNOWN/x")
    assert obs_id == "copilot-42-LP-UNKNOWN-x"


def test_deterministic_observation_id_handles_empty_code() -> None:
    obs_id = deterministic_observation_id(42, "")
    assert obs_id == "copilot-42-unknown"


# ── write_observation: happy path ───────────────────────────────────────────


async def test_write_observation_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "json": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "copilot-117-32693-4",
                "document_id": 117,
                "patient_id": 1,
                "action": "created",
            },
        )

    lab_value = _make_lab_value()

    with _patch_httpx(_handler):
        result = await write_observation(
            document_id="117",
            patient_id="1",
            lab_value=lab_value,
        )

    assert result["id"] == "copilot-117-32693-4"
    assert result["action"] == "created"
    assert len(captured) == 1
    call = captured[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/oe-module-clinical-copilot/public/observation.php")
    assert call["auth"].startswith("Bearer ")

    body = call["json"]
    assert body["resourceType"] == "Observation"
    assert body["id"] == "copilot-117-32693-4"
    assert body["status"] == "final"
    assert body["code"]["coding"][0]["code"] == "32693-4"
    assert body["subject"]["reference"] == "Patient/1"
    # derivedFrom provenance — load-bearing claim.
    assert body["derivedFrom"][0]["reference"] == "DocumentReference/copilot-117"
    # Numeric value preserved.
    assert body["valueQuantity"]["value"] == 4.2
    assert body["valueQuantity"]["unit"] == "mmol/L"
    # Citations attached as the private extension the PHP side persists
    # in its citations column.
    assert isinstance(body["_copilot_citations"], list)
    assert body["_copilot_citations"][0]["bbox_id"] == "p1-b042"


async def test_write_observation_unknown_loinc_uses_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"id": captured[-1]["id"], "document_id": 9, "patient_id": 1, "action": "created"},
        )

    lab_value = _make_lab_value(
        test_name="Ferritin",
        normalized_test_name="ferritin",
        value="123",
        unit="ng/mL",
    )

    with _patch_httpx(_handler):
        await write_observation(
            document_id="9",
            patient_id="1",
            lab_value=lab_value,
        )

    body = captured[0]
    assert body["code"]["coding"][0]["code"] == "LP-UNKNOWN"
    # Sanitised id still satisfies the copilot id pattern.
    assert body["id"] == "copilot-9-LP-UNKNOWN"


async def test_write_observation_non_numeric_uses_value_string(monkeypatch) -> None:
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": captured[-1]["id"], "action": "created"})

    lab_value = _make_lab_value(
        test_name="Blood culture",
        normalized_test_name="blood_culture",
        value="No growth at 48h",
        unit="",
    )

    with _patch_httpx(_handler):
        await write_observation(
            document_id="50",
            patient_id="1",
            lab_value=lab_value,
        )

    body = captured[0]
    assert "valueQuantity" not in body
    assert body["valueString"] == "No growth at 48h"


# ── write_observation: error paths ──────────────────────────────────────────


async def test_write_observation_raises_when_jwt_secret_unset(monkeypatch) -> None:
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "",
    )
    with pytest.raises(RuntimeError):
        await write_observation(
            document_id="117",
            patient_id="1",
            lab_value=_make_lab_value(),
        )


async def test_write_observation_raises_on_4xx(monkeypatch) -> None:
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with _patch_httpx(_handler):
        with pytest.raises(RuntimeError):
            await write_observation(
                document_id="117",
                patient_id="1",
                lab_value=_make_lab_value(),
            )


async def test_write_observation_rejects_invalid_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )
    with pytest.raises(ValueError):
        await write_observation(
            document_id="117",
            patient_id="1",
            lab_value=_make_lab_value(),
            observation_id="not-a-copilot-id",
        )


# ── Privacy ─────────────────────────────────────────────────────────────────


async def test_no_phi_in_logs(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "documents.fhir_writer.settings.copilot_jwt_secret",
        "x" * 32,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "copilot-117-32693-4", "action": "created"})

    lab_value = _make_lab_value(citation_quote=_VALUE_SENTINEL)

    with caplog.at_level(logging.DEBUG, logger="observations.writer"):
        with _patch_httpx(_handler):
            await write_observation(
                document_id="117",
                patient_id="1",
                lab_value=lab_value,
            )

    for record in caplog.records:
        msg = record.getMessage()
        assert _VALUE_SENTINEL not in msg, f"sentinel leaked into msg: {msg}"
        for key, value in record.__dict__.items():
            if isinstance(value, str):
                assert _VALUE_SENTINEL not in value, (
                    f"sentinel leaked into record.{key}={value!r}"
                )
