# Co-Pilot Test Suite

This suite contains 1,250 collected tests across 111 test files in
`agent-api/tests/`, plus a parsers sub-suite under `parsers/`. Coverage
spans both Week 1 (dispatcher / tools / briefing / triage / handoff)
and Week 2 (document ingestion / multi-agent graph / RAG / eval gate)
work, plus shared infrastructure.

The split below is by *functional area*, not by directory — every file
listed here lives directly in `agent-api/tests/`. No tests have been
moved as part of this annotation pass.

## Week 1 tests — dispatcher, tools, sessions, W1 eval gate

Dispatcher and tool-use loop:

- `test_agent_query_graph.py` — `/agent/query` end-to-end behavior
- `test_agent_routing.py` — tool routing decisions
- `test_dispatcher_anthropic_request_shape.py`
- `test_dispatcher_error_class.py`
- `test_dispatcher_fast_path.py`
- `test_dispatcher_history_persistence.py`
- `test_dispatcher_pid_resolution.py`
- `test_dispatcher_response_metadata.py`
- `test_dispatcher_structured_skip.py`
- `test_dispatcher_verification.py`
- `test_scope_check.py` — pre-tool-call patient-scope guard
- `test_tool_schemas.py` — tool schema correctness
- `test_verification.py` — source attribution + domain constraints
- `test_checkpointer.py` — session persistence

Pre-encounter briefing (UC-2):

- `test_briefing_generator.py`
- `test_briefing_cache.py`
- `test_briefing_force_refresh.py`
- `test_briefing_attribution_fixes.py`
- `test_demographics.py`
- `test_demographics_resolver.py`

Morning triage / census (UC-1):

- `test_triage_rules.py` — deterministic ranking rules
- `test_synthetic_patients.py` — classifications for 18 synthetic patients
- `test_census_briefing_warm.py`
- `test_census_cache_key.py`
- `test_census_freshness.py`
- `test_census_resilience.py`
- `test_census_session_fallback.py`

Handoff (UC-5):

- `test_handoff_bundle_cache.py`
- `test_handoff_streaming.py`

Medication safety (UC-4):

- `test_medication_safety.py`
- `test_medication_safety_cache.py`
- `test_medication_safety_endpoint_shape.py`
- `test_medication_safety_unification.py`

Targeted record query (UC-3):

- `test_query_conversation.py`
- `test_query_router.py`

Pre-fetch + cache cascade:

- `test_cache_cascade.py`
- `test_prefetch_bulk_query_fallback.py`
- `test_prefetch_cost_guardrail.py`
- `test_prefetch_warm_ordering.py`

W1 eval gate + golden-set:

- `test_diff_baseline.py` — eval gate diff logic
- `test_eval_log_capture_isolation.py`
- `test_eval_parallelization.py`
- `test_eval_provenance_rubric.py`
- `test_eval_response_cache.py`
- `test_eval_rubrics_llm.py`
- `test_eval_rubrics_mechanical.py`
- `test_eval_run_full_suite.py`
- `test_eval_runner.py`
- `test_eval_scoring.py`
- `test_eval_smoke_subset.py`
- `test_evals_no_phi_real_bug.py`
- `test_prompt_eval.py` — dispatcher / system prompt golden set
- `test_run_matrix.py` — eval matrix runner

## Week 2 tests — document ingestion, graph, RAG, W2 eval gate

Document ingestion + classification:

- `test_document_ingest.py`
- `test_document_ingest_dispatcher.py`
- `test_document_chat.py`
- `test_doc_classifier_dispatch.py`
- `test_documents_store.py`
- `test_post_ingest_context.py`
- `test_post_ingest_e2e_chain.py`

Loaders / OCR / preprocessing:

- `test_docx_loader.py`
- `test_tiff_loader.py`
- `test_ocr.py`
- `test_ocr_engine.py`
- `test_ocr_psm_dpi.py`
- `test_photo_preprocess.py`
- `test_layout_block_polygon.py`

Extractors + schemas:

- `test_extractor_intake.py`
- `test_extractor_intake_prose.py`
- `test_extractor_lab.py`
- `test_criteria_extractor.py`
- `test_intake_nearest_label.py`
- `test_nearest_label_grounded_wiring.py`
- `test_schemas.py` — `extractors/schemas.py` strict-mode validation

Multi-agent graph (LangGraph supervisor + workers + critic):

- `test_graph_skeleton.py`
- `test_graph_nodes.py`
- `test_graph_e2e.py`
- `test_critic.py`
- `test_cross_source_conflict_node.py`
- `test_conflict_detector.py`
- `test_retriever_node_real.py`

Hybrid RAG (sparse + dense + Cohere rerank):

- `test_rag_chunker.py`
- `test_rag_chunker_json.py`
- `test_rag_embed.py`
- `test_rag_rerank.py`
- `test_rag_retrieve.py`
- `test_evidence_search_route.py`

Citations + bbox provenance:

- `test_citations.py`
- `test_citation_verifier.py`
- `test_citation_iou_polygon.py`

Staging + quarantine + labeling:

- `test_staging_endpoints.py`
- `test_staging_store.py`
- `test_staging_watchdog.py`
- `test_quarantine_endpoints.py`
- `test_labeling_queue.py`

FHIR write-back (Observations from documents):

- `test_fhir_writer.py`
- `test_observation_writer.py`

W2 eval gate + corpus + dispatch:

- `test_w2_dispatch.py`
- `test_w2_audit_events.py`
- `test_w2_metrics.py`
- `test_w2_eval.py`
- `test_w2_eval_bucket_counts.py`
- `test_w2_eval_no_real_phi.py`
- `test_w2_eval_smoke.py`
- `test_synthetic_corpus.py` — Wave 2C synthetic eval corpus + bbox-GT rubrics

Multimodal parsers (Phase 9.9):

- `parsers/hl7/` — HL7 v2 parser
- `parsers/xlsx/` — XLSX parser

## Shared / infrastructure

- `test_audit_emit.py`
- `test_audit_middleware.py`
- `test_audit_writer.py`
- `test_fhir_auth.py` — FHIR OAuth token client
- `test_observability.py` — log + metric helpers
- `test_prompt_registry.py` — prompt registry contract

## Markers

Currently enforced via `tests/conftest.py` (`REQUIRED_MARKERS`):

- `@pytest.mark.hard_failure` — 100% gate (infrastructure / contract)
- `@pytest.mark.clinical_accuracy` — 95% gate (clinical correctness)

Additional markers in use:

- `@pytest.mark.smoke` — pre-push hook subset
- `@pytest.mark.live_api` — skipped without live credentials

The dispatcher's W1/W2 split is functional, not enforced via marker.
The test files themselves are not relocated; this README is the
reading guide.
