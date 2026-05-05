"""Tests for the census auto-discovery session-IDs fallback.

Covers two related defenses against the OpenEMR FHIR
``Patient?_count=200&_sort=_id`` -> 500 outage:

  (a) The bulk Patient query no longer includes ``_sort=_id`` (server-side
      sort triggers a SearchFieldOrder type bug in OpenEMR).
  (b) When ``get_census_summary`` is invoked with empty patient_ids
      (auto-discovery mode) and ``build_census`` raises, the tool falls
      back to ``session_context["patient_ids"]`` rather than failing.
  (c) When auto-discovery fails and there is nothing to fall back to,
      the original failure still propagates (so the dispatcher can map
      it to ``fhir_unavailable``).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytestmark = pytest.mark.hard_failure


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.new_event_loop().run_until_complete(coro)


# ── (a) bulk Patient query no longer includes _sort=_id ──────────────────────


def test_get_all_patient_ids_does_not_send_sort_id() -> None:
    from auth.fhir_client import FHIRClient

    client = FHIRClient.__new__(FHIRClient)
    captured: dict[str, Any] = {}

    async def fake_search(resource: str, params: dict[str, str]) -> dict[str, Any]:
        captured.setdefault("calls", []).append((resource, dict(params)))
        return {"entry": [{"resource": {"id": "fhir-1"}}]}

    client.search = fake_search  # type: ignore[method-assign]

    ids = _run(client.get_all_patient_ids(count=200))
    assert ids == ["fhir-1"]

    patient_calls = [c for c in captured["calls"] if c[0] == "Patient"]
    assert patient_calls, "expected at least one Patient search"
    for _, params in patient_calls:
        assert "_sort" not in params, (
            f"Patient bulk query must not include _sort (got {params}); "
            "OpenEMR's SearchFieldOrder constructor 500s on it."
        )
        assert params.get("_count") == "200"


def test_get_all_patient_ids_provider_path_drops_sort_id() -> None:
    from auth.fhir_client import FHIRClient

    client = FHIRClient.__new__(FHIRClient)
    captured: dict[str, Any] = {}

    async def fake_search(resource: str, params: dict[str, str]) -> dict[str, Any]:
        captured.setdefault("calls", []).append((resource, dict(params)))
        # Force the participant search to find a patient so we exercise
        # the Encounter branch only.
        if resource == "Encounter":
            return {
                "entry": [
                    {"resource": {"subject": {"reference": "Patient/abc"}}},
                ]
            }
        return {"entry": []}

    client.search = fake_search  # type: ignore[method-assign]

    ids = _run(client.get_all_patient_ids(count=50, provider_id="prov-1"))
    assert ids == ["abc"]

    encounter_calls = [c for c in captured["calls"] if c[0] == "Encounter"]
    assert encounter_calls, "expected an Encounter search"
    for _, params in encounter_calls:
        assert "_sort" not in params, (
            f"Encounter participant query must not include _sort (got {params})"
        )


# ── (b) and (c) get_census_summary fallback behaviour ────────────────────────


@dataclass
class _FakeEntry:
    patient_id: str
    name: str = "Test Patient"
    triage_level: int = 3
    explanation: str = "ok"

    def to_dict(self) -> dict[str, Any]:  # mimic CensusEntry shape used downstream
        return {
            "patient_id": self.patient_id,
            "name": self.name,
            "triage_level": self.triage_level,
            "explanation": self.explanation,
        }


def test_get_census_summary_falls_back_to_session_patient_ids() -> None:
    from agent import tools as tools_module
    from agent.metrics import agent_prewarm_runs_total

    fallback_ids = ["pt-001", "pt-002"]
    bulk_failure = RuntimeError(
        "Server error '500 Internal Server Error' for url "
        "'http://openemr/apis/default/fhir/Patient?_count=200'"
    )

    fake_entries = [_FakeEntry(pid) for pid in fallback_ids]

    class _FakeCensusResult:
        verified = fake_entries
        dropped_ids: list[str] = []
        generated_at = "2026-05-04T00:00:00+00:00"

    call_log: list[list[str]] = []

    async def build_census_side_effect(pids: list[str], **kwargs: Any) -> Any:
        call_log.append(list(pids))
        if not pids:
            raise bulk_failure
        return _FakeCensusResult()

    async def explain_passthrough(entries: list[Any], **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "patient_id": e.patient_id,
                "name": e.name,
                "triage_level": e.triage_level,
                "explanation": e.explanation,
            }
            for e in entries
        ]

    async def fake_get_bundle(_pid: str) -> dict[str, Any]:
        return {"resources": {}}

    def fake_verify(entry: dict[str, Any], _bundle: dict[str, Any]) -> dict[str, Any]:
        return entry

    before = agent_prewarm_runs_total.labels(
        outcome="census_auto_discovery_failed_fallback"
    )._value.get()  # type: ignore[attr-defined]

    with patch.object(tools_module, "build_census", side_effect=build_census_side_effect), \
         patch.object(tools_module, "explain_census", side_effect=explain_passthrough), \
         patch.object(tools_module, "verify_triage_entry", side_effect=fake_verify), \
         patch.object(tools_module.fhir_client, "get_bundle_for_patient", side_effect=fake_get_bundle), \
         patch.object(tools_module, "_schedule_census_briefing_warm", lambda **_: None), \
         patch.object(tools_module, "_get_cached_bundle", AsyncMock(return_value={"resources": {}})):
        result = _run(
            tools_module.get_census_summary(
                {"provider_id": "prov-1", "patient_ids": []},
                {"patient_ids": fallback_ids, "request_id": "req-xyz"},
            )
        )

    after = agent_prewarm_runs_total.labels(
        outcome="census_auto_discovery_failed_fallback"
    )._value.get()  # type: ignore[attr-defined]

    # build_census should have been called twice: once with [] (which
    # raised), once with the session fallback ids.
    assert len(call_log) == 2
    assert call_log[0] == []
    assert sorted(call_log[1]) == sorted(fallback_ids)

    assert result["result"]["total"] == len(fallback_ids)
    returned_ids = sorted(e["patient_id"] for e in result["result"]["census"])
    assert returned_ids == sorted(fallback_ids)

    assert after - before == 1, (
        "fallback path must increment "
        "agent_prewarm_runs_total{outcome='census_auto_discovery_failed_fallback'}"
    )


def test_get_census_summary_propagates_when_no_session_fallback() -> None:
    """If both input and session patient_ids are empty, the original
    failure must surface so the dispatcher can map it to fhir_unavailable.
    """
    from agent import tools as tools_module

    bulk_failure = RuntimeError("FHIR 500 on Patient bulk query")

    async def build_census_side_effect(pids: list[str], **kwargs: Any) -> Any:
        raise bulk_failure

    with patch.object(tools_module, "build_census", side_effect=build_census_side_effect):
        with pytest.raises(RuntimeError, match="FHIR 500"):
            _run(
                tools_module.get_census_summary(
                    {"provider_id": "prov-1", "patient_ids": []},
                    {"patient_ids": [], "request_id": "req-empty"},
                )
            )
