"""Prometheus metrics for the Clinical Co-Pilot dispatcher.

Centralised here to avoid circular imports between main.py and dispatcher.py.
Both modules import from this file; neither defines metrics directly.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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

# ── Patient-id resolution counter (dispatcher scope check) ───────────────────
# Increments each time the dispatcher's census-scope guard rewrites a
# tool_input.patient_id from a non-canonical form to an in-census pid before
# invoking the tool. Labels:
#   normalize   → ``pt-001`` → ``"1"`` via _normalize_patient_id
#   name_match  → ``"Marcus Webb"`` → ``"pt-001"`` via _resolve_patient_from_census
agent_pid_resolution_total = Counter(
    "agent_pid_resolution_total",
    "Dispatcher patient_id resolutions performed before census scope check",
    ["method"],
)

# ── Census fan-out drop counter ──────────────────────────────────────────────
# Increments every time _build_entry returns None for a patient (transient
# FHIR failure). Surfaces silent census shrinkage that previously caused
# Sara Chen's panel count to oscillate between reloads.
agent_census_dropped_patients_total = Counter(
    "agent_census_dropped_patients_total",
    "Number of patients silently dropped from a census because _build_entry failed",
)

# ── W2 metrics (W2_ARCHITECTURE §10.2) ───────────────────────────────────────
# These instrument the document-ingest path, the LangGraph hybrid-RAG
# retriever, the critic, and the demographic comparator. Each metric is paired
# with a structured log event at its call site (see CLAUDE.md "Observability —
# verifiable latency claims").

agent_w2_document_ingest_total = Counter(
    "agent_w2_document_ingest_total",
    "Document ingest count",
    ["path", "doc_type", "outcome"],
)
agent_w2_extraction_duration_seconds = Histogram(
    "agent_w2_extraction_duration_seconds",
    "End-to-end extraction duration (OCR + classifier + vision + validation)",
    ["doc_type", "classifier_confidence_bucket"],
)
agent_w2_retrieval_duration_seconds = Histogram(
    "agent_w2_retrieval_duration_seconds",
    "Retrieval stage duration",
    ["mode"],   # sparse | dense | rerank | merge
)
agent_w2_retrieval_hits_total = Counter(
    "agent_w2_retrieval_hits_total",
    "Retrieval hits",
    ["mode"],
)
agent_w2_critic_decisions_total = Counter(
    "agent_w2_critic_decisions_total",
    "Critic decisions emitted",
    ["decision", "reason"],   # pass|soft_warn|hard_block × reason category
)
agent_w2_demographic_checks_total = Counter(
    "agent_w2_demographic_checks_total",
    "Wrong-patient demographic check outcomes",
    ["outcome"],
)
agent_w2_classifier_confidence = Histogram(
    "agent_w2_classifier_confidence",
    "Classifier confidence per document",
    ["doc_type"],
    buckets=(0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0),
)
agent_w2_ocr_confidence = Histogram(
    "agent_w2_ocr_confidence",
    "Document-level OCR confidence",
    ["doc_type"],
    buckets=(0.3, 0.5, 0.6, 0.75, 0.9, 1.0),
)
agent_watchdog_last_run_timestamp_seconds = Gauge(
    "agent_watchdog_last_run_timestamp_seconds",
    "Wall-clock timestamp of the last APScheduler watchdog scan (Phase 8 will populate)",
)

# ── Citation repointer (Wave 2B) ─────────────────────────────────────────────
# Increments at every _repoint_citation decision, labelled by the field whose
# citation was being repointed and the outcome of the spatial selection. The
# ``outcome`` label values:
#   kept                                  → cited bbox already contains the
#                                            value text
#   repointed_with_anchor                 → multiple candidates resolved by
#                                            section-anchor spatial preference
#                                            (with or without field hint);
#                                            y-band distance was decisive
#   repointed_with_anchor_label_tiebreak  → multiple candidates tied on |Δy|
#                                            to the nearest anchor; the
#                                            LLM-supplied ``nearest_label``
#                                            broke the tie. y-band remains
#                                            primary — the label only ever
#                                            wins among equals.
#   repointed_no_anchor                   → repointed via the legacy
#                                            single-best-overlap path (no
#                                            structural anchors detected, or
#                                            only one candidate found)
#   no_match                              → no candidate cleared the overlap
#                                            floor; the original LLM citation
#                                            was preserved
agent_citation_repoint_total = Counter(
    "agent_citation_repoint_total",
    "Citation repointer decisions in the intake extractor",
    ["field", "outcome"],
)

# ── OCR engine dispatcher (Wave 2A) ──────────────────────────────────────────
# Recorded once per ``documents.ocr_engine.dispatch_extract_image`` call.
# ``engine`` ∈ {"tesseract", "paddleocr"}; ``outcome`` ∈ {"success", "error"}.
agent_ocr_engine_invocations_total = Counter(
    "agent_ocr_engine_invocations_total",
    "OCR engine invocations, labelled by engine and outcome",
    ["engine", "outcome"],
)

agent_ocr_extraction_duration_seconds = Histogram(
    "agent_ocr_extraction_duration_seconds",
    "OCR engine wall-clock seconds per call, labelled by engine",
    ["engine"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
)

# ── Eval suite parallelization (Wave 2C+) ────────────────────────────────────
# Emitted by ``evals/run_full_suite.py`` once per asyncio.gather batch and
# once per case completion. ``outcome`` ∈ {"success", "error"}.
agent_eval_batch_duration_seconds = Histogram(
    "agent_eval_batch_duration_seconds",
    "Wall-clock seconds spent running one eval-suite batch via asyncio.gather",
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0),
)

agent_eval_cases_completed_total = Counter(
    "agent_eval_cases_completed_total",
    "Eval-suite cases completed by outcome (success|error)",
    ["outcome"],
)

# ── Citation verifier (Wave 2C) ──────────────────────────────────────────────
# Emitted by ``agent.citation_verifier`` once per Claude verifier call.
# ``outcome`` ∈ {"yes", "no", "partial"}. The verifier is the optional
# second-pass node; when off, neither metric increments.
agent_citation_verifier_outcome_total = Counter(
    "agent_citation_verifier_outcome_total",
    "Citation verifier per-call outcomes (yes|no|partial)",
    ["outcome"],
)

# Increments when the per-request verifier-call cap (default 20) trips and a
# remaining citation is skipped. ``reason`` ∈ {"cap"} today; the label is
# kept so future skip reasons (e.g. "budget_exhausted") can be added without
# reshaping the metric.
agent_verifier_capped_total = Counter(
    "agent_verifier_capped_total",
    "Citation verifier skips driven by the per-request cap",
    ["reason"],
)
