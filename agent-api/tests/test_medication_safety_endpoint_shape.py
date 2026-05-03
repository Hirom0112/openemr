"""GET /medication/safety/{patient_id} response-shape contract test.

Locks the renderer-facing field names (`current_medications`, `allergies`,
`interactions`) so the Meds-button bubble keeps rendering content.  The
frontend type lives at agent-ui/src/types.ts (`MedicationSafetyData`).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_medication_safety_endpoint_returns_renderer_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub FHIR bundle so the endpoint runs without network I/O.
    bundle: dict[str, Any] = {
        "resources": {
            "MedicationRequest": [
                {"medicationCodeableConcept": {"coding": [{"display": "Penicillin G"}], "text": "Penicillin G"}},
                {"medicationCodeableConcept": {"coding": [{"display": "Metoprolol"}], "text": "Metoprolol"}},
            ],
            "AllergyIntolerance": [
                {"code": {"coding": [{"display": "Penicillin"}], "text": "Penicillin"}},
            ],
            "Observation": [],
        }
    }

    async def _fake_bundle(_pid: str) -> dict[str, Any]:
        return bundle

    from auth import fhir_client as fhir_client_module
    monkeypatch.setattr(fhir_client_module.fhir_client, "get_bundle_for_patient", _fake_bundle)

    # Skip the LLM summary call — deterministic safety check still runs.
    async def _no_llm(report: Any, langfuse: Any = None) -> Any:
        report.summary = "stub summary"
        return report

    import agent.tools as tools_pkg
    monkeypatch.setattr(tools_pkg, "add_llm_summary", _no_llm)

    from main import app

    client = TestClient(app)
    resp = client.get("/medication/safety/pt-test-123")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Renderer contract — these three keys are what MedicationSafetyRenderer
    # reads; missing them produces an empty bubble in the chat surface.
    assert isinstance(body.get("current_medications"), list)
    assert isinstance(body.get("allergies"), list)
    assert isinstance(body.get("interactions"), list)

    assert body["current_medications"] == ["Penicillin G", "Metoprolol"]
    assert body["allergies"] == ["Penicillin"]
    # Allergy conflicts surface via the dedicated `allergies` list, not the
    # `interactions` list, so a Penicillin-vs-Penicillin G clash should NOT
    # appear under interactions.
    assert all("ALLERGY_CONFLICT" not in s for s in body["interactions"])

    # Freshness contract — the renderer's "Data as of HH:MM · Refresh"
    # indicator depends on this field. Should be a non-empty ISO-8601 string.
    assert isinstance(body.get("generated_at"), str) and body["generated_at"]


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
def test_medication_safety_force_refresh_param_is_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """?force_refresh=true must reach get_medication_safety as input["force_refresh"]."""
    from typing import Any as _Any

    captured: dict[str, _Any] = {}

    async def _fake_tool(input_dict: dict[str, _Any], session_context: dict[str, _Any]) -> dict[str, _Any]:
        captured["force_refresh"] = input_dict.get("force_refresh")
        return {
            "result": {
                "patient_id": input_dict["patient_id"],
                "medications_reviewed": 0,
                "flag_count": 0,
                "flags": [],
                "summary": "",
                "current_medications": [],
                "allergies": [],
                "interactions": [],
                "generated_at": "2026-05-02T00:00:00+00:00",
            },
            "citations": [],
            "metadata": {},
        }

    import main as main_module
    monkeypatch.setattr(main_module, "get_medication_safety", _fake_tool)

    client = TestClient(main_module.app)

    # Default (no query param) → force_refresh False.
    r1 = client.get("/medication/safety/pt-test")
    assert r1.status_code == 200
    assert captured["force_refresh"] is False

    # Explicit force_refresh=true → True threaded into the tool.
    r2 = client.get("/medication/safety/pt-test?force_refresh=true")
    assert r2.status_code == 200
    assert captured["force_refresh"] is True
