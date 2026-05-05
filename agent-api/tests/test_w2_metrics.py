"""Phase 6.1 — Prometheus metrics for the W2 pipeline (W2_ARCHITECTURE §10.2).

Asserts the new metric names + labels are registered, that the
document-ingest happy path increments the ingest counter, that the critic
node increments the decisions counter for each verdict path, that the
demographic comparator increments the wrong-patient counter, and that the
retrieval pipeline emits both a metric and a single ``retrieval_completed``
audit row with cardinality only (no chunk content).

Also includes a PHI-tripwire test that captures every audit emission during
a happy ingest and asserts no clinical value snuck into ``detail_json``.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

pytestmark = [pytest.mark.hard_failure, pytest.mark.asyncio]

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# These imports must succeed to register the metrics on the default registry.
from agent.metrics import (  # noqa: E402
    agent_w2_classifier_confidence,
    agent_w2_critic_decisions_total,
    agent_w2_demographic_checks_total,
    agent_w2_document_ingest_total,
    agent_w2_extraction_duration_seconds,
    agent_w2_ocr_confidence,
    agent_w2_retrieval_duration_seconds,
    agent_w2_retrieval_hits_total,
    agent_watchdog_last_run_timestamp_seconds,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _scrape_metrics_text() -> str:
    """Render the global Prometheus registry to text. Used by labelled-counter
    asserts so we can grep for ``metric{label="value"} N``.
    """
    from prometheus_client import REGISTRY, generate_latest

    return generate_latest(REGISTRY).decode("utf-8")


def _counter_value(text: str, name: str, **labels: str) -> float:
    """Return the float value for one labelled sample line; 0.0 if missing.

    Matches on label-set equality regardless of label ordering — Prometheus
    emits labels alphabetically while callers pass them in semantic order.
    """
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        try:
            label_part, value_str = line[len(name) + 1 :].rsplit("} ", 1)
        except ValueError:
            continue
        # Split label_part on `,` and parse `k="v"` pairs.
        parsed: dict[str, str] = {}
        for tok in label_part.split(","):
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            parsed[k.strip()] = v.strip().strip('"')
        if parsed == labels:
            try:
                return float(value_str)
            except ValueError:
                return 0.0
    return 0.0


# ── 1. Module-surface assertion ──────────────────────────────────────────────


def test_metrics_module_exposes_w2_names() -> None:
    """Each W2 metric is importable, callable/labelable, and registered."""
    # All labelled metrics support .labels(...).inc() / .observe()
    agent_w2_document_ingest_total.labels(
        path="fhir", doc_type="lab_report", outcome="success"
    )
    agent_w2_extraction_duration_seconds.labels(
        doc_type="lab_report", classifier_confidence_bucket="high"
    )
    agent_w2_retrieval_duration_seconds.labels(mode="sparse")
    agent_w2_retrieval_hits_total.labels(mode="sparse")
    agent_w2_critic_decisions_total.labels(decision="pass", reason="none")
    agent_w2_demographic_checks_total.labels(outcome="pass")
    agent_w2_classifier_confidence.labels(doc_type="lab_report")
    agent_w2_ocr_confidence.labels(doc_type="lab_report")
    # The watchdog gauge takes no labels.
    agent_watchdog_last_run_timestamp_seconds.set(0.0)


# ── 2. Document ingest happy path increments ─────────────────────────────────


async def test_document_ingest_increments_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive a happy /document/ingest and assert the success counter advanced."""
    # Reuse the test_document_ingest helpers — same mock surface.
    from tests import test_document_ingest as tdi

    pdf_path = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"
    pdf_bytes = pdf_path.read_bytes()

    before = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_document_ingest_total",
        path="fhir",
        doc_type="lab_report",
        outcome="success",
    )

    tdi._patch_pipeline(monkeypatch)
    resp = await tdi._post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text

    after = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_document_ingest_total",
        path="fhir",
        doc_type="lab_report",
        outcome="success",
    )
    assert after == before + 1


# ── 3. Critic decisions counter, all three verdict paths ─────────────────────


def _layout(bbox_id: str, text: str, conf: float = 1.0) -> dict[str, Any]:
    return {
        "bbox_id": bbox_id,
        "page": 1,
        "bbox": [0.0, 0.0, 100.0, 20.0],
        "text": text,
        "ocr_confidence": conf,
    }


def _lab_extraction(*, bbox_id: str = "p1-b001", quote: str = "4.2") -> dict[str, Any]:
    return {
        "kind": "lab_report",
        "schema_version": "1.0",
        "patient_id": "PT-1",
        "document_reference_id": "DocumentReference/abc",
        "collection_facility": None,
        "values": [
            {
                "test_name": "Lactate",
                "normalized_test_name": "lactate",
                "value": "4.2",
                "unit": "mmol/L",
                "normalized_unit": "mmol/l",
                "reference_range": "0.5-2.0",
                "collection_date": "2024-01-15",
                "abnormal_flag": "high",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/abc",
                        "page_or_section": "p1",
                        "field_or_chunk_id": bbox_id,
                        "quote_or_value": quote,
                    }
                ],
            }
        ],
        "classifier_confidence": 0.95,
        "ocr_confidence_range": [0.95, 1.0],
        "extracted_at": "2024-01-15T10:00:00+00:00",
    }


async def test_critic_increments_decisions_total() -> None:
    from graph.nodes.critic import critic_node
    from graph.state import make_initial_state

    # ── pass path ─────────────────────────────────────────────────────────
    state = make_initial_state(
        request_id="rid", session_id="sess", provider_id="prov"
    )
    state["extraction"] = _lab_extraction()
    state["ocr_layout"] = [_layout("p1-b001", "Lactate 4.2 mmol/L")]
    before_pass = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_critic_decisions_total",
        decision="pass",
        reason="none",
    )
    with patch("graph.nodes.critic.audit_writer.emit", new=AsyncMock()):
        await critic_node(state)
    after_pass = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_critic_decisions_total",
        decision="pass",
        reason="none",
    )
    assert after_pass > before_pass

    # ── hard_block path (unresolvable bbox → CITATION_UNRESOLVABLE) ──────
    state2 = make_initial_state(
        request_id="rid2", session_id="sess2", provider_id="prov2"
    )
    state2["extraction"] = _lab_extraction(bbox_id="p9-b999")
    state2["ocr_layout"] = [_layout("p1-b001", "Lactate 4.2")]
    before_hb = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_critic_decisions_total",
        decision="hard_block",
        reason="CITATION_UNRESOLVABLE",
    )
    with patch("graph.nodes.critic.audit_writer.emit", new=AsyncMock()):
        await critic_node(state2)
    after_hb = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_critic_decisions_total",
        decision="hard_block",
        reason="CITATION_UNRESOLVABLE",
    )
    assert after_hb > before_hb

    # ── soft_warn path: demographic soft_warn folded into critic state ───
    state3 = make_initial_state(
        request_id="rid3", session_id="sess3", provider_id="prov3"
    )
    state3["extraction"] = _lab_extraction()
    state3["ocr_layout"] = [_layout("p1-b001", "Lactate 4.2 mmol/L")]
    state3["demographic_check"] = {
        "decision": "soft_warn",
        "reason_code": "MRN_MATCH_DOB_MISMATCH",
        "message": "",
    }
    # soft_warn from demographic fold-in keeps the critic decision at "pass"
    # (demographic soft_warns surface as soft_warns, not violation_codes —
    # see critic._check_document_path). We instead exercise the soft_warn
    # branch through the structured-data path.
    from verification.dispatcher_response import VerificationResult

    state4 = make_initial_state(
        request_id="rid4", session_id="sess4", provider_id="prov4"
    )
    state4["structured_response"] = {"text": "x"}
    fake = VerificationResult(
        passed=False,
        blocked=False,
        violations=["LOW_CONFIDENCE_TRUNCATED"],
        modified_response={"text": "x"},
        physician_message=None,
    )
    before_sw = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_critic_decisions_total",
        decision="soft_warn",
        reason="LOW_CONFIDENCE_TRUNCATED",
    )
    with patch("graph.nodes.critic.audit_writer.emit", new=AsyncMock()):
        with patch(
            "graph.nodes.critic.verify_dispatcher_response", return_value=fake
        ):
            await critic_node(state4)
    after_sw = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_critic_decisions_total",
        decision="soft_warn",
        reason="LOW_CONFIDENCE_TRUNCATED",
    )
    assert after_sw > before_sw


# ── 4. Demographic check counter ─────────────────────────────────────────────


async def test_demographic_check_increments_outcome() -> None:
    """A chart-vs-document MRN mismatch hard-blocks → counter advances."""
    from graph.nodes.demographics import demographics_node

    chart_patient = {
        "id": "pt-1",
        "birthDate": "1962-03-14",
        "name": [{"given": ["Jane"], "family": "Doe"}],
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": "12345"}
        ],
    }

    async def _provider(_pid: str) -> dict[str, Any]:
        return chart_patient

    extraction = _lab_extraction()
    extraction["demographics"] = {
        "mrn": "99999",
        "name": "Jane Doe",
        "dob": "1962-03-14",
    }

    state = {
        "request_id": "rid-d",
        "session_id": "sess-d",
        "provider_id": "prov-d",
        "patient_id": "pt-1",
        "extraction": extraction,
        "errors": [],
    }

    before = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_demographic_checks_total",
        outcome="hard_block",
    )
    with patch("graph.nodes.demographics.audit_writer.emit", new=AsyncMock()):
        await demographics_node(state, fhir_patient_provider=_provider)  # type: ignore[arg-type]
    after = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_demographic_checks_total",
        outcome="hard_block",
    )
    assert after > before


# ── 5. Retrieval emits one retrieval_completed audit row + metric increments ─


async def test_retrieval_emits_audit_and_metrics() -> None:
    from rag.retrieve import Snippet

    fake_snippets = [
        Snippet(
            chunk_id="c1",
            source_id="kdigo-aki-2012",
            document_title="KDIGO AKI 2012",
            section="STAGE 2",
            page_number=1,
            indexed_version_date=_dt.date(2012, 3, 1),
            content="content one",
            relevance_score=0.9,
        )
    ]
    fake_stats = {
        "sparse_hits": 7,
        "dense_hits": 5,
        "after_rerank": 1,
        "rerank_used": True,
        "sparse_seconds": 0.01,
        "dense_seconds": 0.02,
        "merge_seconds": 0.001,
        "rerank_seconds": 0.05,
        "query_prefix": "stage 2 aki",
    }

    state = {
        "request_id": "rid-r",
        "session_id": "sess-r",
        "provider_id": "prov-r",
        "patient_id": "pt-r",
        "message": "stage 2 aki criteria",
        "errors": [],
    }

    before = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_retrieval_hits_total",
        mode="sparse",
    )
    audit_mock = AsyncMock()
    from graph.nodes import retriever as _retr_mod

    with patch.object(
        _retr_mod, "audit_writer", new=type("X", (), {"emit": audit_mock})()
    ):
        with patch("rag.retrieve.search", new=AsyncMock(return_value=fake_snippets)):
            with patch(
                "rag.retrieve.get_last_retrieval_stats", return_value=fake_stats
            ):
                await _retr_mod.retriever_node(state)  # type: ignore[arg-type]

    after = _counter_value(
        _scrape_metrics_text(),
        "agent_w2_retrieval_hits_total",
        mode="sparse",
    )
    assert after >= before + 7

    # Assert exactly one ``retrieval_completed`` row went out alongside the
    # ``node_handoff`` row that the existing slice emits.
    types = [c.args[0].event_type for c in audit_mock.await_args_list]
    assert types.count("retrieval_completed") == 1
    rc = next(
        c.args[0]
        for c in audit_mock.await_args_list
        if c.args[0].event_type == "retrieval_completed"
    )
    assert rc.detail_json["sparse_hits"] == 7
    assert rc.detail_json["dense_hits"] == 5
    assert rc.detail_json["after_rerank"] == 1
    assert rc.detail_json["rerank_used"] is True


# ── 6. PHI tripwire over a happy ingest ──────────────────────────────────────


async def test_no_phi_in_audit_detail_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture every emit during a happy ingest; assert no clinical values."""
    from tests import test_document_ingest as tdi

    pdf_path = ROOT / "tests" / "fixtures" / "lab_osh_lactate.pdf"
    pdf_bytes = pdf_path.read_bytes()

    mocks = tdi._patch_pipeline(monkeypatch)
    resp = await tdi._post_ingest(pdf_bytes)
    assert resp.status_code == 200, resp.text

    audit_mock = mocks["audit_emit"]
    forbidden = ("4.2", "lactate", "marcus", "webb", "1962-03-14", "12345")
    for call in audit_mock.await_args_list:
        event = call.args[0]
        serialized = str(event.detail_json or {}).lower()
        for word in forbidden:
            assert word not in serialized, (event.event_type, serialized)
