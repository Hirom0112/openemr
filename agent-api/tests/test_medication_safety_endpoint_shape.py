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
