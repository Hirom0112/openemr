"""Prometheus instruments owned by the HL7 v2 parser (Phase 9 Slice 9.4).

Why these live here and not in ``agent/metrics.py``:

The importlinter contract ``parsers-hl7-isolated`` forbids
``parsers.hl7 -> agent``. Centralising every metric in ``agent/metrics``
would invert that boundary. Instead, the parser owns its own
instruments; ``agent/metrics`` carries a marker comment pointing
operators here so a single ``grep agent_hl7_parse`` still surfaces the
catalog entry.

The names + labels match the §5.5 metric table in ``ARCHITECTURE.md``
(updated by Slice 9.10) and the dispatcher's structured log events
(``hl7_parse_completed`` / ``hl7_parse_failed``) per the CLAUDE.md
"Observability — verifiable latency claims" checklist (one metric +
one log per instrumented call site).
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

agent_hl7_parse_total = Counter(
    "agent_hl7_parse_total",
    "HL7 v2 parser invocations by message_type and outcome",
    ["message_type", "outcome"],
)

agent_hl7_parse_duration_seconds = Histogram(
    "agent_hl7_parse_duration_seconds",
    "HL7 v2 parser wall-clock seconds, labelled by message_type",
    ["message_type"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
