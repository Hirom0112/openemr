"""LLM response-format schemas for tool_use structured output.

These schemas are passed as the ``tools`` argument to ``messages.create``
so the model is forced to return structured data via ``tool_use`` rather
than free-text JSON.  They define what the LLM *returns*, not what the
dispatcher sends *to* a tool (those input schemas are in agent/schemas.py).

Every LLM generation call in this project must use one of these schemas.
Free-text JSON parsing (model_validate_json, json.loads on raw text) is
prohibited by W1_ARCHITECTURE.md §4.4.
"""

from typing import Any

PRODUCE_BRIEFING: dict[str, Any] = {
    "name": "produce_briefing",
    "description": "Produce a structured pre-encounter briefing response with source-attributed clinical claims.",
    "input_schema": {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
            "name": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "One of: diagnosis, medications, vitals, labs, allergies, alerts",
                        },
                        "summary": {"type": "string", "description": "1-2 sentence plain-English summary."},
                        "claims": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "source_resource": {"type": "string"},
                                    "source_code": {"type": "string"},
                                    "source_value": {"type": "string"},
                                    "source_dt": {"type": "string"},
                                },
                                "required": ["text", "source_resource", "source_code", "source_value", "source_dt"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["section", "summary", "claims"],
                    "additionalProperties": False,
                },
            },
            "alerts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hard alerts: critical labs, stale values. Do NOT include code-status alerts.",
            },
            "summary": {
                "type": "string",
                "description": "2-3 sentence executive synthesis of the patient's most important issues. Plain English. Not source-attributed.",
            },
            "generated_at": {"type": "string", "description": "ISO-8601 UTC timestamp."},
        },
        "required": ["patient_id", "name", "sections", "alerts", "summary", "generated_at"],
        "additionalProperties": False,
    },
}

PRODUCE_TRIAGE_EXPLANATION: dict[str, Any] = {
    "name": "produce_triage_explanation",
    "description": "Produce a single-sentence triage explanation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "explanation": {
                "type": "string",
                "description": (
                    "One sentence (≤20 words) explaining the most urgent clinical finding. "
                    "State a specific value (e.g. K+ 6.4, RR 26) and its source. "
                    "No clinical recommendations."
                ),
            },
        },
        "required": ["explanation"],
        "additionalProperties": False,
    },
}

PRODUCE_QUERY_ANSWER: dict[str, Any] = {
    "name": "produce_query_answer",
    "description": "Produce a cited answer to a physician's clinical question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": (
                    "Answer to the physician's question in under 100 words. "
                    "Cite the source of every clinical value: state the value, "
                    "the record type, and the date/time. "
                    "If data is absent, state what was searched and the search window used."
                ),
            },
        },
        "required": ["answer"],
        "additionalProperties": False,
    },
}

PRODUCE_HANDOFF: dict[str, Any] = {
    "name": "produce_handoff",
    "description": "Produce an I-PASS handoff summary for one patient.",
    "input_schema": {
        "type": "object",
        "properties": {
            "illness_severity": {
                "type": "string",
                "enum": ["Stable", "Watcher", "Sick"],
                "description": "Overall illness trajectory.",
            },
            "patient_summary": {
                "type": "string",
                "description": "2-3 sentences: diagnosis, hospital course, key findings.",
            },
            "action_list": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pending tasks, ordered tests, follow-ups.",
            },
            "situation_awareness": {
                "type": "string",
                "description": "What could go wrong in the next 12 hours.",
            },
            "contingency_plan": {
                "type": "string",
                "description": "If X happens, do Y.",
            },
        },
        "required": [
            "illness_severity",
            "patient_summary",
            "action_list",
            "situation_awareness",
            "contingency_plan",
        ],
        "additionalProperties": False,
    },
}

PRODUCE_SAFETY_SUMMARY: dict[str, Any] = {
    "name": "produce_safety_summary",
    "description": "Produce a medication safety summary paragraph.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 sentence summary of medication safety flags. "
                    "State each flag severity and drug name. "
                    "Do not make treatment recommendations. "
                    "Do not assert a drug is safe."
                ),
            },
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
}
