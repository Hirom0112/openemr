"""Wave 2C — optional self-verifying citation pass (LangGraph node).

For each ``(value, citation)`` pair in ``state["extraction"]`` we crop the
cited region from the source document and ask Claude vision a single,
short question:

    "Is the value <X> visible in this image crop?
     Answer JSON: {"status": "yes|no|partial", "rationale": "<= 100 chars"}"

Outcome handling (per slice 3.x contract):

* ``yes``     — log only; attach :class:`VerificationResult` to the
                citation so downstream UI can surface a green tick.
* ``partial`` — attach result; if the citation's source LayoutBlock
                granularity is WORD, downgrade it to LINE so the
                surfaced bbox widens enough to encompass the value.
* ``no``      — attach result, then **repoint once** with the rejected
                ``field_or_chunk_id`` excluded. If the second verifier
                pass also returns ``no`` we drop the citation entirely
                and set ``needs_review = True`` on the parent value.

Cost discipline:

* ``settings.verify_citations`` gates the entire node. ``"off"`` makes
  this module a structural no-op.
* ``"sample"`` mode picks a deterministic 10 % slice (hash of
  ``citation_id``) — rerunning the same payload always hits the same
  citations, which keeps eval-suite numbers stable across reruns.
* ``settings.verify_citations_per_request_cap`` (default 20) is enforced
  PRE-call. When the cap fires the remaining citations are skipped
  uniformly and ``agent_verifier_capped_total{reason="cap"}`` increments.

The Claude call uses ``claude-haiku-4-5-20251001`` with vision input —
the cheapest current model that supports image input. JSON-mode keeps
the output schema bounded.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from agent.metrics import (
    agent_citation_verifier_outcome_total,
    agent_verifier_capped_total,
)
from config import settings
from extractors.schemas import VerificationResult

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

VERIFIER_MODEL = "claude-haiku-4-5-20251001"

# Padding (% of bbox width/height each side) applied when the source has no
# polygon. Matches the prompt's expectation of "context around the value".
_BBOX_PAD_FRACTION = 0.10

# Single-line user prompt. Kept short on purpose: the cheaper the prompt,
# the cheaper the second pass (Wave 2C cost guardrail).
VERIFIER_PROMPT = (
    'Is the value "{value}" visible in this image crop? '
    'Reply ONLY with JSON: {{"status": "yes|no|partial", '
    '"rationale": "<=100 chars"}}.'
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _citation_id(item_idx: int, cit_idx: int, citation: dict[str, Any]) -> str:
    """Stable id used for deterministic sampling.

    Uses the citation's content (field_or_chunk_id + quote) plus its position
    so a re-extraction that produces the same content also samples the same.
    """
    raw = (
        f"{item_idx}:{cit_idx}:"
        f"{citation.get('field_or_chunk_id', '')}:"
        f"{citation.get('quote_or_value', '')}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _should_sample(citation_id: str, sample_rate: float) -> bool:
    """Deterministic uniform sampler keyed by the citation_id hash.

    ``sample_rate`` ∈ [0.0, 1.0]; 0.0 → never, 1.0 → always.
    """
    if sample_rate <= 0.0:
        return False
    if sample_rate >= 1.0:
        return True
    # First 8 hex chars → 32-bit int → normalize to [0, 1).
    bucket = int(citation_id[:8], 16) / 0xFFFFFFFF
    return bucket < sample_rate


def _padded_bbox(
    bbox: tuple[float, float, float, float], pad: float = _BBOX_PAD_FRACTION
) -> tuple[float, float, float, float]:
    x, y, w, h = bbox
    px = w * pad
    py = h * pad
    return (max(0.0, x - px), max(0.0, y - py), w + 2 * px, h + 2 * py)


def _polygon_to_bbox(
    polygon: list[tuple[float, float]],
) -> tuple[float, float, float, float]:
    """Shapely-style bounding rect of a polygon. Pure-python — no numpy."""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return (x0, y0, x1 - x0, y1 - y0)


def _crop_region_for_citation(
    citation: dict[str, Any],
    layout: dict[str, dict[str, Any]],
) -> Optional[tuple[float, float, float, float]]:
    """Resolve the citation's crop region in document coordinates.

    Polygon path: bounding rect of the polygon (no padding — polygon already
    encodes the true shape).  Plain bbox path: 10 % padding on each side.
    Returns None when the citation cannot be resolved (no bbox + no
    polygon + no resolvable layout block).
    """
    block = layout.get(citation.get("field_or_chunk_id", ""))
    polygon = (
        citation.get("polygon")
        or (block.get("polygon") if block else None)
    )
    if polygon:
        return _polygon_to_bbox([tuple(p) for p in polygon])
    bbox = citation.get("bbox") or (block.get("bbox") if block else None)
    if bbox:
        return _padded_bbox(tuple(bbox))
    return None


def _crop_image(
    image_bytes: bytes,
    region: tuple[float, float, float, float],
) -> bytes:
    """Return a PNG-encoded crop of ``image_bytes`` at ``region``.

    Pillow is already a transitive dep via ``documents.ocr``; we import
    lazily so the verifier-off code path never pays the import cost.
    """
    from PIL import Image  # local import — see module docstring

    img = Image.open(io.BytesIO(image_bytes))
    x, y, w, h = region
    left = max(0, int(x))
    top = max(0, int(y))
    right = min(img.width, int(x + w))
    bottom = min(img.height, int(y + h))
    if right <= left or bottom <= top:
        # Degenerate region — return the full image rather than raising,
        # so the verifier still gets *something* to look at.
        crop = img
    else:
        crop = img.crop((left, top, right, bottom))
    out = io.BytesIO()
    crop.save(out, format="PNG")
    return out.getvalue()


# ── Claude call ──────────────────────────────────────────────────────────────


# Type alias for the injectable verifier callable. Tests substitute a stub.
VerifierCall = Callable[[str, bytes], Awaitable[VerificationResult]]


async def _default_claude_verify(value: str, crop_png: bytes) -> VerificationResult:
    """Single Claude vision call. JSON-mode output, parsed to a model.

    Uses the cheapest current vision-capable Anthropic model. On any
    exception we fail closed → ``status="no"`` so the pipeline still
    runs the repoint-once recovery path.
    """
    import anthropic  # local import — kept off the verifier-off path

    client = anthropic.AsyncAnthropic()
    b64 = base64.b64encode(crop_png).decode("ascii")
    try:
        resp = await client.messages.create(
            model=VERIFIER_MODEL,
            max_tokens=200,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": VERIFIER_PROMPT.format(value=value),
                        },
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning(
            "citation_verifier_call_failed",
            extra={"error_type": type(exc).__name__},
        )
        return VerificationResult(status="no", rationale="verifier_call_failed")
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "") or ""
            break
    return _parse_verifier_response(text)


def _parse_verifier_response(text: str) -> VerificationResult:
    """Parse the JSON-mode response. Defensive — Claude sometimes wraps in fences."""
    text = text.strip()
    # Strip ```json / ``` fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return VerificationResult(status="no", rationale="unparseable_response")
    status = data.get("status", "no")
    rationale = (data.get("rationale") or "")[:100] or "no rationale"
    if status not in ("yes", "no", "partial"):
        status = "no"
    return VerificationResult(status=status, rationale=rationale)


# ── Pair iteration ───────────────────────────────────────────────────────────


_CITED_FIELDS = (
    # IntakeForm
    ("chief_concern",),
    # IntakeForm.demographics.*
    ("demographics", "name"),
    ("demographics", "dob"),
    ("demographics", "sex"),
    ("demographics", "mrn"),
    ("demographics", "address"),
    # IntakeForm.code_status
    ("code_status",),
)

_CITED_LISTS = (
    "values",            # LabReport.values
    "key_facts",         # UnknownDocument.key_facts
    "current_medications",
    "allergies",
    "family_history",
)


def _walk_cited_items(extraction: dict[str, Any]):
    """Yield ``(item_dict, item_path)`` for every cited container in extraction.

    The caller mutates the dict in place to attach verifier results /
    needs_review flags.
    """
    for path in _CITED_FIELDS:
        node: Any = extraction
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and node.get("citations"):
            yield node, ".".join(path)
    for list_key in _CITED_LISTS:
        items = extraction.get(list_key)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if isinstance(item, dict) and item.get("citations"):
                yield item, f"{list_key}[{idx}]"


# ── Granularity downgrade (partial → LINE) ───────────────────────────────────


def _downgrade_word_to_line(
    citation: dict[str, Any],
    layout: dict[str, dict[str, Any]],
) -> bool:
    """If the cited block is WORD-granularity, swap to its LINE parent.

    Returns True when a downgrade actually occurred.

    The OCR layout uses ``parent_line_id`` (when present) to point a word
    block at its line block. When absent we leave the citation untouched —
    a partial verdict on a LINE block already represents the widest
    available region for that engine.
    """
    block = layout.get(citation.get("field_or_chunk_id", ""))
    if not block:
        return False
    granularity = block.get("granularity")
    if granularity != "word":
        return False
    parent_line_id = block.get("parent_line_id")
    parent = layout.get(parent_line_id) if parent_line_id else None
    if not parent:
        return False
    citation["field_or_chunk_id"] = parent["bbox_id"]
    if "bbox" in parent:
        citation["bbox"] = tuple(parent["bbox"])
    if "polygon" in parent and parent["polygon"]:
        citation["polygon"] = [tuple(p) for p in parent["polygon"]]
    return True


# ── Repoint helper ───────────────────────────────────────────────────────────


def _repoint_excluding(
    citation: dict[str, Any],
    layout: dict[str, dict[str, Any]],
    rejected_id: str,
) -> bool:
    """Attempt one repoint that excludes ``rejected_id``.

    Walks the layout for a block whose normalized text contains the cited
    quote. Returns True when the citation was rewritten in place.
    """
    quote = (citation.get("quote_or_value") or "").strip().lower()
    if not quote:
        return False
    for bbox_id, block in layout.items():
        if bbox_id == rejected_id:
            continue
        text = str(block.get("text", "")).lower()
        if quote and quote in text:
            citation["field_or_chunk_id"] = bbox_id
            if "bbox" in block:
                citation["bbox"] = tuple(block["bbox"])
            if "polygon" in block and block["polygon"]:
                citation["polygon"] = [tuple(p) for p in block["polygon"]]
            return True
    return False


# ── Node entry ───────────────────────────────────────────────────────────────


# Type alias: page_bytes_provider(citation) -> Optional[bytes].  Returning
# None means "no image available for this citation"; the verifier degrades
# by skipping that citation rather than failing the whole node.
PageBytesProvider = Callable[[dict[str, Any]], Awaitable[Optional[bytes]]]


async def citation_verifier_node(
    state: dict[str, Any],
    *,
    page_bytes_provider: Optional[PageBytesProvider] = None,
    verifier_call: Optional[VerifierCall] = None,
) -> dict[str, Any]:
    """LangGraph node — second-pass verifier.

    Returns the standard LangGraph partial-state dict. ``state["extraction"]``
    is mutated in place (verifier results attached, citations possibly
    repointed/dropped, ``needs_review`` set).
    """
    mode = (settings.verify_citations or "off").lower()
    if mode not in ("sample", "all"):
        return {}

    extraction = state.get("extraction")
    if not isinstance(extraction, dict):
        return {}

    layout_list = state.get("ocr_layout") or []
    layout: dict[str, dict[str, Any]] = {
        b["bbox_id"]: b for b in layout_list if isinstance(b, dict) and "bbox_id" in b
    }

    sample_rate = (
        1.0 if mode == "all" else float(settings.verify_citations_sample_rate)
    )
    cap = int(settings.verify_citations_per_request_cap)
    call = verifier_call or _default_claude_verify

    calls_used = 0
    cap_skipped = 0
    outcomes: dict[str, int] = {"yes": 0, "no": 0, "partial": 0}
    t0 = time.monotonic()

    for item, item_path in _walk_cited_items(extraction):
        value_text = (
            item.get("value")
            or item.get("text")
            or item.get("name")
            or item.get("substance")
            or item.get("condition")
            or item.get("test_name")
            or ""
        )
        citations = item.get("citations") or []
        keep: list[dict[str, Any]] = []
        any_dropped = False
        for cit_idx, citation in enumerate(citations):
            cid = _citation_id(0, cit_idx, citation)
            if not _should_sample(cid, sample_rate):
                keep.append(citation)
                continue
            if calls_used >= cap:
                # PRE-call cap. Skip remaining sampled citations uniformly.
                agent_verifier_capped_total.labels(reason="cap").inc()
                cap_skipped += 1
                keep.append(citation)
                continue
            region = _crop_region_for_citation(citation, layout)
            if region is None:
                keep.append(citation)
                continue
            page_bytes = (
                await page_bytes_provider(citation)
                if page_bytes_provider is not None
                else None
            )
            if page_bytes is None:
                keep.append(citation)
                continue
            try:
                crop = _crop_image(page_bytes, region)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "citation_verifier_crop_failed",
                    extra={"error_type": type(exc).__name__, "path": item_path},
                )
                keep.append(citation)
                continue

            calls_used += 1
            result = await call(value_text, crop)
            outcomes[result.status] = outcomes.get(result.status, 0) + 1
            agent_citation_verifier_outcome_total.labels(outcome=result.status).inc()
            citation["verification"] = result.model_dump()

            if result.status == "yes":
                keep.append(citation)
                continue
            if result.status == "partial":
                _downgrade_word_to_line(citation, layout)
                keep.append(citation)
                continue
            # status == "no" — repoint once and re-verify.
            rejected_id = citation.get("field_or_chunk_id", "")
            if _repoint_excluding(citation, layout, rejected_id) and calls_used < cap:
                region2 = _crop_region_for_citation(citation, layout)
                if region2 is not None:
                    try:
                        crop2 = _crop_image(page_bytes, region2)
                    except Exception:  # noqa: BLE001
                        crop2 = None
                    if crop2 is not None:
                        calls_used += 1
                        result2 = await call(value_text, crop2)
                        outcomes[result2.status] = outcomes.get(result2.status, 0) + 1
                        agent_citation_verifier_outcome_total.labels(
                            outcome=result2.status
                        ).inc()
                        citation["verification"] = result2.model_dump()
                        if result2.status != "no":
                            keep.append(citation)
                            continue
            # Still ``no`` (or repoint failed) — drop the citation entirely.
            any_dropped = True
            logger.info(
                "citation_verifier_drop",
                extra={
                    "request_id": state.get("request_id"),
                    "path": item_path,
                    "rejected_field_or_chunk_id": rejected_id,
                },
            )
        item["citations"] = keep
        if any_dropped:
            # At least one citation for this value was rejected twice → flag
            # the value for clinician review.
            item["needs_review"] = True

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "citation_verifier_summary",
        extra={
            "request_id": state.get("request_id"),
            "session_id": state.get("session_id"),
            "mode": mode,
            "calls_used": calls_used,
            "cap_skipped": cap_skipped,
            "yes": outcomes["yes"],
            "no": outcomes["no"],
            "partial": outcomes["partial"],
            "duration_ms": duration_ms,
        },
    )
    return {"extraction": extraction}


__all__ = [
    "VERIFIER_MODEL",
    "VERIFIER_PROMPT",
    "VerificationResult",
    "citation_verifier_node",
    "_citation_id",
    "_should_sample",
    "_padded_bbox",
    "_polygon_to_bbox",
    "_parse_verifier_response",
]
