"""Tool JSON Schema definitions for the Clinical Co-Pilot dispatcher.

Each entry in TOOL_SCHEMAS is a dict with keys ``name``, ``description``, and
``input_schema`` (JSON Schema draft-07) suitable for passing directly to
``anthropic.messages.create(tools=[...])``.

Dispatcher registration
-----------------------
DISPATCHER_TOOLS  — five conversational tools registered with the dispatcher
                    (POST /agent/query).
DIRECT_TOOLS      — one tool called directly by the React panel without going
                    through the dispatcher (POST /agent/triage_rationale).

Disambiguation notes
--------------------
"Lasix plan" queries:
  - interactions / safety concern   → get_medication_safety
  - current dose / history          → query_patient_records
  - full patient context            → get_patient_briefing

"Why is [patient] first?" typed into chat:
  → query_patient_records  (conversational form, through dispatcher)
  NOT get_triage_rationale (that is direct-call only, click-to-expand)

"What are the vitals?"         → query_patient_records
"Give me a briefing on bed 7"  → get_patient_briefing
"Start handoff"                → generate_handoff
"Any allergies?"               → get_medication_safety

Phase 2 migration plan
----------------------
The following files currently use Pydantic ``model_validate_json`` to parse
free-text JSON from LLM responses.  They must be migrated to
``messages.create(tools=[...])`` using the schemas defined here.  The
Pydantic-JSON path must NOT remain as a live fallback alongside tool_use
(ARCHITECTURE.md §4.4 explicitly prohibits it).

  briefing/generator.py   — BriefingResponse via model_validate_json
  triage/explainer.py     — one-liner explanation via direct prompt
  query/conversation.py   — QueryAnswer via model_validate_json
  handoff/generator.py    — HandoffSummary via model_validate_json

Phase 2 is the migration; Phase 1 (this file) defines the shapes only.
"""

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_census_summary",
        "description": (
            "Generate the morning triage census ranked P1–P10. "
            "Calls the deterministic rules engine; LLM produces one-line explanations only "
            "— it does not determine rank order. "
            "Use at session open when Dr. Chen presses 'Go'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "provider_id": {
                    "type": "string",
                    "description": "Authenticated provider identifier from the OpenEMR session.",
                },
                "patient_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "List of patient IDs on the active census.",
                },
            },
            "required": ["provider_id", "patient_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_patient_briefing",
        "description": (
            "Generate a pre-encounter briefing for a single patient. "
            "Returns active problems, recent vitals, current medications, "
            "pending labs/results, and a one-paragraph clinical summary. "
            "Use before entering the patient's room. "
            "Do NOT use for a single targeted question — use query_patient_records instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "FHIR patient resource ID.",
                },
                "provider_id": {
                    "type": "string",
                    "description": "Authenticated provider identifier from the OpenEMR session.",
                },
            },
            "required": ["patient_id", "provider_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_patient_records",
        "description": (
            "Answer a specific clinical question about a patient's records. "
            "Searches encounters (default 24 months), labs (default 12 months), "
            "vitals, medications, and conditions. "
            "Use for targeted questions like 'what did the last echo show?' "
            "or 'what is the current potassium?' "
            "Also use for conversational triage rationale questions such as "
            "'why is bed 7 first?' — do NOT route those to get_triage_rationale. "
            "Do NOT use for full briefings — use get_patient_briefing instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "FHIR patient resource ID.",
                },
                "query": {
                    "type": "string",
                    "description": "The physician's clinical question in natural language.",
                },
                "provider_id": {
                    "type": "string",
                    "description": "Authenticated provider identifier from the OpenEMR session.",
                },
                "encounter_window_months": {
                    "type": "integer",
                    "default": 24,
                    "minimum": 1,
                    "maximum": 60,
                    "description": "How many months back to search encounters. Default 24; max 60.",
                },
                "lab_window_months": {
                    "type": "integer",
                    "default": 12,
                    "minimum": 1,
                    "maximum": 36,
                    "description": "How many months back to search lab results. Default 12; max 36.",
                },
            },
            "required": ["patient_id", "query", "provider_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_medication_safety",
        "description": (
            "Surface medication safety information for a patient: "
            "current medications, known allergies, potential interactions of concern. "
            "If medication_name is provided, focus on that drug's interactions and "
            "contraindications given the patient's profile. "
            "Never asserts a drug is 'safe' — surfaces known concerns only. "
            "Use for 'any allergies?', 'is Lasix safe here?', 'what are the interactions?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "FHIR patient resource ID.",
                },
                "provider_id": {
                    "type": "string",
                    "description": "Authenticated provider identifier from the OpenEMR session.",
                },
                "medication_name": {
                    "type": "string",
                    "description": (
                        "Optional. If provided, focus safety analysis on this specific drug "
                        "and its interactions with the patient's current medication list."
                    ),
                },
            },
            "required": ["patient_id", "provider_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_handoff",
        "description": (
            "Generate a structured shift handoff for all patients on census. "
            "Runs parallel briefing generation internally. "
            "Output includes: one-paragraph clinical status per patient, "
            "active issues, pending items, and escalation triggers. "
            "Takes up to 20 seconds. Use when Dr. Chen says 'start handoff' "
            "or 'generate handoff notes'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "provider_id": {
                    "type": "string",
                    "description": "Authenticated provider identifier from the OpenEMR session.",
                },
                "patient_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "List of patient IDs to include in the handoff.",
                },
                "shift_end_time": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional ISO 8601 datetime of shift end (e.g. '2026-04-29T14:00:00Z').",
                },
            },
            "required": ["provider_id", "patient_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_triage_rationale",
        "description": (
            "Return the full rationale for a patient's triage priority level: "
            "which rules fired, which thresholds were crossed, and the one-line clinical explanation. "
            "This tool is called directly by the React panel click-to-expand affordance "
            "via POST /agent/triage_rationale — it is NOT dispatched through the "
            "conversational loop. "
            "The conversational form ('why is bed 7 first?') routes through the dispatcher "
            "as a query_patient_records call — NOT through this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "string",
                    "description": "FHIR patient resource ID.",
                },
                "provider_id": {
                    "type": "string",
                    "description": "Authenticated provider identifier from the OpenEMR session.",
                },
            },
            "required": ["patient_id", "provider_id"],
            "additionalProperties": False,
        },
        "dispatch_mode": "direct",
    },
]

TOOL_SCHEMA_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOL_SCHEMAS}

DISPATCHER_TOOLS: list[dict[str, Any]] = [
    t for t in TOOL_SCHEMAS if t["name"] != "get_triage_rationale"
]

DIRECT_TOOLS: list[dict[str, Any]] = [
    t for t in TOOL_SCHEMAS if t["name"] == "get_triage_rationale"
]
