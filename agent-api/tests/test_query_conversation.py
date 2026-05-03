"""Tests for the UC-3 query conversation handler.

Specifically covers the "Yvonne / medical problems" regression: the query
tool was returning records_fetched=0 because OpenEMR's FHIR search drops
``clinical-status=active`` filters and the live-search path did not resolve
numeric PIDs to FHIR UUIDs. The fix routes through the cached patient
bundle (which already handles both issues on the briefing path) via the
``records_override`` parameter.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from query.conversation import ConversationHandler, _empty_records_narrative


def _make_handler() -> ConversationHandler:
    redis_saver = MagicMock()
    redis_saver.load = AsyncMock(return_value=[])
    redis_saver.append = AsyncMock(return_value=None)
    redis_saver.refresh_ttl = AsyncMock(return_value=None)
    sqlite_saver = MagicMock()
    sqlite_saver.load = AsyncMock(return_value=[])
    sqlite_saver.append = AsyncMock(return_value=None)
    return ConversationHandler(
        redis_saver=redis_saver,
        sqlite_saver=sqlite_saver,
        redis_client=None,
        langfuse=None,
    )


@pytest.mark.clinical_accuracy
class TestEmptyRecordsNarrative:
    def test_condition_message_is_clinical_not_failure(self):
        msg = _empty_records_narrative("Condition")
        assert "no" in msg.lower() and "condition" in msg.lower()
        assert "unable" not in msg.lower()

    def test_medication_message_mentions_orders(self):
        msg = _empty_records_narrative("MedicationRequest")
        assert "medication" in msg.lower()
        assert "unable" not in msg.lower()

    def test_unknown_resource_falls_back_clearly(self):
        msg = _empty_records_narrative("CarePlan")
        assert "CarePlan" in msg
        assert "unable" not in msg.lower()

    def test_encounter_message_is_clinical_not_failure(self):
        msg = _empty_records_narrative("Encounter")
        assert "encounter" in msg.lower()
        assert "unable" not in msg.lower()


@pytest.mark.clinical_accuracy
@pytest.mark.hard_failure
class TestConversationHandlerEmptyShortCircuit:
    """When records_override is empty, the handler must skip the LLM and
    return the honest "no records on file" message — not the generic
    "I was unable to retrieve" string that reads as a tool failure.
    """

    @pytest.mark.asyncio
    async def test_empty_condition_records_returns_no_problems_message(self):
        handler = _make_handler()
        result = await handler.answer(
            session_id="t1",
            patient_id="8",
            query="what medical problems does she have",
            records_override=[],
        )
        assert "unable" not in result["answer"].lower()
        assert "condition" in result["answer"].lower() or "problem" in result["answer"].lower()
        assert result["records_fetched"] == 0
        assert result["route"]["resource"] == "Condition"

    @pytest.mark.asyncio
    async def test_empty_medication_records_returns_clear_message(self):
        handler = _make_handler()
        result = await handler.answer(
            session_id="t2",
            patient_id="8",
            query="what medications is she on",
            records_override=[],
        )
        assert "unable" not in result["answer"].lower()
        assert "medication" in result["answer"].lower()
        assert result["records_fetched"] == 0

    @pytest.mark.asyncio
    async def test_empty_encounter_records_returns_clear_message(self):
        # Regression: "when was her last appointment" used to route to
        # Observation (LLM fallback), slice an empty list out of the bundle,
        # and return the generic "unable to retrieve" message. Now it routes
        # to Encounter and surfaces the honest no-records narrative.
        handler = _make_handler()
        result = await handler.answer(
            session_id="t3-enc",
            patient_id="8",
            query="when was her last appointment",
            records_override=[],
        )
        assert "unable" not in result["answer"].lower()
        assert "encounter" in result["answer"].lower()
        assert result["records_fetched"] == 0
        assert result["route"]["resource"] == "Encounter"


@pytest.mark.clinical_accuracy
@pytest.mark.hard_failure
class TestConversationHandlerRecordsOverride:
    """When records_override is supplied (non-empty), the handler must use
    those records instead of calling the live FHIR search. We don't run the
    LLM in unit tests, but we can confirm the records_fetched count reflects
    the override and the route was still computed.
    """

    @pytest.mark.asyncio
    async def test_records_override_skips_live_search(self, monkeypatch):
        # Sentinel that would explode if the live search path were taken.
        async def _boom(*_a, **_kw):
            raise AssertionError("live FHIR search should not be called when records_override is set")

        monkeypatch.setattr("query.conversation.search_for_patient", _boom)

        # Stub the LLM call so we don't make a network request — we only need
        # to confirm the record list reached the handler.
        handler = _make_handler()
        captured: dict = {}

        class _StubMsg:
            content: list = []

        async def _fake_create(**kwargs):
            captured["messages"] = kwargs.get("messages", [])
            return _StubMsg()

        handler._client.messages.create = _fake_create  # type: ignore[attr-defined]

        records = [{"resourceType": "Condition", "id": "c1", "code": {"text": "Diabetic nephropathy"}}]
        result = await handler.answer(
            session_id="t3",
            patient_id="8",
            query="what medical problems does she have",
            records_override=records,
        )
        assert result["records_fetched"] == 1
        # The supplied record made it into the prompt verbatim.
        last_user = captured["messages"][-1]["content"]
        assert "Diabetic nephropathy" in last_user
