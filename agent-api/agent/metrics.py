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

agent_cache_hits_total = Counter(
    "agent_cache_hits_total",
    "Anthropic prompt cache hits measured in input tokens served from cache",
)

agent_cache_misses_total = Counter(
    "agent_cache_misses_total",
    "Anthropic prompt cache misses measured in cache-creation input tokens",
)
