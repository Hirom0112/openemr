"""Prometheus metrics for the Clinical Co-Pilot dispatcher.

Centralised here to avoid circular imports between main.py and dispatcher.py.
Both modules import from this file; neither defines metrics directly.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

agent_tool_calls_total = Counter(
    "agent_tool_calls_total",
    "Total tool calls by tool name",
    ["tool"],
)

agent_dispatch_latency_seconds = Histogram(
    "agent_dispatch_latency_seconds",
    "Dispatcher end-to-end latency in seconds",
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0],
)

agent_tool_misroute_total = Counter(
    "agent_tool_misroute_total",
    "Total detected tool misroutes",
)

# ── Deterministic fast-path counter ──────────────────────────────────────────
# Increments each time the dispatcher short-circuits the LLM planner loop and
# calls a tool directly based on a deterministic free-text pattern match
# (e.g. "brief Marcus Webb").  Labelled by which tool was invoked so we can
# compare fast-path vs LLM-path traffic per tool.
agent_fast_path_hits_total = Counter(
    "agent_fast_path_hits_total",
    "Dispatcher deterministic fast-path hits, labelled by tool invoked",
    ["tool"],
)

# ── Anthropic prompt-cache token accounting ──────────────────────────────────
# These count *input tokens* served from / created in the Anthropic prompt
# cache, not Redis hit/miss counts. Renamed in 2026-05 from
# ``agent_cache_{hits,misses}_total`` for clarity.
agent_prompt_cache_hits_total = Counter(
    "agent_prompt_cache_hits_total",
    "Anthropic prompt cache hits measured in input tokens served from cache",
)

agent_prompt_cache_misses_total = Counter(
    "agent_prompt_cache_misses_total",
    "Anthropic prompt cache misses measured in cache-creation input tokens",
)

# ── Redis data-cache hit/miss counters ───────────────────────────────────────
# Labelled by which data cache was read. Values for ``cache``:
# bundle | briefing | census | explanation.
agent_data_cache_hits_total = Counter(
    "agent_data_cache_hits_total",
    "Redis data-cache hits, labelled by cache name",
    ["cache"],
)

agent_data_cache_misses_total = Counter(
    "agent_data_cache_misses_total",
    "Redis data-cache misses (including read errors), labelled by cache name",
    ["cache"],
)

# ── Prefetch / pre-warm telemetry ────────────────────────────────────────────
agent_prewarm_duration_seconds = Histogram(
    "agent_prewarm_duration_seconds",
    "Wall-clock seconds spent in the /agent/prefetch background warmer",
    ["outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
)

agent_prewarm_runs_total = Counter(
    "agent_prewarm_runs_total",
    "Total /agent/prefetch background warmer runs by outcome",
    ["outcome"],
)

# ── Client-side (browser) action timing ──────────────────────────────────────
agent_client_timing_seconds = Histogram(
    "agent_client_timing_seconds",
    "Browser-reported action duration (POST /agent/client-timing), by action",
    ["action"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
)

# FHIR token cache hit/miss counters live in auth/fhir_client.py since auth is
# a leaf (cannot import from agent). They are still scraped at /metrics.

# ── Checkpointer (Redis / SQLite) op metrics ─────────────────────────────────
agent_checkpointer_ops_total = Counter(
    "agent_checkpointer_ops_total",
    "Checkpointer load/save operations by op, backend, and outcome",
    ["op", "backend", "outcome"],
)

agent_checkpointer_op_duration_seconds = Histogram(
    "agent_checkpointer_op_duration_seconds",
    "Checkpointer load/save wall-clock seconds by op and backend",
    ["op", "backend"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# ── Patient-id normalization counter ─────────────────────────────────────────
# Increments at the entry point of every single-patient tool. The ``form``
# label tracks which input form the dispatcher passed in:
#   synthetic_to_pid → ``pt-008`` → ``8``  (most common misroute source)
#   already_pid      → ``8``                (canonical, no work needed)
#   uuid_to_pid      → FHIR UUID            (passed through to fhir_client)
#   unknown          → empty / unrecognised input
agent_pid_normalization_total = Counter(
    "agent_pid_normalization_total",
    "Patient-id forms arriving at single-patient tools",
    ["form"],
)

# ── Census fan-out drop counter ──────────────────────────────────────────────
# Increments every time _build_entry returns None for a patient (transient
# FHIR failure). Surfaces silent census shrinkage that previously caused
# Sara Chen's panel count to oscillate between reloads.
agent_census_dropped_patients_total = Counter(
    "agent_census_dropped_patients_total",
    "Number of patients silently dropped from a census because _build_entry failed",
)
