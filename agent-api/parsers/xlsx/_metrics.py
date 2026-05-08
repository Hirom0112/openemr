"""Prometheus instruments owned by the XLSX parser (Phase 9 Slice 9.5).

Why these live here and not in ``agent/metrics.py``: the importlinter
contract ``parsers-xlsx-isolated`` forbids ``parsers.xlsx -> agent``.
Centralising every metric in ``agent/metrics`` would invert that
boundary. Instead, the parser owns its own instruments;
``agent/metrics`` carries a marker comment pointing operators here so a
single ``grep agent_xlsx_parse`` still surfaces the catalog entry.

Names match the §5.5 metric table in ``W1_ARCHITECTURE.md`` (updated by
Slice 9.10) and the dispatcher's structured log events
(``xlsx_parse_completed`` / ``xlsx_parse_failed``) per the CLAUDE.md
"Observability — verifiable latency claims" checklist.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

agent_xlsx_parse_total = Counter(
    "agent_xlsx_parse_total",
    "XLSX parser invocations by outcome",
    ["outcome"],
)

agent_xlsx_parse_duration_seconds = Histogram(
    "agent_xlsx_parse_duration_seconds",
    "XLSX parser wall-clock seconds, labelled by outcome",
    ["outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0),
)

agent_xlsx_rows_extracted_total = Counter(
    "agent_xlsx_rows_extracted_total",
    "Total rows extracted from XLSX workbooks, labelled by sheet",
    ["sheet"],
)
