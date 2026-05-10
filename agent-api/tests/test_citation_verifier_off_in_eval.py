"""Phase 4.8 — eval-mode forces citation_verifier OFF.

Pre-fix: ``settings.verify_citations`` defaulted to ``"sample"`` for both
the production HTTP path AND the eval runner. The verifier mutates
``state["extraction"]`` in place when it runs (drops or repoints citations
on a "no" verdict), and even at temp=0 the underlying vision call has
inter-run server-side variance — driving ~30 case flips per full-suite run.

Post-fix: ``evals.runner.run_case`` and ``evals.run_full_suite.main``
both force ``settings.verify_citations = "off"`` (overridable via the
``EVAL_VERIFY_CITATIONS`` env var) so identical inputs produce identical
extractions across reruns. Production ``/document/ingest`` (which never
imports the eval modules) keeps the ``"sample"`` default.

This test asserts the override is applied and that under that override
the verifier is a structural no-op — citations passed in survive
untouched. The pre-fix code path (no override → "sample" mode → verifier
runs) would mutate the citation list when a "no" verdict came back.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _restore_verify_citations():
    """Restore verify_citations after each test."""
    from config import settings

    prior = settings.verify_citations
    yield
    settings.verify_citations = prior


async def test_run_case_sets_verify_citations_off_by_default(monkeypatch) -> None:
    """A bare ``run_case`` call (no env override) flips settings to 'off'.

    This is the regression: pre-Phase-4.8 the eval inherited the production
    "sample" default and the verifier mutated extraction during scoring.
    """
    from config import settings

    # Force a non-eval (production) value so we can see the runner override.
    settings.verify_citations = "sample"
    monkeypatch.delenv("EVAL_VERIFY_CITATIONS", raising=False)

    # We don't need to actually run the graph — exercise just the override
    # block by importing run_case and triggering its top-of-function setup
    # via a minimal duck-typed case object whose path returns early
    # (skipped reason). The override runs unconditionally before any branch.
    from evals.runner import run_case

    class _StubCase:
        case_id = "stub-verifier-off"
        bucket = "evidence_retrieval"  # routes to the env-check skip path
        evidence_query = "anything"
        fixture_key = ""
        chart_patient = {"id": "p1"}
        document_modality = None

    # Strip env vars so the evidence-retrieval branch returns "skipped".
    monkeypatch.delenv("AUDIT_DB_URL", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    outcome = await run_case(_StubCase(), fixtures_root=Path("/tmp"))
    # The override must have fired before the skip return.
    assert settings.verify_citations == "off", (
        f"run_case did not force verify_citations off (got {settings.verify_citations!r})"
    )
    # Sanity: we hit the skip path and didn't actually run a graph.
    assert outcome.skipped_reason and "AUDIT_DB_URL" in outcome.skipped_reason


async def test_eval_verify_citations_env_override_respected(monkeypatch) -> None:
    """``EVAL_VERIFY_CITATIONS=sample`` re-enables the verifier (audit knob)."""
    from config import settings

    settings.verify_citations = "off"
    monkeypatch.setenv("EVAL_VERIFY_CITATIONS", "sample")

    from evals.runner import run_case

    class _StubCase:
        case_id = "stub-verifier-on-via-env"
        bucket = "evidence_retrieval"
        evidence_query = "anything"
        fixture_key = ""
        chart_patient = {"id": "p1"}
        document_modality = None

    monkeypatch.delenv("AUDIT_DB_URL", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    await run_case(_StubCase(), fixtures_root=Path("/tmp"))
    assert settings.verify_citations == "sample", (
        f"env override ignored (got {settings.verify_citations!r})"
    )


async def test_verifier_node_with_off_mode_does_not_mutate_extraction() -> None:
    """The structural proof: with mode="off" the node returns {} and never
    touches state. A verifier_call stub records whether it was invoked; if
    invoked even once, the test fails (i.e. we'd be back in the variance
    regime).
    """
    from unittest.mock import patch

    from agent.citation_verifier import citation_verifier_node
    from extractors.schemas import VerificationResult

    extraction = {
        "kind": "lab_report",
        "values": [
            {
                "test_name": "Lactate",
                "value": "4.2",
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/abc",
                        "field_or_chunk_id": "p1-l001",
                        "quote_or_value": "4.2",
                        "bbox": (10.0, 10.0, 30.0, 12.0),
                    }
                ],
            }
        ],
    }
    layout = [
        {
            "bbox_id": "p1-l001",
            "page": 1,
            "bbox": [5.0, 8.0, 100.0, 16.0],
            "text": "Lactate 4.2 mmol/L",
            "ocr_confidence": 0.95,
            "granularity": "line",
        }
    ]
    state = {"extraction": extraction, "ocr_layout": layout}
    extraction_before = repr(extraction)

    invoked = False

    async def stub_verifier(value: str, crop: bytes) -> VerificationResult:
        nonlocal invoked
        invoked = True
        return VerificationResult(status="no", rationale="forced")

    async def page_provider(_cit: dict) -> bytes:
        return b"\x89PNG\r\n\x1a\n"  # not really used; verifier returns first

    with patch("agent.citation_verifier.settings") as cfg:
        cfg.verify_citations = "off"
        cfg.verify_citations_sample_rate = 1.0
        cfg.verify_citations_per_request_cap = 20
        out = await citation_verifier_node(
            state,
            page_bytes_provider=page_provider,
            verifier_call=stub_verifier,
        )

    assert out == {}
    assert invoked is False, "verifier_call invoked despite mode=off"
    assert repr(extraction) == extraction_before, "extraction mutated despite mode=off"
