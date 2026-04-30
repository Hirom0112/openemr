"""Tool schema validation tests — Phase 8 Phase 1.

All tests are hard_failure: schema correctness is infrastructure, not opinion.
"""

import pytest
import jsonschema

from agent.schemas import (
    DIRECT_TOOLS,
    DISPATCHER_TOOLS,
    TOOL_SCHEMA_BY_NAME,
    TOOL_SCHEMAS,
)

# ── Structure tests ────────────────────────────────────────────────────────────

@pytest.mark.hard_failure
def test_total_tool_count() -> None:
    assert len(TOOL_SCHEMAS) == 6


@pytest.mark.hard_failure
def test_dispatcher_tools_count() -> None:
    assert len(DISPATCHER_TOOLS) == 5


@pytest.mark.hard_failure
def test_direct_tools_count() -> None:
    assert len(DIRECT_TOOLS) == 1


@pytest.mark.hard_failure
def test_triage_rationale_not_in_dispatcher_tools() -> None:
    names = [t["name"] for t in DISPATCHER_TOOLS]
    assert "get_triage_rationale" not in names


@pytest.mark.hard_failure
def test_triage_rationale_in_direct_tools() -> None:
    names = [t["name"] for t in DIRECT_TOOLS]
    assert "get_triage_rationale" in names


@pytest.mark.hard_failure
@pytest.mark.parametrize("tool", TOOL_SCHEMAS)
def test_tool_has_required_keys(tool: dict) -> None:
    assert "name" in tool
    assert "description" in tool
    assert "input_schema" in tool


@pytest.mark.hard_failure
@pytest.mark.parametrize("tool", TOOL_SCHEMAS)
def test_input_schema_is_object_type(tool: dict) -> None:
    assert tool["input_schema"]["type"] == "object"


@pytest.mark.hard_failure
@pytest.mark.parametrize("tool", TOOL_SCHEMAS)
def test_input_schema_has_required_list(tool: dict) -> None:
    assert "required" in tool["input_schema"]
    assert isinstance(tool["input_schema"]["required"], list)
    assert len(tool["input_schema"]["required"]) >= 1


@pytest.mark.hard_failure
@pytest.mark.parametrize("tool", TOOL_SCHEMAS)
def test_input_schema_is_valid_json_schema(tool: dict) -> None:
    jsonschema.Draft7Validator.check_schema(tool["input_schema"])


# ── Per-tool field assertions ─────────────────────────────────────────────────

@pytest.mark.hard_failure
def test_census_summary_requires_provider_id_and_patient_ids() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_census_summary"]["input_schema"]
    assert "provider_id" in schema["required"]
    assert "patient_ids" in schema["required"]


@pytest.mark.hard_failure
def test_census_summary_patient_ids_is_array() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_census_summary"]["input_schema"]
    assert schema["properties"]["patient_ids"]["type"] == "array"


@pytest.mark.hard_failure
def test_query_records_has_encounter_window_with_maximum() -> None:
    schema = TOOL_SCHEMA_BY_NAME["query_patient_records"]["input_schema"]
    prop = schema["properties"]["encounter_window_months"]
    assert "maximum" in prop
    assert prop["maximum"] == 60


@pytest.mark.hard_failure
def test_query_records_has_lab_window_with_maximum() -> None:
    schema = TOOL_SCHEMA_BY_NAME["query_patient_records"]["input_schema"]
    prop = schema["properties"]["lab_window_months"]
    assert "maximum" in prop
    assert prop["maximum"] == 36


@pytest.mark.hard_failure
def test_medication_safety_medication_name_is_optional() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_medication_safety"]["input_schema"]
    assert "medication_name" not in schema["required"]
    assert "medication_name" in schema["properties"]


@pytest.mark.hard_failure
def test_generate_handoff_shift_end_time_has_date_time_format() -> None:
    schema = TOOL_SCHEMA_BY_NAME["generate_handoff"]["input_schema"]
    prop = schema["properties"]["shift_end_time"]
    assert prop.get("format") == "date-time"


@pytest.mark.hard_failure
def test_triage_rationale_dispatch_mode_is_direct() -> None:
    tool = TOOL_SCHEMA_BY_NAME["get_triage_rationale"]
    assert tool.get("dispatch_mode") == "direct"


# ── Sample valid input validation ─────────────────────────────────────────────

@pytest.mark.hard_failure
def test_census_summary_valid_input_validates() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_census_summary"]["input_schema"]
    jsonschema.validate(
        {"provider_id": "prov-1", "patient_ids": ["pt-001", "pt-002"]},
        schema,
    )


@pytest.mark.hard_failure
def test_patient_briefing_valid_input_validates() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_patient_briefing"]["input_schema"]
    jsonschema.validate({"patient_id": "pt-001", "provider_id": "prov-1"}, schema)


@pytest.mark.hard_failure
def test_query_records_valid_input_validates() -> None:
    schema = TOOL_SCHEMA_BY_NAME["query_patient_records"]["input_schema"]
    jsonschema.validate(
        {
            "patient_id": "pt-001",
            "query": "what is the current potassium?",
            "provider_id": "prov-1",
            "encounter_window_months": 24,
            "lab_window_months": 12,
        },
        schema,
    )


@pytest.mark.hard_failure
def test_query_records_defaults_are_optional_in_schema() -> None:
    schema = TOOL_SCHEMA_BY_NAME["query_patient_records"]["input_schema"]
    jsonschema.validate(
        {"patient_id": "pt-001", "query": "any recent echo?", "provider_id": "prov-1"},
        schema,
    )


@pytest.mark.hard_failure
def test_medication_safety_valid_input_without_medication_name() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_medication_safety"]["input_schema"]
    jsonschema.validate({"patient_id": "pt-001", "provider_id": "prov-1"}, schema)


@pytest.mark.hard_failure
def test_medication_safety_valid_input_with_medication_name() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_medication_safety"]["input_schema"]
    jsonschema.validate(
        {"patient_id": "pt-001", "provider_id": "prov-1", "medication_name": "Lasix"},
        schema,
    )


@pytest.mark.hard_failure
def test_generate_handoff_valid_input_validates() -> None:
    schema = TOOL_SCHEMA_BY_NAME["generate_handoff"]["input_schema"]
    jsonschema.validate(
        {"provider_id": "prov-1", "patient_ids": ["pt-001", "pt-002"]},
        schema,
    )


@pytest.mark.hard_failure
def test_generate_handoff_with_shift_end_time_validates() -> None:
    schema = TOOL_SCHEMA_BY_NAME["generate_handoff"]["input_schema"]
    jsonschema.validate(
        {
            "provider_id": "prov-1",
            "patient_ids": ["pt-001"],
            "shift_end_time": "2026-04-29T14:00:00Z",
        },
        schema,
    )


@pytest.mark.hard_failure
def test_triage_rationale_valid_input_validates() -> None:
    schema = TOOL_SCHEMA_BY_NAME["get_triage_rationale"]["input_schema"]
    jsonschema.validate({"patient_id": "pt-001", "provider_id": "prov-1"}, schema)


# ── TOOL_SCHEMA_BY_NAME lookup ────────────────────────────────────────────────

@pytest.mark.hard_failure
def test_schema_by_name_lookup() -> None:
    expected_names = {
        "get_census_summary",
        "get_patient_briefing",
        "query_patient_records",
        "get_medication_safety",
        "generate_handoff",
        "get_triage_rationale",
    }
    assert set(TOOL_SCHEMA_BY_NAME.keys()) == expected_names
