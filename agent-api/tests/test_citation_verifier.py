"""Tests for ``agent.citation_verifier`` (Wave 2C).

Covered paths:

* ``yes``     — citation untouched, verification block attached, no metric
                anomalies.
* ``partial`` — citation kept, WORD-granularity block downgraded to its
                LINE parent.
* ``no``      — repoint once, then drop on second ``no``; parent value
                gets ``needs_review = True``.
* cap         — once ``verify_citations_per_request_cap`` calls have
                fired, remaining citations are skipped uniformly and
                ``agent_verifier_capped_total`` increments.
* sample      — deterministic 10 % sampler hits the same citations on
                replay; off-mode is a no-op.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.hard_failure

from agent.citation_verifier import (
    _citation_id,
    _padded_bbox,
    _parse_verifier_response,
    _polygon_to_bbox,
    _should_sample,
    citation_verifier_node,
)
from extractors.schemas import VerificationResult


# ── Helpers ──────────────────────────────────────────────────────────────────


def _png_bytes() -> bytes:
    """Tiny in-memory PNG so the cropper has something real to slice."""
    from PIL import Image

    img = Image.new("RGB", (200, 100), color=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _layout_word_and_line() -> list[dict[str, Any]]:
    return [
        {
            "bbox_id": "p1-w001",
            "page": 1,
            "bbox": [10.0, 10.0, 30.0, 12.0],
            "text": "4.2",
            "ocr_confidence": 0.95,
            "granularity": "word",
            "parent_line_id": "p1-l001",
        },
        {
            "bbox_id": "p1-l001",
            "page": 1,
            "bbox": [5.0, 8.0, 100.0, 16.0],
            "text": "Lactate 4.2 mmol/L",
            "ocr_confidence": 0.95,
            "granularity": "line",
        },
        {
            "bbox_id": "p1-l002",
            "page": 1,
            "bbox": [5.0, 30.0, 120.0, 16.0],
            "text": "alternative line containing 4.2",
            "ocr_confidence": 0.95,
            "granularity": "line",
        },
    ]


def _lab_extraction(citation_field: str = "p1-w001") -> dict[str, Any]:
    return {
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
                        "field_or_chunk_id": citation_field,
                        "quote_or_value": "4.2",
                        "bbox": (10.0, 10.0, 30.0, 12.0),
                    }
                ],
            }
        ],
    }


async def _provider_always(_citation: dict[str, Any]) -> bytes:
    return _png_bytes()


# ── Pure-helper tests ────────────────────────────────────────────────────────


def test_should_sample_deterministic_for_same_id() -> None:
    cid = _citation_id(0, 0, {"field_or_chunk_id": "x", "quote_or_value": "y"})
    assert _should_sample(cid, 1.0) is True
    assert _should_sample(cid, 0.0) is False
    assert _should_sample(cid, 0.5) == _should_sample(cid, 0.5)


def test_padded_bbox_grows_by_10_percent() -> None:
    out = _padded_bbox((100.0, 100.0, 50.0, 20.0))
    assert out == (95.0, 98.0, 60.0, 24.0)


def test_polygon_to_bbox() -> None:
    poly = [(10.0, 20.0), (30.0, 22.0), (28.0, 40.0), (8.0, 38.0)]
    assert _polygon_to_bbox(poly) == (8.0, 20.0, 22.0, 20.0)


def test_parse_verifier_response_strips_fences() -> None:
    out = _parse_verifier_response('```json\n{"status":"yes","rationale":"value present"}\n```')
    assert out.status == "yes"
    assert out.rationale == "value present"


def test_parse_verifier_response_invalid_json_falls_back_to_no() -> None:
    out = _parse_verifier_response("not json at all")
    assert out.status == "no"


# ── Node-level outcome tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_yes_outcome_keeps_citation_and_attaches_verification() -> None:
    state = {
        "extraction": _lab_extraction(),
        "ocr_layout": _layout_word_and_line(),
        "request_id": "req-yes",
        "session_id": "s",
    }

    async def stub(value: str, crop: bytes) -> VerificationResult:
        assert value == "4.2"
        return VerificationResult(status="yes", rationale="value clearly visible")

    with patch("agent.citation_verifier.settings") as cfg:
        cfg.verify_citations = "all"
        cfg.verify_citations_sample_rate = 1.0
        cfg.verify_citations_per_request_cap = 20
        out = await citation_verifier_node(
            state,
            page_bytes_provider=_provider_always,
            verifier_call=stub,
        )
    val = out["extraction"]["values"][0]
    assert val.get("needs_review", False) is False
    assert len(val["citations"]) == 1
    assert val["citations"][0]["verification"]["status"] == "yes"


@pytest.mark.asyncio
async def test_partial_outcome_downgrades_word_to_line() -> None:
    state = {
        "extraction": _lab_extraction(citation_field="p1-w001"),
        "ocr_layout": _layout_word_and_line(),
    }

    async def stub(value: str, crop: bytes) -> VerificationResult:
        return VerificationResult(status="partial", rationale="only digit visible")

    with patch("agent.citation_verifier.settings") as cfg:
        cfg.verify_citations = "all"
        cfg.verify_citations_sample_rate = 1.0
        cfg.verify_citations_per_request_cap = 20
        out = await citation_verifier_node(
            state,
            page_bytes_provider=_provider_always,
            verifier_call=stub,
        )
    citation = out["extraction"]["values"][0]["citations"][0]
    assert citation["field_or_chunk_id"] == "p1-l001"  # downgraded to line parent
    assert citation["verification"]["status"] == "partial"


@pytest.mark.asyncio
async def test_no_outcome_repoints_then_drops_and_flags_needs_review() -> None:
    state = {
        "extraction": _lab_extraction(citation_field="p1-w001"),
        "ocr_layout": _layout_word_and_line(),
    }

    calls: list[str] = []

    async def stub(value: str, crop: bytes) -> VerificationResult:
        calls.append(value)
        return VerificationResult(status="no", rationale="value not visible")

    with patch("agent.citation_verifier.settings") as cfg:
        cfg.verify_citations = "all"
        cfg.verify_citations_sample_rate = 1.0
        cfg.verify_citations_per_request_cap = 20
        out = await citation_verifier_node(
            state,
            page_bytes_provider=_provider_always,
            verifier_call=stub,
        )
    val = out["extraction"]["values"][0]
    # First "no" triggered a repoint; second "no" dropped the citation.
    assert len(calls) == 2
    assert val["citations"] == []
    assert val["needs_review"] is True


@pytest.mark.asyncio
async def test_per_request_cap_skips_remaining_and_increments_metric() -> None:
    # Build an extraction with 5 cited items so the cap (set to 2) trips.
    extraction: dict[str, Any] = {"kind": "lab_report", "values": []}
    for i in range(5):
        extraction["values"].append(
            {
                "test_name": f"T{i}",
                "value": str(i),
                "abnormal_flag": "normal",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "DocumentReference/abc",
                        "field_or_chunk_id": "p1-l001",
                        "quote_or_value": str(i),
                        "bbox": (10.0, 10.0, 30.0, 12.0),
                    }
                ],
            }
        )
    state = {"extraction": extraction, "ocr_layout": _layout_word_and_line()}

    async def stub(value: str, crop: bytes) -> VerificationResult:
        return VerificationResult(status="yes", rationale="ok")

    from agent.metrics import agent_verifier_capped_total

    before = agent_verifier_capped_total.labels(reason="cap")._value.get()  # type: ignore[attr-defined]
    with patch("agent.citation_verifier.settings") as cfg:
        cfg.verify_citations = "all"
        cfg.verify_citations_sample_rate = 1.0
        cfg.verify_citations_per_request_cap = 2  # cap fires after 2 calls
        await citation_verifier_node(
            state,
            page_bytes_provider=_provider_always,
            verifier_call=stub,
        )
    after = agent_verifier_capped_total.labels(reason="cap")._value.get()  # type: ignore[attr-defined]
    # 5 citations - 2 verified = 3 cap-skipped.
    assert after - before == 3


@pytest.mark.asyncio
async def test_off_mode_is_a_noop() -> None:
    state = {"extraction": _lab_extraction(), "ocr_layout": _layout_word_and_line()}
    called = False

    async def stub(value: str, crop: bytes) -> VerificationResult:
        nonlocal called
        called = True
        return VerificationResult(status="yes", rationale="x")

    with patch("agent.citation_verifier.settings") as cfg:
        cfg.verify_citations = "off"
        cfg.verify_citations_sample_rate = 1.0
        cfg.verify_citations_per_request_cap = 20
        out = await citation_verifier_node(
            state,
            page_bytes_provider=_provider_always,
            verifier_call=stub,
        )
    assert called is False
    assert out == {}


@pytest.mark.asyncio
async def test_sample_mode_is_deterministic_across_replays() -> None:
    # Build 100 citations — at 10 % rate ~10 should be sampled. The
    # important property is that two replays select the same set.
    def make_state() -> dict[str, Any]:
        extraction: dict[str, Any] = {"kind": "lab_report", "values": []}
        for i in range(100):
            extraction["values"].append(
                {
                    "test_name": f"T{i}",
                    "value": str(i),
                    "abnormal_flag": "normal",
                    "citations": [
                        {
                            "source_type": "document",
                            "source_id": "DocumentReference/abc",
                            "field_or_chunk_id": "p1-l001",
                            "quote_or_value": f"q-{i}",
                            "bbox": (10.0, 10.0, 30.0, 12.0),
                        }
                    ],
                }
            )
        return {"extraction": extraction, "ocr_layout": _layout_word_and_line()}

    sampled_runs: list[list[str]] = []

    for _ in range(2):
        seen: list[str] = []

        async def recording_stub(value: str, crop: bytes) -> VerificationResult:
            seen.append(value)
            return VerificationResult(status="yes", rationale="ok")

        with patch("agent.citation_verifier.settings") as cfg:
            cfg.verify_citations = "sample"
            cfg.verify_citations_sample_rate = 0.1
            cfg.verify_citations_per_request_cap = 100
            await citation_verifier_node(
                make_state(),
                page_bytes_provider=_provider_always,
                verifier_call=recording_stub,
            )
        sampled_runs.append(seen)

    # Replay determinism: same citations sampled in both runs.
    assert sampled_runs[0] == sampled_runs[1]
    # Sanity: at 10 % we expect roughly 10 — guard against an obvious off-by-N.
    assert 3 <= len(sampled_runs[0]) <= 25
