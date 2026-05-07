"""Tests for the DOCX prose-mode intake extractor (Phase 9 Slice 9.6).

Covers:

- ``_build_user_content_prose`` skips image blocks and includes the
  paragraph + run JSON.
- Round-trip: a stubbed Claude response feeds into
  ``extract_intake_from_docx`` and yields a valid ``IntakeForm`` whose
  citation locator is ``para=13|run=2`` for the LDL-142 fact (the spec's
  required prose-mode anchor).
- Empty-paragraphs fallback returns ``UnknownDocument`` (no LLM call).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from documents.docx_loader import extract_docx_paragraphs  # noqa: E402
from extractors.intake import (  # noqa: E402
    _build_user_content_prose,
    extract_intake_from_docx,
)
from extractors.schemas import IntakeForm, UnknownDocument  # noqa: E402

pytestmark = pytest.mark.hard_failure

DOCX_DIR = ROOT / "tests" / "fixtures" / "w2" / "multimodal" / "docx"
CHEN_DOCX = DOCX_DIR / "p01-chen-referral.docx"


# --------------------------------------------------------------------------- #
# _build_user_content_prose
# --------------------------------------------------------------------------- #


def test_prose_user_content_has_no_image_blocks() -> None:
    paragraphs, _meta = extract_docx_paragraphs(CHEN_DOCX.read_bytes())
    content = _build_user_content_prose(
        paragraphs,
        patient_id="pt-123",
        document_reference_id="doc-1",
        tracked_changes_present=False,
    )
    assert content, "prose content must be non-empty"
    # Every block must be type=text. NO image blocks — DOCX has no images.
    for block in content:
        assert block["type"] == "text", f"unexpected block type {block['type']}"
    body = content[0]["text"]
    assert "patient_id = pt-123" in body
    assert "document_reference_id = doc-1" in body
    # Locator grammar must appear in the serialized prose.
    assert "para=13" in body
    assert "run=2" in body
    # The Chen-specific fact is in the run text.
    assert "LDL-C at 142 mg/dL" in body


def test_prose_user_content_carries_tracked_changes_flag() -> None:
    paragraphs, _meta = extract_docx_paragraphs(CHEN_DOCX.read_bytes())
    content = _build_user_content_prose(
        paragraphs,
        patient_id="pt-1",
        document_reference_id="doc-1",
        tracked_changes_present=True,
    )
    body = content[0]["text"]
    assert "tracked_changes_present = true" in body


# --------------------------------------------------------------------------- #
# extract_intake_from_docx round-trip with a stubbed LLM
# --------------------------------------------------------------------------- #


def _stub_claude_response_with_ldl_142() -> dict:
    """Build a valid IntakeForm tool_input where the LDL-142 fact lands
    on a current_medications entry (statin) with citation locator
    para=13|run=2 — the spec's required citable LabValue/LDL anchor.

    Note: the IntakeForm schema does NOT carry standalone LabValues; the
    prose extractor surfaces numeric facts via medication entries OR
    chief_concern citations. We use the chief_concern path for the LDL
    fact (it's a clinical concern) and a separate medication entry for
    atorvastatin so both appear in the round-trip."""
    return {
        "kind": "intake_form",
        "schema_version": "1.0",
        "patient_id": "pt-chen",
        "document_reference_id": "doc-chen",
        "demographics": {
            "name": {
                "value": "Margaret Chen",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "doc-chen",
                        "page_or_section": "prose",
                        "field_or_chunk_id": "para=10|run=1",
                        "quote_or_value": "Margaret Chen",
                    }
                ],
            }
        },
        "chief_concern": {
            "value": "LDL-C at 142 mg/dL on atorvastatin 40 mg",
            "citations": [
                {
                    "source_type": "document",
                    "source_id": "doc-chen",
                    "page_or_section": "History of Present Illness",
                    "field_or_chunk_id": "para=13|run=2",
                    "quote_or_value": "LDL-C at 142 mg/dL",
                }
            ],
        },
        "current_medications": [
            {
                "name": "atorvastatin 40 mg PO daily",
                "citations": [
                    {
                        "source_type": "document",
                        "source_id": "doc-chen",
                        "page_or_section": "Current Medications",
                        "field_or_chunk_id": "para=19",
                        "quote_or_value": "atorvastatin 40 mg PO daily",
                    }
                ],
            }
        ],
        "allergies": [],
        "family_history": [],
        "classifier_confidence": 0.92,
        "ocr_confidence_range": [1.0, 1.0],
        "extracted_at": _dt.datetime(
            2026, 5, 6, 12, 0, 0, tzinfo=_dt.timezone.utc
        ).isoformat(),
    }


@pytest.mark.asyncio
async def test_extract_intake_from_docx_round_trip_chen_ldl_142() -> None:
    """End-to-end round-trip with a stubbed Claude response. The
    resulting IntakeForm must carry the LDL=142 fact with a
    ``para=13|run=2`` locator (the spec's required citable anchor)."""
    tool_input = _stub_claude_response_with_ldl_142()

    fake_resp = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_intake_form",
                input=tool_input,
            )
        ]
    )
    fake_messages = MagicMock()
    fake_messages.create = AsyncMock(return_value=fake_resp)
    fake_client = MagicMock()
    fake_client.messages = fake_messages

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        result = await extract_intake_from_docx(
            CHEN_DOCX.read_bytes(),
            patient_id="pt-chen",
            document_reference_id="doc-chen",
        )

    assert isinstance(result, IntakeForm)
    # Demographic citation lands on para=10|run=1.
    assert result.demographics is not None
    assert result.demographics.name is not None
    assert (
        result.demographics.name.citations[0].field_or_chunk_id
        == "para=10|run=1"
    )
    # The LDL-142 fact lands on chief_concern with para=13|run=2 — the
    # spec's required citable anchor.
    assert result.chief_concern is not None
    assert "142 mg/dL" in result.chief_concern.citations[0].quote_or_value
    assert (
        result.chief_concern.citations[0].field_or_chunk_id
        == "para=13|run=2"
    )
    # OCR confidence range is forced to (1.0, 1.0) for prose mode.
    assert result.ocr_confidence_range == (1.0, 1.0)


@pytest.mark.asyncio
async def test_extract_intake_from_docx_empty_returns_unknown(
    tmp_path: Path,
) -> None:
    """Empty DOCX must short-circuit to UnknownDocument (no LLM call)."""
    import docx as _docx

    src = tmp_path / "empty.docx"
    _docx.Document().save(src)

    # No anthropic patch — if we accidentally call it the test fails
    # with a network error rather than a silent success.
    result = await extract_intake_from_docx(
        src.read_bytes(),
        patient_id="pt-x",
        document_reference_id="doc-x",
    )
    assert isinstance(result, UnknownDocument)
    assert result.document_kind_guess == "docx_empty"
