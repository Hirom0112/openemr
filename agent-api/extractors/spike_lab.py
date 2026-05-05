"""Phase 1 SPIKE — Claude Sonnet 4.5 vision + PyMuPDF layout → LabReport.

Proves end-to-end (W2_ARCHITECTURE.md §5.3, §8) that:

1. PyMuPDF emits stable bbox IDs.
2. Claude can fill the LabReport schema using ONLY values located in the OCR
   layout, attaching a Citation per filled field.
3. Every Citation.field_or_chunk_id resolves to a real bbox.
4. Every Citation.quote_or_value is a normalized substring of that bbox's
   OCR text (the §8.4 fidelity check).

If this round-trip doesn't hold, the rest of Pillar 1 is unviable.

CLI:
    python -m extractors.spike_lab agent-api/tests/fixtures/lab_osh_lactate.pdf
    (run from agent-api/)

Or from repo root:
    python -m agent-api.extractors.spike_lab agent-api/tests/fixtures/lab_osh_lactate.pdf
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List

import anthropic
import pymupdf

# Local imports — support both `python -m extractors.spike_lab` (cwd=agent-api)
# and direct invocation by augmenting sys.path.
_THIS = Path(__file__).resolve()
_AGENT_API = _THIS.parents[1]
if str(_AGENT_API) not in sys.path:
    sys.path.insert(0, str(_AGENT_API))

from documents.ocr import LayoutBlock, document_confidence, extract_layout  # noqa: E402
from extractors.schemas import Citation, LabReport, LabValue  # noqa: E402

logger = logging.getLogger(__name__)

# Model preferences with fallbacks per task brief.
_MODEL_CANDIDATES = (
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5",
    "claude-3-5-sonnet-20241022",
)

_PROMPT = """You are extracting structured lab data from an outside-hospital
laboratory report. You have two inputs:

1. One image per page of the PDF.
2. A JSON layout produced by deterministic OCR. Each block has a `bbox_id`
   (e.g. "p2-b005"), the page number, and the OCR text inside that region.

Your job: fill the LabReport schema by calling the `submit_lab_report` tool.

HARD RULES (the agent will reject your output otherwise):

- Use ONLY values you can locate in the OCR layout. Do NOT invent bbox_ids.
- For EVERY filled clinical field, attach a Citation with:
    source_type      = "document"
    source_id        = the document_reference_id passed to you
    page_or_section  = the page number as a string ("1", "2", ...)
    field_or_chunk_id = the bbox_id from the OCR layout (e.g. "p2-b005")
    quote_or_value   = the exact substring from THAT bbox's text that
                       contains the value. Do NOT rephrase. If the bbox
                       text is "Lactate\\n4.2\\nmmol/L\\n0.5 - 2.2\\nHH"
                       and you are citing the value "4.2", quote_or_value
                       MUST be "4.2" (or any exact substring of that
                       block's text that contains "4.2").
- Each LabValue.citations must have at least one citation.
- For abnormal_flag, map: "HH"->"critical_high", "LL"->"critical_low",
  "H"->"high", "L"->"low", blank->"normal".
- normalized_test_name: lowercase test name (e.g. "lactate", "wbc",
  "creatinine", "sodium").
- Set kind="lab_report", schema_version="1.0".
- Set classifier_confidence to a float in [0,1] reflecting your certainty
  this is a lab report.
- Set ocr_confidence_range to (min_conf, max_conf) across cited blocks.
- Set extracted_at to the current UTC ISO 8601 timestamp.

Inputs follow.
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace for substring fidelity check."""
    return re.sub(r"\s+", " ", s.lower()).strip()


def _render_pages_to_png(pdf_bytes: bytes) -> List[bytes]:
    pngs: List[bytes] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
            pngs.append(pix.tobytes("png"))
    return pngs


def _layout_to_prompt_json(blocks: Iterable[LayoutBlock]) -> str:
    return json.dumps(
        [
            {
                "bbox_id": b.bbox_id,
                "page": b.page,
                "text": b.text,
                "ocr_confidence": b.ocr_confidence,
            }
            for b in blocks
        ],
        ensure_ascii=False,
        indent=2,
    )


def _build_user_content(
    pdf_bytes: bytes,
    blocks: List[LayoutBlock],
    patient_id: str,
    document_reference_id: str,
) -> List[dict[str, Any]]:
    pngs = _render_pages_to_png(pdf_bytes)
    content: List[dict[str, Any]] = []
    for png in pngs:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": (
                f"patient_id = {patient_id}\n"
                f"document_reference_id = {document_reference_id}\n"
                f"current_utc = {datetime.now(timezone.utc).isoformat()}\n\n"
                "OCR layout JSON (the only source of truth for bbox_ids):\n"
                f"{_layout_to_prompt_json(blocks)}"
            ),
        }
    )
    return content


def _call_claude(
    client: anthropic.Anthropic,
    user_content: List[dict[str, Any]],
) -> dict[str, Any]:
    """Call Claude with the lab-report tool. Returns the tool input dict.

    Tries model candidates in order until one succeeds.
    """
    tool = {
        "name": "submit_lab_report",
        "description": "Submit the structured LabReport extracted from the document.",
        "input_schema": LabReport.model_json_schema(),
    }
    last_err: Exception | None = None
    for model in _MODEL_CANDIDATES:
        try:
            t0 = time.monotonic()
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_lab_report"},
                system=_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "spike_claude_call_ok",
                extra={"model": model, "duration_ms": duration_ms},
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "submit_lab_report":
                    return dict(block.input)
            raise RuntimeError(f"no tool_use block in response from {model}")
        except anthropic.NotFoundError as e:
            last_err = e
            logger.warning(
                "spike_claude_model_unavailable",
                extra={"model": model, "error": str(e)},
            )
            continue
    raise RuntimeError(f"all model candidates failed; last error: {last_err}")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def extract_lab_spike(
    pdf_bytes: bytes,
    patient_id: str,
    document_reference_id: str,
) -> LabReport:
    """Run the spike pipeline and return a validated LabReport.

    Raises pydantic ValidationError if Claude's output doesn't validate.
    Citation fidelity is checked separately by `verify_citations`.
    """
    blocks = extract_layout(pdf_bytes)
    if not blocks:
        raise RuntimeError("OCR layer produced no blocks — cannot proceed")

    client = anthropic.Anthropic()
    user_content = _build_user_content(
        pdf_bytes, blocks, patient_id, document_reference_id
    )
    tool_input = _call_claude(client, user_content)
    # Claude returns the tool input as a dict-of-JSON-primitives. Strict mode
    # rejects ISO date/datetime strings and lists-as-tuples, so we round-trip
    # through JSON — the JSON validator parses strings into dates/datetimes
    # and accepts arrays for tuple fields.
    return LabReport.model_validate_json(json.dumps(tool_input))


# --------------------------------------------------------------------------- #
# Self-check (the architectural claim)
# --------------------------------------------------------------------------- #


def verify_citations(
    report: LabReport, blocks: List[LayoutBlock]
) -> tuple[bool, List[str]]:
    """Verify every citation resolves and quote_or_value is a substring.

    Returns (ok, diagnostic_lines). One line per citation, prefixed with
    PASS or FAIL. The fidelity rule applies only to observed fields; here
    every citation we walk is on an observed lab value.
    """
    by_id = {b.bbox_id: b for b in blocks}
    lines: List[str] = []
    all_ok = True
    for lv in report.values:
        for c in lv.citations:
            if c.source_type != "document":
                # Spike only emits document citations; flag anything else.
                lines.append(
                    f"FAIL  {lv.test_name}: non-document source_type={c.source_type!r}"
                )
                all_ok = False
                continue
            block = by_id.get(c.field_or_chunk_id)
            if block is None:
                lines.append(
                    f"FAIL  {lv.test_name}: bbox_id {c.field_or_chunk_id!r} "
                    f"not in layout"
                )
                all_ok = False
                continue
            if _normalize(c.quote_or_value) not in _normalize(block.text):
                lines.append(
                    f"FAIL  {lv.test_name}: quote_or_value "
                    f"{c.quote_or_value!r} not in bbox {c.field_or_chunk_id} "
                    f"text {block.text!r}"
                )
                all_ok = False
                continue
            lines.append(
                f"PASS  {lv.test_name}={lv.value} {lv.unit or ''} "
                f"-> {c.field_or_chunk_id} quote={c.quote_or_value!r}"
            )
    return all_ok, lines


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(argv) < 2:
        print("usage: python -m extractors.spike_lab <pdf_path>", file=sys.stderr)
        return 2
    pdf_path = Path(argv[1])
    if not pdf_path.exists():
        print(f"file not found: {pdf_path}", file=sys.stderr)
        return 2

    pdf_bytes = pdf_path.read_bytes()
    blocks = extract_layout(pdf_bytes)
    print(f"[ocr] extracted {len(blocks)} blocks across "
          f"{blocks[-1].page if blocks else 0} pages "
          f"(doc_conf={document_confidence(blocks):.2f})")

    patient_id = "pt-spike-001"
    document_reference_id = "doc-spike-001"

    try:
        report = extract_lab_spike(pdf_bytes, patient_id, document_reference_id)
    except Exception as e:
        print(f"FAIL  extraction error: {e}", file=sys.stderr)
        return 1

    print(f"[claude] returned {len(report.values)} lab values")
    for lv in report.values:
        print(f"  - {lv.test_name}={lv.value} {lv.unit or ''} "
              f"flag={lv.abnormal_flag} ({len(lv.citations)} citation(s))")

    ok, lines = verify_citations(report, blocks)
    print()
    print("=" * 60)
    print("CITATION FIDELITY CHECK")
    print("=" * 60)
    for ln in lines:
        print(ln)
    print()

    # Acceptance: at least 3 of 4 expected labs must be cited and verified,
    # and Lactate MUST be among them.
    expected = {"lactate", "wbc", "cr", "creatinine", "sodium"}
    verified_norm: set[str] = set()
    for lv in report.values:
        # Only count verified rows (all citations passed).
        verified_for_this = all(
            c.source_type == "document"
            and c.field_or_chunk_id in {b.bbox_id for b in blocks}
            and _normalize(c.quote_or_value)
            in _normalize(
                next(b for b in blocks if b.bbox_id == c.field_or_chunk_id).text
            )
            for c in lv.citations
        )
        if verified_for_this:
            verified_norm.add(lv.normalized_test_name.lower())

    has_lactate = "lactate" in verified_norm
    n_verified = len(verified_norm & expected)
    print(f"verified test_names = {sorted(verified_norm)}")
    print(f"n_expected_verified = {n_verified} / 4 (need >= 3, lactate required)")

    overall_pass = ok and has_lactate and n_verified >= 3
    if overall_pass:
        print("\nSPIKE RESULT: PASS")
        return 0
    else:
        print("\nSPIKE RESULT: FAIL")
        if not has_lactate:
            print("  - Lactate not among verified values (required)")
        if n_verified < 3:
            print(f"  - only {n_verified}/4 expected labs verified (need 3)")
        if not ok:
            print("  - one or more citations failed resolution/fidelity")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
